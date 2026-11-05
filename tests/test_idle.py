"""Behaviour that protects a running game (no real Qt window needed)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np

from pwa_overlay import app as app_mod
from pwa_overlay import win32
from pwa_overlay.analyzer import Analysis
from pwa_overlay.config import Config


class FakeTimer:
    def __init__(self, ms):
        self.ms = ms

    def interval(self):
        return self.ms

    def setInterval(self, ms):  # noqa: N802 (Qt naming)
        self.ms = ms


class FakeOverlay:
    def __init__(self):
        self.visible = False
        self.items = []
        self.capture_excluded = True

    def park(self):
        self.visible, self.items = False, []

    def present(self, items):
        self.items, self.visible = items, bool(items)

    def cover(self, rect):
        pass

    def isVisible(self):  # noqa: N802
        return self.visible


class FakeOcr:
    def __init__(self):
        self.loaded = True
        self.releases = 0
        self.force_cpu = False
        self.device = "gpu/medium"
        self.warm_shape = (40, 60)

    def release(self):
        self.loaded = False
        self.releases += 1

    def warmup(self, h, w):
        self.warm_shape = (h, w)


def make_controller(**cfg):
    ctl = app_mod.Controller.__new__(app_mod.Controller)  # skip Qt/window setup
    ctl.cfg = Config(**cfg)
    ctl.games = {g.lower() for g in ctl.cfg.game_exes}
    ctl.overlay = FakeOverlay()
    ctl.timer = FakeTimer(ctl.cfg.tick_ms)
    ctl.submitted = []
    ctl.analyzer = SimpleNamespace(ocr=FakeOcr(),
                                   analyze=lambda frame, prev: ctl.submitted.append(1) or
                                   Analysis([], [], [], 0))
    ctl.translator = SimpleNamespace(version=0, machine_translation=True, status="",
                                     pending_count=lambda: 0)
    ctl.states = {}
    ctl.executor = ThreadPoolExecutor(max_workers=1)
    ctl.last_active = time.monotonic()
    ctl.active = False
    ctl.active_since = 0.0
    ctl._release_queued = False
    ctl.own_pid = 0
    ctl.enabled = True
    ctl.status = ""
    return ctl


def settle(ctl):
    ctl.executor.submit(lambda: None).result(timeout=5)


def test_game_in_front_pauses_everything_even_with_always(monkeypatch):
    monkeypatch.setattr(win32, "foreground_exe", lambda: "cs2.exe")
    monkeypatch.setattr(win32, "find_target_windows",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked")))
    ctl = make_controller(require_foreground=False)
    ctl._grab = lambda r: (_ for _ in ()).throw(AssertionError("captured"))
    ctl.overlay.visible = True
    ctl._tick()
    settle(ctl)
    assert not ctl.overlay.visible                       # no topmost window over the game
    assert not ctl.analyzer.ocr.loaded                   # GPU memory given back
    assert ctl.timer.interval() == ctl.cfg.idle_tick_ms  # slow polling
    assert "게임" in ctl.status


def test_finished_ocr_does_not_block_release(monkeypatch):
    monkeypatch.setattr(win32, "foreground_exe", lambda: "cs2.exe")
    ctl = make_controller()
    done = ctl.executor.submit(lambda: Analysis([], [], [], 0))
    done.result()
    ctl.states = {1: app_mod.WindowState(1, win32.Rect(0, 0, 10, 10), future=done)}
    ctl._go_idle(time.monotonic(), "x", game=True)
    settle(ctl)
    assert ctl.states[1].future is None
    assert not ctl.analyzer.ocr.loaded


def test_running_ocr_finishes_then_one_release(monkeypatch):
    ctl = make_controller()
    gate = threading.Event()
    running = ctl.executor.submit(gate.wait)
    ctl.states = {1: app_mod.WindowState(1, win32.Rect(0, 0, 10, 10), future=running)}
    for _ in range(5):
        ctl._go_idle(time.monotonic(), "x", game=True)
    assert ctl.analyzer.ocr.loaded       # not released mid-inference...
    gate.set()
    settle(ctl)
    assert not ctl.analyzer.ocr.loaded   # ...but right after it
    assert ctl.analyzer.ocr.releases == 1


def test_other_app_keeps_ocr_until_idle_timeout():
    ctl = make_controller(idle_release_s=60)
    ctl._go_idle(time.monotonic(), "x")
    settle(ctl)
    assert ctl.analyzer.ocr.loaded            # quick alt-tab: keep it warm
    ctl._go_idle(time.monotonic() + 61, "x")  # a minute later
    settle(ctl)
    assert not ctl.analyzer.ocr.loaded


def test_returning_to_an_unchanged_page_runs_no_ocr():
    ctl = make_controller()
    frame = np.full((40, 60, 3), 30, np.uint8)
    st = app_mod.WindowState(1, win32.Rect(0, 0, 60, 40))
    st.gray = app_mod.to_gray(frame)
    st.last_sig = 123            # this page has been read before
    ctl.states = {1: st}
    ctl._go_idle(time.monotonic(), "x")
    t = time.monotonic() + 5
    ctl.active_since = t - 5
    for k in range(5):
        ctl._update_window(st, frame, t + k * 0.2, game_running=False, main_window=True)
    settle(ctl)
    assert ctl.submitted == []


def test_running_game_switches_ocr_to_cpu(monkeypatch):
    monkeypatch.setattr(win32, "foreground_exe", lambda: "explorer.exe")
    monkeypatch.setattr(win32, "running_exes", lambda names: {"cs2.exe"})
    monkeypatch.setattr(win32, "find_target_windows", lambda *a, **k: [])
    ctl = make_controller()
    ctl._tick()
    assert ctl.analyzer.ocr.force_cpu
    monkeypatch.setattr(win32, "running_exes", lambda names: set())
    ctl._tick()
    assert not ctl.analyzer.ocr.force_cpu


def test_gpu_toggle_switches_engine_and_is_remembered(tmp_path):
    import json
    ctl = make_controller()
    ctl.root = tmp_path
    ctl.notify = lambda *a: None
    ctl.analyzer.ocr.params = SimpleNamespace(device="cpu")
    (tmp_path / "config.json").write_text('{"tick_ms": 100}', encoding="utf-8")
    ctl.set_gpu(True)
    settle(ctl)
    assert ctl.analyzer.ocr.params.device == "gpu"
    assert not ctl.analyzer.ocr.loaded        # reloads with the new device on next OCR
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == \
        {"tick_ms": 100, "ocr_device": "gpu"}  # other settings kept
