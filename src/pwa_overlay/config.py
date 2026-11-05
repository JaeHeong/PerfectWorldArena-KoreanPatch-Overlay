from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_TARGET = "完美世界竞技平台.exe"


def app_root() -> Path:
    """Folder holding data/ and logs/: next to the exe when frozen, repo root in dev."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass
class Config:
    target_exe: str = DEFAULT_TARGET
    target_title: str = ""          # optional substring filter on window title
    tick_ms: int = 120              # window tracking / change detection interval
    stable_ms: int = 250            # wait for the screen to settle before OCR
    max_wait_ms: int = 1500         # ...but OCR anyway if it keeps changing (animations)
    ocr_scale: float = 1.0          # upscale frames before OCR (small text)
    ocr_box_thresh: float = 0.5
    ocr_unclip_ratio: float = 1.6
    min_ocr_score: float = 0.5
    # cpu (default): PP-OCRv6 small, ~0.46 GB RAM, ~1.3 s per new screen (0.4 s when
    # mostly unchanged). gpu: PP-OCRv6 medium on DirectML, ~0.45 s but 2-4 GB of RAM.
    ocr_device: str = "cpu"         # cpu | gpu | auto
    ocr_model: str = "medium"       # model used on the GPU (CPU always uses small)
    ocr_cpu_threads: int = 4
    idle_release_s: int = 60        # free OCR memory (incl. GPU) after this long unused
    idle_tick_ms: int = 500         # slower polling while the client isn't active
    game_exes: list = field(default_factory=lambda: ["cs2.exe"])  # pause + release OCR
    # while a game process is running (even minimized / in the background) OCR uses the
    # small CPU model with 2 threads instead of loading the GPU model
    allow_gpu_while_game_running: bool = False
    reload_dwell_ms: int = 700      # launcher must stay in front this long before a cold load
    max_backoff_ms: int = 6000      # animated pages: re-OCR at most this rarely when unchanged
    fuzzy_cutoff: float = 86.0
    font_family: str = "Malgun Gothic"
    exclude_from_capture: bool = True
    require_foreground: bool = True # hide while the client is not the active window
    machine_translation: bool = True  # Claude, when ANTHROPIC_API_KEY is set
    mt_model: str = "claude-haiku-5-5"
    # disabled: an A/B on real screens showed adaptive was no better, a bit slower,
    # and once blanked a real UI line
    mt_thinking: str = "disabled"   # disabled | adaptive
    mt_max_calls_per_hour: int = 300
    mt_max_calls_per_day: int = 1500
    debug: bool = False
    dump_dir: str = ""              # save annotated frames here after each OCR

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Defaults overlaid with config.json. A broken file never stops the app:
        it is ignored (and reported in cfg.load_error, logged once logging is up)."""
        cfg = cls()
        cfg.load_error = ""
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                cfg.load_error = f"{path.name} ignored ({e}); using defaults"
                return cfg
            names = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in names:
                    setattr(cfg, k, v)
        return cfg

    def ocr_params(self):
        from .analyzer import OcrParams

        return OcrParams(self.ocr_scale, self.ocr_box_thresh, self.ocr_unclip_ratio,
                         self.min_ocr_score, self.ocr_device, self.ocr_model,
                         self.ocr_cpu_threads)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                        encoding="utf-8")
