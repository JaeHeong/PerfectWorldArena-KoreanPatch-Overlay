"""Zip the built app for friends, leaving out anything private or machine-specific.

    uv run python packaging/make_share_zip.py [output.zip]

Excluded: logs/ (log lines, saved screenshots that can show nicknames/Steam IDs),
data/translation_cache.json (this PC's own cache: unreviewed, may hold status
messages or chat), data/translation_failures.json (this PC's retry state),
config.json (this PC's settings).
Included: the exe, OCR models, mappings, glossary, user dictionary and the
reviewed, nickname-free data/translation_cache.shared.json (run
packaging/publish_cache.py first), which the app merges into its cache at startup.
The API key is never in the folder (it's read from the Windows environment).
"""

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dist" / "PWAKoreanOverlay"
EXCLUDE_DIRS = {"logs"}
EXCLUDE_FILES = {Path("data") / "translation_failures.json", Path("config.json"),
                 Path("data") / "translation_cache.json"}


def desktop() -> Path:
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)  # CSIDL_DESKTOPDIRECTORY
        if buf.value:
            return Path(buf.value)
    except Exception:  # noqa: BLE001
        pass
    return Path.home() / "Desktop"


def main() -> int:
    if not (APP / "PWAKoreanOverlay.exe").exists():
        print("build first: packaging\\build.ps1")
        return 1
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else desktop() / "완미한글오버레이.zip"
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(APP.rglob("*")):
            rel = path.relative_to(APP)
            if path.is_dir() or rel.parts[0] in EXCLUDE_DIRS or rel in EXCLUDE_FILES:
                continue
            if key and path.suffix in (".json", ".txt", ".log") and \
                    key in path.read_text(encoding="utf-8", errors="ignore"):
                raise SystemExit(f"refusing to zip {rel}: it contains the API key")
            if rel.parts[0] == "data":
                continue  # data comes from the repository below, not this PC's copy
            z.write(path, Path("완미한글오버레이") / rel)
            n += 1
        # reviewed data from the repository: mappings, glossary, user dictionary and
        # the nickname-free shared cache (merged into the local cache on first start)
        data = ROOT / "data"
        files = sorted((data / "mappings").glob("*.json")) + [
            data / "glossary.json", data / "user_dict.json", data / "translation_cache.shared.json", data / "LICENSE.md"]
        for path in files:
            if not path.exists():
                continue
            if key and key in path.read_text(encoding="utf-8", errors="ignore"):
                raise SystemExit(f"refusing to zip {path.name}: it contains the API key")
            z.write(path, Path("완미한글오버레이") / path.relative_to(ROOT))
            n += 1
    print(f"{out}  ({n} files, {out.stat().st_size / 2**20:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
