"""Main loop: track the client's windows, capture, OCR on change, update overlay.

Game safety: whenever a game (cs2.exe) is the foreground window, nothing is captured
or drawn, the overlay window is hidden and the OCR engine (and its GPU memory) is
released. While a game merely runs in the background, OCR uses the small CPU model.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

try:  # DWM composes layered windows anyway; the flag only makes GDI hide/reshow the cursor
    import mss.windows.gdi as _mss_gdi
    _mss_gdi.CAPTUREBLT = 0
except Exception:  # noqa: BLE001 (mss layout changed: harmless, keep the default)
    pass
import mss
import numpy as np
from PyQt6.QtCore import QObject, QRect, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import win32
from .analyzer import (Analysis, Analyzer, Item, Line, signature_changed, to_gray,
                       translate_lines)
from .config import Config
from .dictionary import Dictionary, has_cjk, normalize
from .currency import FxRate, add_krw
from .overlay import DrawItem, OverlayWindow, draw_items, paint_items
from .translator import Translator, make_translator

log = logging.getLogger(__name__)

CELL = 16                 # change detection grid (pixels)
CELL_CHANGED_PIXELS = 6   # this many pixels changing by > CHANGE_DIFF mark a cell changed
CHANGE_DIFF = 40
INVALIDATION_SPACING_S = 2.5  # text that keeps changing (countdowns) re-OCRs this rarely


@dataclass
class WindowState:
    hwnd: int
    rect: win32.Rect
    items: list[Item] = field(default_factory=list)
    gray: np.ndarray | None = None          # previous frame (full resolution)
    dirty: bool = True
    dirty_since: float = 0.0
    last_change: float = 0.0
    from_invalidation: bool = False         # dirty only because an item's text changed
    future: Future | None = None
    ocr_size: tuple[int, int] = (0, 0)
    ocr_started: float = 0.0
    last_ocr_end: float = -1e9
    last_ocr_dur: float = 0.0
    pending_frame: np.ndarray | None = None  # frame being OCR'd right now
    # last OCR result, kept so late machine translations can be painted without OCR
    lines: list[Line] = field(default_factory=list)
    lines_frame: np.ndarray | None = None
    translated_version: int = -1
    # animated pages: identical OCR results back off and mask the animated cells
    last_sig: int | None = None
    backoff_ms: int = 0
    anim_mask: np.ndarray | None = None
    changed_accum: np.ndarray | None = None
    empty_streak: int = 0


def changed_cells(prev: np.ndarray | None, gray: np.ndarray) -> np.ndarray:
    """Bool grid (CELL x CELL cells) of where the picture changed."""
    h, w = gray.shape
    gh, gw = (h + CELL - 1) // CELL, (w + CELL - 1) // CELL
    if prev is None or prev.shape != gray.shape:
        return np.ones((gh, gw), bool)
    diff = (np.abs(gray.astype(np.int16) - prev.astype(np.int16)) > CHANGE_DIFF)
    pad = np.zeros((gh * CELL, gw * CELL), bool)
    pad[:h, :w] = diff
    counts = pad.reshape(gh, CELL, gw, CELL).sum(axis=(1, 3))
    return counts >= CELL_CHANGED_PIXELS


def result_signature(lines: list[Line]) -> int:
    return hash(frozenset((normalize(l.text), l.x0 // 8, l.y0 // 8, l.x1 // 8, l.y1 // 8)
                          for l in lines))


class UntranslatedLog:
    """Collects OCR'd Chinese that the dictionary could not translate (unique)."""

    def __init__(self, path: Path):
        self.path = path
        self.seen: set[str] = set()
        if path.exists():
            self.seen = set(path.read_text(encoding="utf-8").splitlines())

    def add(self, texts: list[str]) -> None:
        new = [t for t in texts if t not in self.seen and len(t) >= 2]
        if not new:
            return
        self.seen.update(new)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write("".join(t + "\n" for t in new))


