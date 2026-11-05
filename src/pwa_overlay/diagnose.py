"""OCR diagnostics: compare OCR settings on one frame and show what gets read.

    uv run python -m pwa_overlay.diagnose --image shot.png
    uv run python -m pwa_overlay.diagnose            # waits for the client to be active

Writes logs/diag/<time>/: raw.png, report.txt and annotated_<variant>.png where
green = translated, orange = Chinese read but not in the dictionary,
gray = other text.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

from .analyzer import Analyzer, OcrParams, translate_lines
from .config import Config, app_root
from .dictionary import Dictionary, has_cjk

VARIANTS = {
    "default": OcrParams(),
    "x1.5": OcrParams(scale=1.5),
    "x2": OcrParams(scale=2.0),
    "x2_low": OcrParams(scale=2.0, box_thresh=0.3, text_score=0.3),
}


def load_image(path: Path) -> np.ndarray:
    import cv2

    data = np.fromfile(str(path), dtype=np.uint8)  # handles non-ASCII paths
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read image: {path}")
    return img


def capture_client(cfg: Config, wait_s: float) -> np.ndarray:
    import mss

    from . import win32

    deadline = time.monotonic() + wait_s
    print(f"waiting up to {wait_s:.0f}s for {cfg.target_exe} to be the active window...")
    while time.monotonic() < deadline:
        wins = win32.find_target_windows(cfg.target_exe, cfg.target_title,
                                         exclude_pid=os.getpid())
        fg = win32.foreground_pid()
        active = [w for w in wins if w.pid == fg]
        if active:
            time.sleep(0.8)  # let it repaint after activation
            r = max(active, key=lambda w: w.rect.w * w.rect.h).rect
            with getattr(mss, "MSS", mss.mss)() as sct:
                shot = sct.grab({"left": r.x, "top": r.y, "width": r.w, "height": r.h})
            return np.ascontiguousarray(np.asarray(shot)[:, :, :3])
        time.sleep(0.3)
    raise SystemExit("client never became the active window")


def annotate(frame: np.ndarray, analysis, out: Path) -> None:
    import cv2

    img = frame.copy()
    translated = {(i.x0, i.y0) for i in analysis.items}
    for it in analysis.items:
        cv2.rectangle(img, (it.x0, it.y0), (it.x1, it.y1), (60, 200, 60), 2)
    for ln in analysis.lines:
        if any(abs(ln.x0 - x) < 4 and abs(ln.y0 - y) < 4 for x, y in translated):
            continue
        color = (0, 140, 255) if has_cjk(ln.text) else (150, 150, 150)
        cv2.rectangle(img, (ln.x0, ln.y0), (ln.x1, ln.y1), color, 1)
    cv2.imencode(".png", img)[1].tofile(str(out))


def preview(frame: np.ndarray, cfg: Config, root: Path, out: Path) -> int:
    import cv2
    from PyQt6.QtGui import QGuiApplication, QImage, QPainter

    from .overlay import draw_items, paint_items
    from .translator import make_translator

    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # needed for fonts
    dictionary = Dictionary.from_dir(root / "data" / "mappings",
                                     root / "data" / "user_dict.json", cfg.fuzzy_cutoff)
    translator = make_translator(dictionary, root, cfg.machine_translation, cfg.mt_model,
                                 cfg.mt_thinking, cfg.mt_max_calls_per_hour,
                                 cfg.mt_max_calls_per_day)
    analyzer = Analyzer(translator, cfg.ocr_params())

    t0 = time.perf_counter()
    lines = analyzer.ocr.read(frame)
    t_ocr = time.perf_counter() - t0
    first = translate_lines(frame, lines, translator)
    t1 = time.perf_counter()
    while translator.pending_count() and time.perf_counter() - t1 < 60:
        time.sleep(0.05)
    t_mt = time.perf_counter() - t1
    final = translate_lines(frame, lines, translator)
    translator.close()

    h, w = frame.shape[:2]
    rgb = np.ascontiguousarray(frame[:, :, ::-1])
    img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
    p = QPainter(img)
    paint_items(p, draw_items(final.items, lines, w, 0, 0, h), 0, 0, cfg.font_family)
    p.end()
    img.save(str(out / "preview.png"))

    by_method: dict[str, int] = {}
    for i in final.items:
        by_method[i.method] = by_method.get(i.method, 0) + 1
    report = [
        f"OCR {t_ocr * 1000:.0f}ms, lines={len(lines)}",
        f"before Claude: translated={len(first.items)}",
        f"after Claude ({t_mt:.1f}s): translated={len(final.items)} "
        f"untranslated={len(final.untranslated)} by={by_method}",
        f"translator: {translator.status}",
        "",
    ]
    report += [f"[{i.method}] {i.zh} -> {i.ko}" for i in final.items]
    shaky = [i for i in final.items if i.uncertain]
    if shaky:
        report += ["", "-- read with low OCR confidence (if one keeps coming back wrong, "
                       "add the misread text to the \"OCR 오인식\" group in data/user_dict.json):"]
        report += [f"  {i.zh} -> {i.ko} ({i.method})" for i in shaky]
    if final.untranslated:
        report += ["", "-- still untranslated:"] + final.untranslated
    (out / "preview.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    print(f"\nwritten to {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, help="analyze a saved screenshot")
    ap.add_argument("--wait", type=float, default=120)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--preview", action="store_true",
                    help="translate with the full chain (dictionary, cache, Claude) and "
                         "save preview.png showing what the overlay would draw")
    args = ap.parse_args()

    import cv2

    root = app_root()
    cfg = Config.load(root / "config.json")
    frame = load_image(args.image) if args.image else capture_client(cfg, args.wait)

    out = root / "logs" / "diag" / time.strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", frame)[1].tofile(str(out / "raw.png"))

    if args.preview:
        return preview(frame, cfg, root, out)

    dictionary = Dictionary.from_dir(root / "data" / "mappings",
                                     root / "data" / "user_dict.json")
    analyzer = Analyzer(dictionary)  # dictionary only: shows what the dictionary covers
    report = [f"frame {frame.shape[1]}x{frame.shape[0]}, dictionary {len(dictionary)}"]
    summary = []
    base_cjk: set[str] | None = None
    for name in args.variants.split(","):
        analyzer.ocr.params = VARIANTS[name]
        analyzer.ocr.reuse.MAX_READS = 0  # time real recognition, not reuse
        for _ in range(2):  # warm-up: engine load and first-shape costs are not OCR time
            analyzer.ocr.read(frame)
        times = []
        for _ in range(3):
            analyzer.ocr.reuse.reset()
            t = time.perf_counter()
            lines = analyzer.ocr.read(frame)
            times.append((time.perf_counter() - t) * 1000)
        ms = sorted(times)[1]
        analysis = translate_lines(frame, lines, dictionary)
        cjk = [l for l in lines if has_cjk(l.text)]
        texts = {l.text for l in cjk}
        if base_cjk is None:
            base_cjk = texts
            diff = ""
        else:
            diff = f"  +{len(texts - base_cjk)}/-{len(base_cjk - texts)} vs {args.variants.split(',')[0]}"
        summary.append(f"{name:8s} {ms:6.0f}ms  lines={len(lines):3d}  chinese={len(cjk):3d}  "
                       f"translated={len(analysis.items):3d}  "
                       f"untranslated={len(analysis.untranslated):3d}{diff}")
        report.append(f"\n=== {name} ===")
        report += [f"  [{l.score:.2f}] ({l.x0},{l.y0}) {l.text}" for l in lines]
        report.append("  -- translated:")
        report += [f"    {i.zh} -> {i.ko} ({i.method})" for i in analysis.items]
        annotate(frame, analysis, out / f"annotated_{name}.png")
    report[1:1] = summary
    (out / "report.txt").write_text("\n".join(report), encoding="utf-8")
    print("(dictionary only - add --preview to include the cache and Claude)")
    print("\n".join(summary))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
