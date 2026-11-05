"""Refresh data/translation_cache.shared.json (the cache that goes to git) from a
local cache, leaving out anything personal.

    uv run python packaging/publish_cache.py [local cache] [--min-seen 5]
                                             [--include-candidates]

Default source: the exe's cache (dist/PWAKoreanOverlay/data/translation_cache.json),
else data/translation_cache.json.

Each shareable line (not "keep as is", not marked by Claude as written by players)
ends up in one of three groups:
  shared     already in the shared cache (reviewed before) or approved in
             data/cache_allowlist.txt: kept, with the latest translation
  auto       Claude called it UI text ("kind": "ui") and it was seen on at least
             --min-seen separate occasions: added
  candidate  everything else (seen fewer times, or cached before lines had a kind):
             NOT added; listed in build/cache_review.tsv for review. Approve one by
             adding its Chinese source to data/cache_allowlist.txt.
Lines in data/cache_blocklist.txt (status messages, chat, clan names) are always
left out. This PC's sighting counts are not published. Review the git diff (and
the auto group in the review file) before committing.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pwa_overlay.translator import SHARED_CACHE_NAME, TranslationCache, cache_key  # noqa: E402


def read_list(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {cache_key(line.strip()) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?")
    ap.add_argument("--min-seen", type=int, default=5)
    ap.add_argument("--include-candidates", action="store_true",
                    help="after reviewing build/cache_review.tsv (and dropping or "
                         "blocklisting what must not be shared): add the candidates too")
    args = ap.parse_args()
    if args.source:
        src = Path(args.source)
    else:
        src = ROOT / "dist" / "PWAKoreanOverlay" / "data" / "translation_cache.json"
        if not src.exists():
            src = ROOT / "data" / "translation_cache.json"
    cache = TranslationCache(src)
    share = cache.shareable()
    blocked = read_list(ROOT / "data" / "cache_blocklist.txt")
    allowed = read_list(ROOT / "data" / "cache_allowlist.txt")
    out = ROOT / "data" / SHARED_CACHE_NAME
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}

    result: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {"auto": [], "candidate": []}
    for k, e in share.items():
        if k in blocked:
            continue
        if k in old or k in allowed:
            result[k] = TranslationCache.public_copy(e)
        elif e.get("kind") == "ui" and int(e.get("seen", 0)) >= args.min_seen:
            result[k] = TranslationCache.public_copy(e)
            groups["auto"].append(e)
        else:
            groups["candidate"].append(e)
            if args.include_candidates:
                result[k] = TranslationCache.public_copy(e)
    # reviewed lines this PC no longer has (another PC's cache) stay; lines this PC
    # now treats as not shareable (KEEP, user text, invalid) or blocks are dropped
    for k, e in old.items():
        if k not in result and k not in blocked and k not in cache.data:
            result[k] = e
    excluded = sum(1 for k, e in cache.items() if k not in share)

    out.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    review = ROOT / "build" / "cache_review.tsv"
    review.parent.mkdir(exist_ok=True)
    with review.open("w", encoding="utf-8") as f:
        f.write("group\tseen\tkind\tzh\tko\n")
        for g in ("auto", "candidate"):
            for e in sorted(groups[g], key=lambda e: (-int(e.get("seen", 0)), e["zh"])):
                f.write(f"{g}\t{e.get('seen', 0)}\t{e.get('kind', '-')}\t{e['zh']}\t{e['ko']}\n")
    print(f"{out.relative_to(ROOT)}: {len(result)} entries "
          f"(+{len(set(result) - set(old))} / -{len(set(old) - set(result))}) from {src}")
    print(f"auto {len(groups['auto'])}, candidates {len(groups['candidate'])} "
          f"(review: {review.relative_to(ROOT)}), not shareable {excluded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
