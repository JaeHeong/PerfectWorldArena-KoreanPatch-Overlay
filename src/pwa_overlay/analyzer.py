"""Frame -> translated overlay items.

OCR runs on a BGR numpy frame of one window. Detected lines are grouped into
paragraph runs (wrapped sentences are translated whole; headings and list items
on their own), each unit is looked up through the translator, and for every hit we
measure where the original ink is and which colours to paint so the Korean covers
the Chinese cleanly.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .dictionary import has_cjk

log = logging.getLogger(__name__)

UNCERTAIN_SCORE = 0.85   # line confidence below this is "OCR uncertain"
UNCERTAIN_CHAR = 0.5     # ...as is any line with a character below this
ART_SPREAD = 30.0        # background spread above this = text drawn over artwork
INK_DIFF = 40            # gray-level change that counts as a changed ink pixel
INK_CHANGED_PIXELS = 6   # this many changed ink pixels invalidate a translation

_LIST_MARKER = re.compile(r"^\s*(\d+[.、．)）]|[（(]\d+[）)]|[•·\-*])")
_TERMINATOR = re.compile(r"[。！？!?；;]\s*$")
_DATE_TIME = re.compile(r"(\d{4}\.\d{1,2}\.\d{1,2})(\d{1,2}:\d{2})")
_SPLIT_GAP = re.compile(r"(?<=[㐀-鿿:：])\s+(?=[㐀-鿿])")
_LATIN3 = re.compile(r"[A-Za-z0-9]{3,}")


@dataclass
class Line:
    x0: int
    y0: int
    x1: int
    y1: int
    text: str
    score: float
    chars: tuple[float, ...] | None = None  # per-character confidence, if known

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def uncertain(self) -> bool:
        return self.score < UNCERTAIN_SCORE or bool(
            self.chars and min(self.chars) < UNCERTAIN_CHAR)


@dataclass
class Item:
    """A translated region, in window-relative pixel coordinates.

    (x0, y0, x1, y1) is the padded detector box, used to check whether the text
    underneath changed; `fill` is what gets painted (the measured ink area, or the
    whole box for text over artwork)."""
    x0: int
    y0: int
    x1: int
    y1: int
    zh: str
    ko: str
    method: str
    bg: tuple[int, int, int]  # RGB
    fg: tuple[int, int, int]  # RGB
    line_count: int = 1
    fill: tuple[int, int, int, int] = (0, 0, 0, 0)
    font_px: int = 0
    art: bool = False
    uncertain: bool = False
    gray: np.ndarray | None = field(default=None, repr=False)  # OCR-time gray crop
    bg_lum: float = 0.0
    ink_lum: float = -1.0  # gray level of the glyphs at OCR time (-1: unknown)


@dataclass
class Analysis:
    items: list[Item]
    untranslated: list[str]
    lines: list[Line]
    version: int = 0  # translator version these lookups saw


@dataclass
class OcrParams:
    scale: float = 1.0          # upscale before OCR (1.0 measured best; see diagnose)
    box_thresh: float = 0.5     # detector box confidence
    unclip_ratio: float = 1.6   # how much detected boxes are grown
    text_score: float = 0.5     # recognizer confidence cutoff
    device: str = "auto"        # auto | gpu | cpu  (gpu = DirectML)
    model: str = "medium"       # PP-OCRv6 size: small | medium
    cpu_threads: int = 4        # cap CPU use so a running game keeps its cores


def to_gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    import cv2

    return cv2.cvtColor(np.ascontiguousarray(frame[:, :, :3]), cv2.COLOR_BGR2GRAY)


# ---- OCR engine ---------------------------------------------------------------
class _RecReuse:
    """Reuse recognition results for text crops that are pixel-identical to a crop
    from the previous read (recognition is 70-90% of OCR time). Forgotten
    periodically so earlier misreads get a fresh look."""

    MAX_READS = 20
    MAX_AGE_S = 30.0

    def __init__(self):
        self.prev: list[tuple[np.ndarray, str, float, object]] = []
        self.reads = 0
        self.since = time.monotonic()

    def reset(self) -> None:
        self.prev, self.reads, self.since = [], 0, time.monotonic()

    def tick(self) -> None:
        self.reads += 1
        if self.reads > self.MAX_READS or time.monotonic() - self.since > self.MAX_AGE_S:
            self.reset()

    def find(self, crop: np.ndarray):
        for old, txt, score, word in self.prev:
            if old.shape != crop.shape:
                continue
            d = np.abs(old.astype(np.int16) - crop.astype(np.int16))
            if d.mean() < 2 and d.max() < 40:
                return txt, score, word
        return None


class OcrEngine:
    """RapidOCR (PP-OCRv6). Loaded lazily; release() frees GPU/CPU memory while idle.
    `force_cpu` (set while a game is running) switches to the small CPU model."""

    def __init__(self, params: OcrParams | None = None):
        self.params = params or OcrParams()
        self._engine = None
        self.device = "none"
        self.force_cpu = False
        self.reuse = _RecReuse()
        self._lock = threading.Lock()
        self.warm_shape: tuple[int, int] | None = None
        self.pad_shape: tuple[int, int] = (0, 0)  # GPU: one input canvas size

    def _build(self, use_gpu: bool, model: str, threads: int):
        from rapidocr import ModelType, OCRVersion, RapidOCR

        size = ModelType.MEDIUM if model == "medium" else ModelType.SMALL
        engine = RapidOCR(params={
            "Global.use_cls": False,          # UI text is never rotated
            "Global.log_level": "error",
            "Global.max_side_len": 4096,      # default (2000) would undo upscaling
            "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": size,
            # never upscale for detection: the default ("min" side to 736) blows a
            # 64x800 side strip of the client up to 736x9200 (1+ GB spikes, seconds
            # of CPU). UI text is already at native size; only shrink huge frames.
            "Det.limit_type": "max", "Det.limit_side_len": 2048,
            "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": size,
            "EngineConfig.onnxruntime.use_dml": use_gpu,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
        self._tune(engine)
        return engine

    def _tune(self, engine) -> None:
        """float32 det normalisation (rapidocr promotes the frame to float64)."""
        try:
            det = engine._load_det_model()
            pp = getattr(det, "preprocess_op", None)
            if pp is not None and hasattr(pp, "mean"):
                pp.mean = np.asarray(pp.mean, dtype=np.float32)
                pp.std = np.asarray(pp.std, dtype=np.float32)
                pp.scale = np.float32(pp.scale)
        except Exception:  # noqa: BLE001 (optimisation only)
            log.debug("det preprocess tuning skipped", exc_info=True)
        self._wrap_rec(engine)

    def _wrap_rec(self, engine) -> None:
        original = engine.recognize_txt
        reuse = self.reuse

        def recognize_txt(img_list):
            try:
                from rapidocr.ch_ppocr_rec.typings import TextRecOutput

                hits = [reuse.find(c) for c in img_list]
                todo = [i for i, h in enumerate(hits) if h is None]
                fresh = original([img_list[i] for i in todo]) if todo else None
                txts, scores, words = [], [], []
                k = 0
                for i, h in enumerate(hits):
                    if h is None:
                        txts.append(fresh.txts[k])
                        scores.append(fresh.scores[k])
                        words.append(fresh.word_results[k])
                        k += 1
                    else:
                        txts.append(h[0]); scores.append(h[1]); words.append(h[2])
                reuse.prev = [(c, t, s, w) for c, t, s, w in
                              zip(img_list, txts, scores, words)]
                return TextRecOutput(imgs=list(img_list), txts=tuple(txts),
                                     scores=list(scores), word_results=tuple(words),
                                     elapse=getattr(fresh, "elapse", 0.0))
            except Exception:  # noqa: BLE001 (never let the optimisation break OCR)
                log.debug("rec reuse failed, full recognition", exc_info=True)
                reuse.reset()
                return original(img_list)

        engine.recognize_txt = recognize_txt

    def load(self):
        with self._lock:
            want_cpu = self.force_cpu or self.params.device == "cpu"
            if self._engine is not None and self.device.startswith("cpu") != want_cpu:
                self._release_locked()  # game started/stopped: switch engines
            if self._engine is not None:
                return self._engine
            p = self.params
            if not want_cpu:
                try:
                    self._engine = self._build(True, p.model, p.cpu_threads)
                    self.device = f"gpu/{p.model}"
                except Exception:
                    if p.device == "gpu":
                        raise
                    log.warning("GPU OCR unavailable, falling back to CPU", exc_info=True)
            if self._engine is None:
                model = "small"  # medium on CPU takes ~5 s per frame: never worth it
                threads = 2 if self.force_cpu else p.cpu_threads
                self._engine = self._build(False, model, threads)
                self.device = f"cpu/{model}"
            self.reuse.reset()
            log.info("OCR engine loaded (%s)", self.device)
            return self._engine

    def _release_locked(self) -> None:
        if self._engine is not None:
            self._engine = None
            self.reuse.reset()
            self.warm_shape = None
            self.pad_shape = (0, 0)
            log.info("OCR engine released (%s)", self.device)
            self.device = "none"

    def release(self) -> None:
        with self._lock:
            self._release_locked()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def warmup(self, h: int, w: int) -> None:
        """DirectML sessions are fastest on the first input shape they see; feed the
        real frame size first so later OCRs don't run on the slow path."""
        engine = self.load()
        if self.warm_shape == (h, w):
            return
        try:
            from rapidocr.ch_ppocr_rec.typings import TextRecInput

            t = time.perf_counter()
            engine(np.zeros((h, w, 3), np.uint8), use_cls=False)  # det at frame shape
            rec = engine._load_rec_model()
            rec(TextRecInput(img=[np.zeros((48, 320, 3), np.uint8)] * 6))
            self.warm_shape = (h, w)
            log.info("OCR warm-up %dx%d on %s: %.0f ms", w, h, self.device,
                     (time.perf_counter() - t) * 1000)
        except Exception:  # noqa: BLE001
            log.debug("warm-up failed", exc_info=True)

    def read(self, frame_bgr: np.ndarray) -> list[Line]:
        import cv2

        engine = self.load()
        p = self.params
        img = frame_bgr
        if p.scale != 1.0:
            img = cv2.resize(frame_bgr, None, fx=p.scale, fy=p.scale,
                             interpolation=cv2.INTER_CUBIC)
        if self.device.startswith("gpu"):
            # DirectML keeps separate (large) buffers for every input shape it sees:
            # pad smaller frames (popups, tooltips) to one canvas so memory stays flat
            ph, pw = self.pad_shape
            h, w = img.shape[:2]
            self.pad_shape = (max(ph, h), max(pw, w))
            ph, pw = self.pad_shape
            if (h, w) != (ph, pw):
                canvas = np.zeros((ph, pw, 3), img.dtype)
                canvas[:h, :w] = img
                img = canvas
        self.reuse.tick()
        out = engine(img, use_cls=False, box_thresh=p.box_thresh,
                     unclip_ratio=p.unclip_ratio, text_score=p.text_score,
                     return_word_box=True)
        lines: list[Line] = []
        if out.boxes is None or out.txts is None:
            return lines
        words = list(out.word_results or [])
        for i, (box, text, score) in enumerate(zip(out.boxes, out.txts, out.scores)):
            score = float(score)
            if score < p.text_score or not text.strip():
                continue
            xs = [pt[0] / p.scale for pt in box]
            ys = [pt[1] / p.scale for pt in box]
            chars = _char_info(words[i] if i < len(words) else None, text, p.scale)
            lines.extend(_split_line(int(min(xs)), int(min(ys)), int(round(max(xs))),
                                     int(round(max(ys))), text.strip(), score, chars))
        return lines


