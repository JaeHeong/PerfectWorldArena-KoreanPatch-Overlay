from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys

MUTEX_NAME = "Local\\PWAKoreanOverlay.SingleInstance"


def _already_running() -> bool:
    """One overlay at a time: a second launch (double-clicked shortcut) just exits."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    _already_running.handle = handle  # keep it alive for the life of the process
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def _message_box(text: str, title: str = "완미 한글 오버레이") -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION


def _selftest(image: str, root, cfg) -> int:
    """Run OCR + the full translation chain on one image and log the result (used to
    check a built exe end to end, GPU and API included)."""
    import time

    import cv2
    import numpy as np

    from .analyzer import Analyzer
    from .dictionary import Dictionary
    from .translator import make_translator

    log = logging.getLogger("pwa_overlay.selftest")
    frame = cv2.imdecode(np.fromfile(image, np.uint8), cv2.IMREAD_COLOR)
    d = Dictionary.from_dir(root / "data" / "mappings", root / "data" / "user_dict.json",
                            cfg.fuzzy_cutoff)
    t = make_translator(d, root, cfg.machine_translation, cfg.mt_model, cfg.mt_thinking,
                        cfg.mt_max_calls_per_hour, cfg.mt_max_calls_per_day)
    analyzer = Analyzer(t, cfg.ocr_params())
    t0 = time.perf_counter()
    first = analyzer.analyze(frame)
    log.info("selftest OCR on %s: %d lines, %.0f ms", analyzer.ocr.device, len(first.lines),
             (time.perf_counter() - t0) * 1000)
    deadline = time.monotonic() + 30
    while t.pending_count() and time.monotonic() < deadline:
        time.sleep(0.1)
    from .analyzer import translate_lines
    final = translate_lines(frame, first.lines, t)
    log.info("selftest translated %d, untranslated %d, translator: %s",
             len(final.items), len(final.untranslated), t.status)
    for i in final.items[:8]:
        log.info("selftest  %s -> %s (%s)", i.zh, i.ko, i.method)
    t.close()
    return 0 if final.items else 1


def main() -> int:
    from .config import Config, app_root

    root = app_root()
    cfg = Config.load(root / "config.json")

    ap = argparse.ArgumentParser(prog="pwa-overlay",
                                 description="완미세계 경기 플랫폼 한국어 OCR 오버레이")
    ap.add_argument("--target", help=f"target process exe name (default: {cfg.target_exe})")
    ap.add_argument("--title", help="only windows whose title contains this text")
    ap.add_argument("--debug", action="store_true", help="outline boxes, verbose log")
    ap.add_argument("--dump", metavar="DIR", help="save annotated frames after each OCR")
    ap.add_argument("--no-exclude", action="store_true",
                    help="do not hide the overlay from screen capture")
    ap.add_argument("--always", action="store_true",
                    help="show even when the client is not the active window")
    ap.add_argument("--selftest", metavar="IMAGE",
                    help="OCR + translate one image, log the result and exit")
    args = ap.parse_args()
    if args.always:
        cfg.require_foreground = False
    if args.target:
        cfg.target_exe = args.target
    if args.title:
        cfg.target_title = args.title
    if args.debug:
        cfg.debug = True
    if args.dump:
        cfg.dump_dir = args.dump
    if args.no_exclude:
        cfg.exclude_from_capture = False

    (root / "logs").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(root / "logs" / "overlay.log", encoding="utf-8")]
    if sys.stderr is not None:  # the windowed exe has no console
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    # An exception inside a Qt slot would abort the whole process (0xC0000409):
    # log it instead and keep running.
    def _log_uncaught(exc_type, exc, tb):
        logging.getLogger("pwa_overlay").error("uncaught exception",
                                               exc_info=(exc_type, exc, tb))
    sys.excepthook = _log_uncaught
    if getattr(cfg, "load_error", ""):
        logging.getLogger("pwa_overlay").warning(cfg.load_error)

    if args.selftest:
        return _selftest(args.selftest, root, cfg)
    if _already_running():
        _message_box("완미 한글 오버레이가 이미 실행 중입니다.\n작업 표시줄 트레이의 '한' 아이콘을 확인하세요.")
        return 0

    # Work in physical pixels end to end: window rects, captures and overlay agree.
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"

    from PyQt6.QtWidgets import QApplication

    from .app import Controller, build_tray

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    logging.getLogger(__name__).info("target=%s title=%r root=%s",
                                     cfg.target_exe, cfg.target_title, root)
    ctl = Controller(cfg, root)
    tray = build_tray(app, ctl)  # noqa: F841 (keep a reference)
    code = app.exec()
    ctl.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
