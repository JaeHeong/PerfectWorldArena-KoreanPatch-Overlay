"""Refresh data/translation_cache.shared.json (the cache that goes to git) from a
local cache, leaving out anything personal.

    uv run python packaging/publish_cache.py [local cache]

Default source: the exe's cache (dist/PWAKoreanOverlay/data/translation_cache.json),
else data/translation_cache.json. Left out: "keep as is" entries (other players'
nicknames) and entries listed in data/cache_blocklist.txt (one Chinese source per
line: status messages, chat, clan names you don't want published). Review the git
diff before committing.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pwa_overlay.translator import SHARED_CACHE_NAME, TranslationCache, cache_key  # noqa: E402


def main() -> int:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        src = ROOT / "dist" / "PWAKoreanOverlay" / "data" / "translation_cache.json"
        if not src.exists():
            src = ROOT / "data" / "translation_cache.json"
    data = TranslationCache(src).shareable()
    block_file = ROOT / "data" / "cache_blocklist.txt"
    blocked = set()
    if block_file.exists():
        blocked = {cache_key(line.strip()) for line in
                   block_file.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.startswith("#")}
    data = {k: e for k, e in data.items() if k not in blocked}
    out = ROOT / "data" / SHARED_CACHE_NAME
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"{out.relative_to(ROOT)}: {len(data)} entries "
          f"(+{len(set(data) - set(old))} / -{len(set(old) - set(data))}) from {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