def _char_info(word, text: str, scale: float):
    """[(char, conf, x0, x1)] for the non-space characters of `text`, or None."""
    try:
        chars = [(c, float(s), min(pt[0] for pt in b) / scale, max(pt[0] for pt in b) / scale)
                 for c, s, b in word]
    except Exception:  # noqa: BLE001 (format differs / missing)
        return None
    if "".join(c for c, *_ in chars) != text.replace(" ", ""):
        return None
    return chars


def _split_line(x0, y0, x1, y1, text, score, chars) -> list[Line]:
    """Split one detector box that spans two UI labels ("活动规则 神马币："), and put
    the space back between a date and a time ("2026.10.2312:00")."""
    text = _DATE_TIME.sub(r"\1 \2", text)
    parts = _SPLIT_GAP.split(text)
    if len(parts) == 1 or chars is None:
        conf = tuple(c[1] for c in chars) if chars else None
        return [Line(x0, y0, x1, y1, text, score, conf)]
    out, k = [], 0
    for part in parts:
        n = len(part.replace(" ", ""))
        seg = chars[k:k + n]
        k += n
        if not seg:
            continue
        confs = tuple(c[1] for c in seg)
        out.append(Line(int(seg[0][2]), y0, int(round(seg[-1][3])), y1, part.strip(),
                        min(score, min(confs)), confs))
    return out or [Line(x0, y0, x1, y1, text, score, None)]