class Controller(QObject):
    def __init__(self, cfg: Config, root: Path):
        super().__init__()
        self.cfg = cfg
        self.root = root
        self.enabled = True
        self.states: dict[int, WindowState] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
        self.sct = getattr(mss, "MSS", mss.mss)()
        self.own_pid = os.getpid()
        self.games = {g.lower() for g in cfg.game_exes}
        self.untranslated = UntranslatedLog(root / "logs" / "untranslated.txt")
        self.translator = self._make_translator()
        self.fx = FxRate(root / "data" / "fx_rate.json")
        self._load_items()
        if cfg.show_krw:
            self.fx.refresh_async()   # background, only if the saved rate is 12+ h old
        self.analyzer = Analyzer(self.translator, cfg.ocr_params())
        self.overlay = OverlayWindow(cfg.font_family, cfg.exclude_from_capture, cfg.debug)
        self.notify = lambda title, text: None  # set by the tray
        self.status = "대기 중"
        self.active = False
        self.active_since = 0.0
        self.last_active = time.monotonic()
        self._release_queued = False
        if win32.lower_own_priority():
            log.info("process priority lowered (below normal)")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(cfg.idle_tick_ms)

    # ---- dictionary / translation ----------------------------------------
    def _load_dictionary(self) -> Dictionary:
        return Dictionary.from_dir(self.root / "data" / "mappings",
                                   self.root / "data" / "user_dict.json",
                                   self.cfg.fuzzy_cutoff)

    def _make_translator(self) -> Translator:
        d = self._load_dictionary()
        t = make_translator(d, self.root, self.cfg.machine_translation, self.cfg.mt_model,
                            self.cfg.mt_thinking, self.cfg.mt_max_calls_per_hour,
                            self.cfg.mt_max_calls_per_day)
        log.info("dictionary %d entries, cache %d entries, machine translation: %s",
                 len(d), len(t.cache), "on" if t.machine_translation else "off")
        return t

    def reload_dictionary(self) -> None:
        """Re-read mappings/glossary/user_dict. A typo in a JSON file is reported and
        the old dictionary stays in use; queued translations are kept."""
        try:
            d = self._load_dictionary()
        except (OSError, ValueError) as e:
            log.error("dictionary reload failed: %s", e)
            self.notify("사전 오류", f"{e}\n이전 사전을 계속 사용합니다.")
            return
        self.translator.set_dictionary(d)
        for st in self.states.values():
            st.translated_version = -1  # re-translate the last OCR with the new data
        self.notify("사전", f"다시 불러왔습니다 ({len(d)}개 항목)")

    def retranslate_screen(self) -> None:
        """Forget the cached translations of what is on screen now and ask again."""
        texts: list[str] = []
        for st in self.states.values():
            texts += [l.text for l in st.lines] + [i.zh for i in st.items]
            st.items = []
            st.translated_version = -1
        n = self.translator.forget(texts)
        self.notify("다시 번역", f"캐시 {n}개를 지우고 다시 번역합니다.")

    def save_screen(self) -> None:
        """The overlay is invisible to screen capture (so OCR never reads it), which
        also hides it from screenshot tools. Save the client window with the current
        translations painted on instead."""
        def grab_and_save():
            states = [s for s in self.states.values() if s.lines]
            if not states:
                self.notify("화면 저장", "아직 번역된 화면이 없습니다. 완미 창을 먼저 띄워 주세요.")
                return
            out = self.root / "logs" / "screens"
            out.mkdir(parents=True, exist_ok=True)
            st = max(states, key=lambda s: s.rect.w * s.rect.h)
            frame = np.ascontiguousarray(self._grab(st.rect))
            h, w = frame.shape[:2]
            rgb = np.ascontiguousarray(frame[:, :, ::-1])
            img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
            p = QPainter(img)
            paint_items(p, draw_items(st.items, st.lines, w, 0, 0, h, self._display_text), 0, 0,
                        self.cfg.font_family)
            p.end()
            path = out / time.strftime("screen_%Y%m%d_%H%M%S.png")
            img.save(str(path))
            self.notify("화면 저장", f"{path.name} 저장됨 (logs\\screens)")
            os.startfile(out)  # noqa: S606

        QTimer.singleShot(300, grab_and_save)  # let the tray menu close first

    def set_gpu(self, on: bool) -> None:
        """Switch OCR between the light CPU model and GPU acceleration (DirectML:
        faster, but 2+ GB of RAM). Takes effect on the next OCR; remembered."""
        device = "gpu" if on else "cpu"
        if self.cfg.ocr_device == device:
            return
        self.cfg.ocr_device = device
        self.analyzer.ocr.params.device = device
        if not self._release_queued:
            self._release_queued = True
            self.executor.submit(self._do_release)
        self._save_setting("ocr_device", device)
        self.notify("OCR 모드", "GPU 가속 (빠름, 메모리 약 2GB)" if on
                    else "CPU (가벼움, 메모리 약 0.5GB)")

    def _load_items(self) -> None:
        """Official CS2 item names from the installed game, in the background. The
        game files are re-read only after a CS2 update, and never while it runs."""
        import threading

        from .items import load_or_build

        def work():
            try:
                allow = not win32.running_exes(self.games)
                names = load_or_build(self.root / "data" / "cs2_items.json", allow)
            except Exception:  # noqa: BLE001 (optional feature)
                log.exception("CS2 item names not loaded")
                return
            if names is not None:
                self.translator.items = names
                self.translator.version += 1   # re-translate what is on screen
            else:
                log.info("CS2 item names: CS2 install not found (feature off)")

        threading.Thread(target=work, name="cs2-items", daemon=True).start()

    def _display_text(self, ko: str) -> str:
        """What is drawn for a translation: yuan amounts get a won value."""
        if not getattr(self.cfg, "show_krw", False) or getattr(self, "fx", None) is None:
            return ko
        self.fx.refresh_async()   # returns at once unless the rate is due (12 h)
        return add_krw(ko, self.fx.rate)

    def set_show_krw(self, on: bool) -> None:
        self.cfg.show_krw = on
        self._save_setting("show_krw", on)

    def _save_setting(self, key: str, value) -> None:
        """Update one key in config.json, leaving the rest (and CLI overrides) alone."""
        import json

        path = self.root / "config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        data[key] = value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def import_cache(self) -> None:
        """Merge a translation_cache.json a friend shared into ours."""
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(None, "공유받은 번역 캐시 가져오기",
                                              str(Path.home() / "Downloads"),
                                              "번역 캐시 (*.json)")
        if not path:
            return
        try:
            added, updated, skipped = self.translator.cache.merge_file(Path(path))
        except ValueError as e:
            self.notify("가져오기 실패", str(e))
            return
        self.translator.version += 1  # repaint the screen with the new translations
        log.info("cache import %s: +%d new, %d updated, %d skipped", path, added, updated,
                 skipped)
        self.notify("번역 캐시 가져오기",
                    f"새 번역 {added}개, 갱신 {updated}개" + (f", 건너뜀 {skipped}개" if skipped else ""))

    def export_cache(self) -> None:
        """Save a copy of our cache to share with friends."""
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            None, "번역 캐시 내보내기",
            str(Path.home() / "Desktop" / time.strftime("translation_cache_%Y%m%d.json")),
            "번역 캐시 (*.json)")
        if not path:
            return
        n = self.translator.cache.export_to(Path(path))
        self.notify("번역 캐시 내보내기", f"{n}개 번역을 저장했습니다.\n{Path(path).name}")

    def clear_uncertain_cache(self) -> None:
        n = self.translator.cache.remove(uncertain_only=True)
        self.notify("번역 캐시", f"OCR이 불확실했던 번역 {n}개를 지웠습니다.")

    # ---- main loop ----------------------------------------------------
    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if not on:
            self.overlay.park()

    def tick(self) -> None:
        try:
            self._tick()
        except Exception:  # never let one bad frame kill the loop
            log.exception("tick failed")

    def _tick(self) -> None:
        now = time.monotonic()
        # 1. a game in front wins over every setting (--always included)
        if win32.foreground_exe().lower() in self.games:
            self._go_idle(now, "게임 중 · 일시 정지", game=True)
            return
        # 2. our own tray menu / dialogs took the focus: keep showing what we had
        #    (no capture: the menu may sit over the client)
        if win32.foreground_pid() == self.own_pid:
            return
        game_running = bool(win32.running_exes(self.games))
        ocr = self.analyzer.ocr
        ocr.force_cpu = game_running and not self.cfg.allow_gpu_while_game_running

        windows = win32.find_target_windows(self.cfg.target_exe, self.cfg.target_title,
                                            exclude_pid=self.own_pid,
                                            exclude_exes=self.games)
        alive = {w.hwnd for w in windows}
        for hwnd in list(self.states):
            if hwnd not in alive:
                del self.states[hwnd]
        if not windows:
            self._go_idle(now, "완미 클라이언트를 기다리는 중")
            return
        target_pids = {w.pid for w in windows}
        if not self.enabled or (self.cfg.require_foreground
                                 and win32.foreground_pid() not in target_pids):
            # client is in the background: whatever covers it is not ours to translate
            self._go_idle(now, "꺼짐" if not self.enabled else "완미 창이 활성화되지 않음")
            return

        if not self.active:
            self.active, self.active_since = True, now
        self.last_active = now
        if self.timer.interval() != self.cfg.tick_ms:
            self.timer.setInterval(self.cfg.tick_ms)

        draw: list[DrawItem] = []
        for w in windows:  # largest first
            st = self.states.get(w.hwnd)
            if st is None:
                st = self.states[w.hwnd] = WindowState(w.hwnd, w.rect, dirty_since=now)
            if (st.rect.w, st.rect.h) != (w.rect.w, w.rect.h):
                st.items, st.gray, st.anim_mask, st.last_sig = [], None, None, None
            st.rect = w.rect
            frame = self._grab(w.rect)
            self._update_window(st, frame, now, game_running, w is windows[0])
            draw += draw_items(st.items, st.lines, w.rect.w, w.rect.x, w.rect.y, w.rect.h,
                               self._display_text)
        n = sum(len(s.items) for s in self.states.values())
        pending = self.translator.pending_count()
        capped = getattr(self.translator, "capped", 0)
        if capped != getattr(self, "_seen_capped", 0):
            self._seen_capped = capped
            self.notify("번역 한도", "API 호출 한도에 도달해 잠시 사전/캐시만 씁니다.\n"
                        "한도가 풀리면 보고 있던 화면을 자동으로 다시 번역합니다.")
        self.status = (f"번역 중 · 창 {len(windows)}개 · 문구 {n}개"
                       + (f" · 번역 대기 {pending}개" if pending else "")
                       + f" · {self.translator.status} · OCR {ocr.device}")
        x0 = min(w.rect.x for w in windows)
        y0 = min(w.rect.y for w in windows)
        x1 = max(w.rect.right for w in windows)
        y1 = max(w.rect.bottom for w in windows)
        self.overlay.cover(QRect(x0, y0, x1 - x0, y1 - y0))
        if draw != self.overlay.items:
            log.debug("draw %d items, first=%s", len(draw),
                      (draw[0].x0, draw[0].y0, draw[0].ko) if draw else None)
        self.overlay.present(draw)

    def _go_idle(self, now: float, status: str, game: bool = False) -> None:
        """Nothing to translate: no window, slow polling, give memory back. A game in
        front releases the OCR engine (and its GPU memory) at once."""
        self.active = False
        self.status = status
        self.overlay.park()
        if self.timer.interval() != self.cfg.idle_tick_ms:
            self.timer.setInterval(self.cfg.idle_tick_ms)
        for st in self.states.values():
            # drop OCR work that hasn't started; a running one finishes, then release
            if st.future is not None and (st.future.done() or st.future.cancel()):
                st.future, st.pending_frame = None, None
            if st.last_sig is not None:
                # already read: an unchanged page needs no OCR on return (change
                # detection against st.gray catches anything that did change)
                st.dirty, st.from_invalidation = False, False
            else:
                st.dirty, st.dirty_since = True, now  # never read yet: read on return
        ocr = self.analyzer.ocr
        if ocr.loaded and not self._release_queued and (
                game or now - self.last_active > self.cfg.idle_release_s):
            self._release_queued = True
            self.executor.submit(self._do_release)  # FIFO: never mid-inference

    def _do_release(self) -> None:
        try:
            self.analyzer.ocr.release()
        finally:
            self._release_queued = False

    def _grab(self, r: win32.Rect) -> np.ndarray:
        shot = self.sct.grab({"left": r.x, "top": r.y, "width": r.w, "height": r.h})
        return np.asarray(shot)[:, :, :3]  # BGRA -> BGR view

    def _update_window(self, st: WindowState, frame: np.ndarray, now: float,
                       game_running: bool, main_window: bool) -> None:
        gray = to_gray(frame)

        # 1. drop translations whose text changed (a digit, a page switch...)
        # (only when our own drawing can't show up in the capture)
        own_pixels_visible = not self.cfg.exclude_from_capture or (
            self.overlay.isVisible() and not self.overlay.capture_excluded)
        if st.items and not own_pixels_visible:
            keep = [i for i in st.items if not signature_changed(gray, i)]
            if len(keep) != len(st.items):
                st.items = keep
                if not st.dirty:
                    st.dirty, st.dirty_since, st.from_invalidation = True, now, True
                st.last_change = now

        # 2. change detection on a cell grid, ignoring cells known to animate
        cells = changed_cells(st.gray, gray)
        st.gray = gray
        if st.changed_accum is None or st.changed_accum.shape != cells.shape:
            st.changed_accum = np.zeros_like(cells)
            st.anim_mask = None
        st.changed_accum |= cells
        if cells.mean() > 0.25:  # big change: page switch, resize
            st.anim_mask, st.backoff_ms = None, 0
        relevant = cells & ~st.anim_mask if st.anim_mask is not None else cells
        if relevant.any():
            st.last_change, st.backoff_ms = now, 0
            if not st.dirty or st.from_invalidation:
                st.dirty, st.dirty_since, st.from_invalidation = True, now, False

        # 3. collect a finished OCR
        if st.future is not None and st.future.done():
            fut, st.future = st.future, None
            st.last_ocr_end, st.last_ocr_dur = now, now - st.ocr_started
            try:
                result: Analysis = fut.result()
            except Exception:
                log.exception("OCR failed")
            else:
                self._take_result(st, result, gray, now)
            st.pending_frame = None

        # 3b. new machine translations arrived: repaint from the last OCR, no re-OCR
        if (st.translated_version != self.translator.version and st.lines
                and st.lines_frame is not None and st.future is None):
            v = self.translator.version
            result = translate_lines(st.lines_frame, st.lines, self.translator,
                                     prev_items=st.items)
            st.items = [i for i in result.items if not signature_changed(gray, i)]
            st.translated_version = v

        # 4. start OCR if needed
        if not st.dirty or st.future is not None:
            return
        settled = (now - st.last_change) * 1000 >= self.cfg.stable_ms
        overdue = (now - st.dirty_since) * 1000 >= self.cfg.max_wait_ms + st.backoff_ms
        spacing = st.last_ocr_end + st.last_ocr_dur
        if st.from_invalidation:
            spacing = max(spacing, st.last_ocr_end + INVALIDATION_SPACING_S)
        if st.empty_streak:
            spacing = max(spacing, st.last_ocr_end
                          + min(self.cfg.stable_ms * 2 ** st.empty_streak, 8000) / 1000)
        if not (settled or overdue) or now < spacing:
            return
        ocr = self.analyzer.ocr
        if not ocr.loaded and game_running and \
                (now - self.active_since) * 1000 < self.cfg.reload_dwell_ms:
            return  # a quick alt-tab mid-game shouldn't trigger a cold model load
        h, w = frame.shape[:2]
        if main_window and ocr.warm_shape != (h, w):
            self.executor.submit(ocr.warmup, h, w)  # queued before the real OCR
        st.dirty, st.from_invalidation = False, False
        st.ocr_size, st.ocr_started = (w, h), now
        st.pending_frame = frame.copy()
        st.changed_accum = np.zeros_like(st.changed_accum)
        st.future = self.executor.submit(self.analyzer.analyze, st.pending_frame,
                                         list(st.items))

    def _take_result(self, st: WindowState, result: Analysis, gray: np.ndarray,
                     now: float) -> None:
        if st.ocr_size != (gray.shape[1], gray.shape[0]):
            return
        has_text = any(has_cjk(l.text) for l in result.lines)
        st.empty_streak = 0 if has_text else min(st.empty_streak + 1, 6)
        sig = result_signature(result.lines)
        if sig == st.last_sig:
            # nothing new (an animation re-triggered OCR): back off, learn the mask
            st.backoff_ms = min(st.backoff_ms * 2 or self.cfg.max_wait_ms,
                                self.cfg.max_backoff_ms)
            st.anim_mask = st.changed_accum.copy() if st.anim_mask is None \
                else st.anim_mask | st.changed_accum
        else:
            st.last_sig, st.backoff_ms, st.anim_mask = sig, 0, None
            st.items = [i for i in result.items if not signature_changed(gray, i)]
            st.lines, st.lines_frame = result.lines, st.pending_frame
            st.translated_version = result.version
            if not self.translator.machine_translation:
                self.untranslated.add(result.untranslated)
            if self.cfg.dump_dir and st.pending_frame is not None:
                self._dump(st.pending_frame, result)
        log.debug("OCR hwnd=%x %dx%d %.0fms: %d lines, %d translated, %d untranslated%s",
                  st.hwnd, st.rect.w, st.rect.h, st.last_ocr_dur * 1000, len(result.lines),
                  len(result.items), len(result.untranslated),
                  f", unchanged (backoff {st.backoff_ms}ms)" if sig == st.last_sig
                  and st.backoff_ms else "")

    def _dump(self, frame: np.ndarray, result: Analysis) -> None:
        """Save the frame with translations painted on (debug aid)."""
        out = Path(self.cfg.dump_dir)
        out.mkdir(parents=True, exist_ok=True)
        h, w = frame.shape[:2]
        rgb = np.ascontiguousarray(frame[:, :, ::-1])
        img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        p = QPainter(img)
        paint_items(p, draw_items(result.items, result.lines, w, 0, 0, h, self._display_text), 0, 0,
                    self.cfg.font_family, True)
        p.end()
        img.save(str(out / time.strftime("frame_%H%M%S.png")))

    def shutdown(self) -> None:
        self.timer.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.translator.close()
        self.overlay.close()