# ---- grouping -------------------------------------------------------------------
def group_paragraphs(lines: list[Line]) -> list[list[Line]]:
    """Group vertically stacked lines of similar height that share a left edge or a
    centre (centred dialogs) and have small gaps."""
    ordered = sorted(lines, key=lambda l: (l.y0, l.x0))
    used = [False] * len(ordered)
    groups: list[list[Line]] = []
    for i, first in enumerate(ordered):
        if used[i]:
            continue
        used[i] = True
        group = [first]
        cur = first
        for j in range(i + 1, len(ordered)):
            if used[j]:
                continue
            nxt = ordered[j]
            gap = nxt.y0 - cur.y1
            if gap > cur.h * 0.9:
                if nxt.y0 > cur.y1 + cur.h * 2:
                    break
                continue
            if gap < -cur.h * 0.3:
                continue  # same row, different element
            tol = max(6, first.h * 0.6)
            left_ok = abs(nxt.x0 - first.x0) <= tol
            centre_ok = abs((nxt.x0 + nxt.x1) - (first.x0 + first.x1)) / 2 <= tol
            value_x = _value_start(first)  # "描述：xxxx" continues under the value
            hang_ok = value_x is not None and abs(nxt.x0 - value_x) <= tol
            if not (left_ok or centre_ok or hang_ok):
                continue
            if abs(nxt.h - cur.h) > max(3, cur.h * 0.35):
                continue
            used[j] = True
            group.append(nxt)
            cur = nxt
        groups.append(group)
    return groups


_FIELD_LABEL = re.compile(r"^[一-鿿]{1,6}[：:]")  # "描述：", "说明：" ...


def _value_start(line: Line) -> float | None:
    """x where the value starts in a "label：value" line, estimated from the text."""
    m = _FIELD_LABEL.match(line.text)
    if not m or len(line.text) <= m.end():
        return None
    return line.x0 + line.w * m.end() / len(line.text)


def _full(line: Line, group: list[Line]) -> bool:
    """Does the line run to the group's right edge (i.e. the UI wrapped it)? Measured
    at the right edge, so indented continuation lines count too."""
    right = max(l.x1 for l in group)
    span = right - min(l.x0 for l in group)
    return line.x1 >= right - max(2 * line.h, 0.2 * span)


def split_runs(group: list[Line]) -> list[list[Line]]:
    """Split a vertical group into translation units: a new run starts before a list
    marker, after a sentence terminator, and after a short non-last line (a heading
    or a one-line item)."""
    if len(group) < 2:
        return [group]
    runs, cur = [], [group[0]]
    for prev, line in zip(group, group[1:]):
        if (_LIST_MARKER.match(line.text) or _TERMINATOR.search(prev.text)
                or not _full(prev, group)):
            runs.append(cur)
            cur = []
        cur.append(line)
    runs.append(cur)
    return runs


def looks_wrapped(run: list[Line]) -> bool:
    """One sentence wrapped by the UI: same text size, every line but the last runs
    to the right edge and carries real text."""
    if len(run) < 2:
        return False
    heights = [l.h for l in run]
    if max(heights) > min(heights) * 1.2 + 1:
        return False  # a title over a tag/subtitle, not a wrapped sentence
    return all(_full(l, run) and len(re.findall(r"[㐀-鿿]", l.text)) >= 8
               for l in run[:-1])