def _tray_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(234, 74, 170))
    p.setPen(QColor(0, 0, 0, 0))
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QColor(255, 255, 255))
    f = QFont("Malgun Gothic")
    f.setPixelSize(40)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), 0x84, "한")  # AlignCenter
    p.end()
    return QIcon(pm)


def build_tray(app: QApplication, ctl: Controller) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(_tray_icon(), app)
    menu = QMenu()
    ctl.notify = lambda title, text: tray.showMessage(title, text,
                                                      QSystemTrayIcon.MessageIcon.Information,
                                                      4000)

    toggle = QAction("번역 표시", menu, checkable=True)
    toggle.setChecked(True)
    toggle.toggled.connect(ctl.set_enabled)
    menu.addAction(toggle)
    gpu = QAction("GPU 가속 (빠름 · 메모리 약 2GB 사용)", menu, checkable=True)
    gpu.setChecked(ctl.cfg.ocr_device == "gpu")
    gpu.toggled.connect(ctl.set_gpu)
    menu.addAction(gpu)
    krw = QAction("위안(¥) 금액에 원화 환산 표시", menu, checkable=True)
    krw.setChecked(ctl.cfg.show_krw)
    krw.toggled.connect(ctl.set_show_krw)
    menu.addAction(krw)
    menu.addSeparator()

    def open_path(p: Path):
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
        os.startfile(p)  # noqa: S606 (opens in the user's default editor)

    def add(label, slot):
        a = QAction(label, menu)
        a.triggered.connect(slot)
        menu.addAction(a)

    add("번역된 화면 저장(스크린샷)", ctl.save_screen)
    add("이 화면 다시 번역", ctl.retranslate_screen)
    add("불확실한 번역 캐시 비우기", ctl.clear_uncertain_cache)
    menu.addSeparator()
    add("번역 캐시 가져오기(공유받은 파일 합치기)", ctl.import_cache)
    add("번역 캐시 내보내기(공유용)", ctl.export_cache)
    menu.addSeparator()
    add("번역 안 된 문장 보기", lambda: open_path(ctl.untranslated.path))
    add("사용자 사전 열기", lambda: open_path(ctl.root / "data" / "user_dict.json"))
    add("용어집 열기", lambda: open_path(ctl.root / "data" / "glossary.json"))
    add("사전 다시 불러오기", ctl.reload_dictionary)
    menu.addSeparator()
    add("종료", app.quit)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: toggle.toggle()
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    def refresh_tip():
        tray.setToolTip(f"완미 한글 오버레이 — {ctl.status}"[:127])

    t = QTimer(tray)
    t.timeout.connect(refresh_tip)
    t.start(1000)
    refresh_tip()
    tray.show()
    return tray