# ---- colours, ink, invalidation ------------------------------------------------
def _lum(c) -> float:
    def ch(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _sample(frame, raw, padded):
    """-> bg RGB, fg RGB, spread (art if large), ink rect in frame coords."""
    rx0, ry0, rx1, ry1 = raw
    px0, py0, px1, py1 = padded
    h = ry1 - ry0
    box = frame[ry0:ry1, rx0:rx1, :3].astype(np.int32)
    if box.size == 0:
        return (0, 0, 0), (255, 255, 255), 0.0, raw
    if h >= 8 and (rx1 - rx0) >= 8:
        k = int(min(max(round(0.14 * h), 1), h // 4))
        mask = np.ones(box.shape[:2], bool)
        mask[k:-k, k:-k] = False
        ring = box[mask]
        interior = box[k:-k, k:-k].reshape(-1, 3)
    else:
        ring = np.concatenate([
            frame[py0, px0:px1, :3], frame[py1 - 1, px0:px1, :3],
            frame[py0:py1, px0, :3], frame[py0:py1, px1 - 1, :3]]).astype(np.int32)
        interior = box.reshape(-1, 3)
    bg = np.median(ring, axis=0)
    spread = float(np.median(np.abs(ring - bg), axis=0).max())
    dist = np.abs(interior - bg).sum(axis=1)
    strong = interior[dist >= max(np.percentile(dist, 92), 60)] if len(interior) else interior
    if len(strong) == 0:
        lum = 0.299 * bg[2] + 0.587 * bg[1] + 0.114 * bg[0]
        fg = np.array([20, 20, 20]) if lum > 140 else np.array([235, 235, 235])
    else:
        fg = strong.mean(axis=0)
    bg_rgb = (int(bg[2]), int(bg[1]), int(bg[0]))
    fg_rgb = (int(fg[2]), int(fg[1]), int(fg[0]))
    if contrast(fg_rgb, bg_rgb) < 3.0:
        fg_rgb = (0, 0, 0) if contrast((0, 0, 0), bg_rgb) > contrast((255, 255, 255), bg_rgb) \
            else (255, 255, 255)

    # ink rect: rows/cols with clearly-not-background pixels
    thr = max(40, 0.5 * float(np.abs(fg - bg).max()))
    ink = np.abs(box - bg).max(axis=2) > thr
    rows = np.where(ink.sum(axis=1) >= 2)[0]
    cols = np.where(ink.sum(axis=0) >= 2)[0]
    if len(rows) and len(cols) and ink.mean() < 0.9:
        ix0 = max(rx0, rx0 + int(cols[0]) - 2)
        ix1 = min(rx1, rx0 + int(cols[-1]) + 3)
        iy0 = max(ry0, ry0 + int(rows[0]) - 2)
        iy1 = min(ry1, ry0 + int(rows[-1]) + 3)
        ink_rect = (ix0, iy0, ix1, iy1)
    else:
        ink_rect = raw
    return bg_rgb, fg_rgb, spread, ink_rect


def _make_item(frame, gray, group: list[Line], ko: str, method: str, pad: int,
               uncertain: bool = False) -> Item:
    h, w = frame.shape[:2]
    raw = (max(0, min(l.x0 for l in group)), max(0, min(l.y0 for l in group)),
           min(w, max(l.x1 for l in group)), min(h, max(l.y1 for l in group)))
    padded = (max(0, raw[0] - pad), max(0, raw[1] - pad),
              min(w, raw[2] + pad), min(h, raw[3] + pad))
    zh = "".join(l.text for l in group)
    bg, fg, spread, ink = _sample(frame, raw, padded)
    art = spread > ART_SPREAD
    if len(group) == 1:
        ink_h = ink[3] - ink[1] if ink != raw else int(0.65 * (raw[3] - raw[1]))
        fill = raw if art else ink
        font_px = max(9, int(ink_h / 0.78))
        if art:  # display titles: keep Korean no taller than the original ink
            font_px = min(font_px, int((raw[3] - raw[1]) * 0.9))
    else:
        heights = []
        for l in group:
            _, _, _, li = _sample(frame, (l.x0, l.y0, l.x1, l.y1), (l.x0, l.y0, l.x1, l.y1))
            heights.append(li[3] - li[1])
        font_px = max(9, int(float(np.median(heights)) / 0.78))
        fill = raw
    x0, y0, x1, y1 = padded
    bg_lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    crop = gray[y0:y1, x0:x1].copy()
    far = np.abs(crop.astype(np.int16) - bg_lum) > INK_DIFF
    ink_lum = float(np.median(crop[far])) if far.any() else -1.0
    return Item(x0, y0, x1, y1, zh, ko, method, bg, fg, len(group), fill, font_px, art,
                uncertain, crop, bg_lum, ink_lum)


def signature_changed(frame_or_gray: np.ndarray, item: Item) -> bool:
    """Has the text under this item changed since OCR? Compares only ink pixels
    (pixels far from the item's background, then or now), so a glyph change (e.g. one
    digit) is caught while an animated background behind the text is ignored."""
    gray = to_gray(frame_or_gray)
    h, w = gray.shape[:2]
    if item.x1 > w or item.y1 > h or item.gray is None:
        return True
    cur = gray[item.y0:item.y1, item.x0:item.x1].astype(np.int16)
    old = item.gray.astype(np.int16)
    if cur.shape != old.shape:
        return True
    if item.ink_lum >= 0:
        # glyph pixels only: far from the background AND close to the text's own
        # gray level, so a moving background (even artwork) behind the text is
        # ignored, while a changed glyph (one digit) still changes its strokes
        ink = (np.abs(old - item.bg_lum) > INK_DIFF) & (np.abs(old - item.ink_lum) < 45)
    else:
        ink = (np.abs(old - item.bg_lum) > INK_DIFF) | (np.abs(cur - item.bg_lum) > INK_DIFF)
    changed = (np.abs(cur - old) > INK_DIFF) & ink
    return int(changed.sum()) >= max(INK_CHANGED_PIXELS, 0.04 * int(ink.sum()))


# ---- translation units ------------------------------------------------------------
def reading_order_context(lines: list[Line], limit: int = 60) -> tuple[str, ...]:
    """Screen text in reading order (rows of ~12px, then left to right), including
    Latin labels like SEASON/RANK that hint at what a card is."""
    keep = [l for l in lines if has_cjk(l.text) or _LATIN3.search(l.text)]
    keep.sort(key=lambda l: (round(l.y0 / 12), l.x0))
    return tuple(l.text for l in keep[:limit])


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _lookup(translator, text: str, **kw):
    # Dictionary.lookup ignores the extra keyword arguments the Translator uses
    return translator.lookup(text, **kw)


def translate_lines(frame: np.ndarray, lines: list[Line], translator, pad: int = 2,
                    prev_items: list[Item] | None = None) -> Analysis:
    """`translator` is anything with lookup(text, ...) -> Match | None (a Dictionary,
    or a Translator that may queue misses for Claude). `prev_items` are translations
    still valid for this frame: an OCR variant of the same text reuses them instead
    of being re-translated."""
    version = getattr(translator, "version", 0)  # before any lookup (no lost repaints)
    gray = to_gray(frame)
    context = reading_order_context(lines)
    prev = prev_items or []
    items: list[Item] = []
    untranslated: list[str] = []

    def unit(group: list[Line], enqueue: bool = True, allow_phrase: bool = True,
             dict_only: bool = False) -> bool:
        text = "".join(l.text for l in group)
        uncertain = any(l.uncertain for l in group)
        chars = None
        if len(group) == 1 and group[0].chars and len(group[0].chars) == len(text.replace(" ", "")):
            chars = tuple(zip(text.replace(" ", ""), group[0].chars))
        kw = dict(allow_phrase=allow_phrase, context=context,
                  score=min(l.score for l in group), uncertain=uncertain, chars=chars)
        if dict_only:
            kw["dict_only"] = True
        m = _lookup(translator, text, enqueue=False, **kw)
        if m is None and prev:
            box = (min(l.x0 for l in group), min(l.y0 for l in group),
                   max(l.x1 for l in group), max(l.y1 for l in group))
            for p in prev:
                if _iou(box, (p.x0, p.y0, p.x1, p.y1)) >= 0.6:
                    items.append(_make_item(frame, gray, group, p.ko, "carry", pad, uncertain))
                    return True
        if m is None and enqueue:
            m = _lookup(translator, text, enqueue=True, **kw)
        if m is None:
            return False
        items.append(_make_item(frame, gray, group, m.ko, m.method, pad, uncertain))
        return True

    for group in group_paragraphs(lines):
        if not any(has_cjk(l.text) for l in group):
            continue
        for run in split_runs(group):
            cjk = [l for l in run if has_cjk(l.text)]
            if not cjk:
                continue
            if len(run) > 1:
                wrapped = looks_wrapped(run)
                # phrase-stitching across separate lines would glue labels together
                # separate labels stacked up: only a dictionary entry may join them
                if unit(run, enqueue=wrapped, allow_phrase=False, dict_only=not wrapped):
                    continue
                if wrapped:  # translate the sentence as a whole, not line by line
                    untranslated.append("".join(l.text for l in run))
                    continue
            for line in cjk:
                if not unit([line]):
                    untranslated.append(line.text)
    return Analysis(items, untranslated, lines, version)


class Analyzer:
    def __init__(self, translator, params: OcrParams | None = None):
        self.translator = translator  # Dictionary or Translator
        self.ocr = OcrEngine(params)

    def analyze(self, frame_bgr: np.ndarray, prev_items: list[Item] | None = None) -> Analysis:
        frame_bgr = np.ascontiguousarray(frame_bgr[:, :, :3])
        lines = self.ocr.read(frame_bgr)
        return translate_lines(frame_bgr, lines, self.translator, prev_items=prev_items)
