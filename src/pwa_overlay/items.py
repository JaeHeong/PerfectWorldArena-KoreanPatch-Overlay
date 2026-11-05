"""Official Korean names for CS2 items, read from the player's own CS2 install.

Market names on the platform look like "熊刀（★）| 多普勒 (久经沙场)". CS2 ships the
same names in every language (resource/csgo_schinese.txt, csgo_koreana.txt inside
pak01_dir.vpk), keyed by the same tokens, so pairing them gives the names Korean
players know: "우르수스 칼 (★) | 도플러 (현장에서 쓰인)".

Nothing from Valve is bundled: the pairs are built on this PC from the installed
game (only after a game update, never while CS2 runs) and kept in
data/cs2_items.json. Without CS2 installed the feature simply stays off.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_VERSION = 1
_TOK = re.compile(r'^\s*"([^"]+)"\s+"((?:[^"\\]|\\.)*)"', re.M)
_CJK = re.compile(r"[一-鿿]")
# "weapon | skin": OCR often reads the bar as l, I or 1 when it is glued to the name
_GLUED = re.compile(r"(?<=[0-9A-Za-z一-鿿）)★])\s*[Il1]\s*(?=[一-鿿])")
# ...or not read at all: "M4A4 破浪狂飙" (only after a known weapon, see _split)
_SPACED = re.compile(r"(?<=[0-9A-Za-z）)★])\s+(?=[一-鿿])")
_PAREN = re.compile(r"^\s*[（(]\s*(?P<w>[^（()）]+?)\s*[）)]?\s*$")
_WEAR_TAIL = re.compile(r"^(?P<skin>.+?)\s*[（(]\s*(?P<wear>[^（()）]+?)\s*[）)]?\s*$")
_STAR = re.compile(r"\s*[（(]?\s*★\s*[）)]?\s*")
_STATTRAK = re.compile(r"^\s*StatTrak\S*\s*", re.I)
_ELLIPSIS = re.compile(r"[.…。]+$")


def _key(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def parse_tokens(text: str) -> dict[str, str]:
    return {k.lower(): v.replace('\\"', '"') for k, v in _TOK.findall(text)}


def build_tables(zh: dict[str, str], ko: dict[str, str]) -> dict[str, dict[str, str]]:
    """Token-paired name tables: left = what comes before "|" (weapons, gloves,
    item types), right = skins, stickers, patches, graffiti, wear = condition."""
    left: dict[str, str] = {}
    right: dict[str, str] = {}
    wear: dict[str, str] = {}

    def put(table, z, k):
        z, k = z.strip(), k.strip()
        if not z or not k or len(z) > 40 or "\\n" in z or _CJK.search(k):
            return
        table.setdefault(_key(z), k)

    for tok, z in zh.items():
        k = ko.get(tok)
        if k is None:
            continue
        if tok.startswith("sfui_invtooltip_wear_amount_"):
            put(wear, z, k)
        elif (tok.startswith(("sfui_wpnhud_", "csgo_wearable_", "csgo_tool_", "csgo_type_"))
              and not tok.endswith("_desc")) or tok in (
                "button_sticker", "button_spray", "button_musickit", "type_customplayer"):
            put(left, z, k)
        elif (tok.startswith("paintkit_") and tok.endswith("_tag")) or (
                tok.startswith(("stickerkit_", "patchkit_", "spraykit_"))
                and not tok.endswith("_desc")):
            put(right, z, k)
    return {"left": left, "right": right, "wear": wear}


def _near(table: dict[str, str], key: str) -> str | None:
    """OCR slips: one wrong character in a name of 3+ characters, if unambiguous."""
    if len(key) < 3:
        return None
    hits = [v for k, v in table.items()
            if len(k) == len(key) and sum(a != b for a, b in zip(k, key)) == 1]
    return hits[0] if len(hits) == 1 else None


class ItemNames:
    def __init__(self, tables: dict[str, dict[str, str]]):
        self.left = tables.get("left", {})
        self.right = tables.get("right", {})
        self.wear = tables.get("wear", {})

    def __len__(self) -> int:
        return len(self.left) + len(self.right) + len(self.wear)

    def _left(self, s: str) -> str | None:
        k = _key(s)
        if k in self.left:
            return self.left[k]
        if not _CJK.search(s):
            return s.strip()          # AK-47, M4A4: already readable
        return _near(self.left, k)

    def _wear(self, s: str) -> str | None:
        k = _key(_ELLIPSIS.sub("", s))
        if k in self.wear:
            return self.wear[k]
        if _ELLIPSIS.search(s) or len(k) < 4:   # cut off on screen: "(久经…"
            hits = {v for w, v in self.wear.items() if w.startswith(k)} if k else set()
            if len(hits) == 1:
                return hits.pop()
        return _near(self.wear, k)

    def _split(self, s: str) -> tuple[str, str] | None:
        """(left, right) of "weapon | skin". A real bar wins; a bar misread as l/I/1
        counts only where the part before it is a known weapon."""
        bar = re.search(r"\s*[|｜丨Ⅰ]\s*", s)
        if bar:
            left, right = s[:bar.start()], s[bar.end():]
            return (left, right) if _CJK.search(right) else None
        for m in list(_GLUED.finditer(s)) + list(_SPACED.finditer(s)):
            left = _STAR.sub("", _STATTRAK.sub("", s[:m.start()])).strip()
            if _key(left) in self.left:
                return s[:m.start()], s[m.end():]
        return None

    def lookup(self, text: str) -> str | None:
        """Korean for a CS2 item name line, or None if it isn't one we know."""
        s = text.strip()
        if not s or not _CJK.search(s):
            return None
        k = _key(s)
        if k in self.wear:
            return self.wear[k]
        if k in self.left and _CJK.search(s):
            return self.left[k]
        bare = _STAR.sub("", s).strip()   # "运动手套（★）" on its own line
        if "★" in s and _key(bare) in self.left:
            return f"{self.left[_key(bare)]} (★)"
        p = _PAREN.match(s)   # a condition on its own line: "(久经沙场)", "(久经…"
        if p:
            w = self._wear(p.group("w"))
            return f"({w})" if w else None
        split = self._split(s)
        if split is None:
            return None
        left, right = split
        stattrak = _STATTRAK.match(left)
        left = _STATTRAK.sub("", left)
        star = "★" in left
        left = _STAR.sub("", left).strip()
        left_ko = self._left(left) if left else None
        if left_ko is None:
            return None
        wear_ko = ""
        skin = right
        w = _WEAR_TAIL.match(right)
        if w:
            wk = self._wear(w.group("wear"))
            if wk is not None:
                skin, wear_ko = w.group("skin"), f" ({wk})"
        skin = _ELLIPSIS.sub("", skin).strip()
        skin_ko = self.right.get(_key(skin)) or _near(self.right, _key(skin))
        if skin_ko is None:
            return None
        return (f"{'StatTrak™ ' if stattrak else ''}{left_ko}{' (★)' if star else ''}"
                f" | {skin_ko}{wear_ko}")

    def glossary_terms(self) -> dict[str, str]:
        """Weapon, glove and condition names worth giving Claude as glossary."""
        return {k: v for t in (self.left, self.wear) for k, v in t.items()
                if _CJK.search(k) and len(k) >= 2}


# ---- building from the installed game ---------------------------------------------
def find_cs2_vpk() -> Path | None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return None
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
            libs.append(Path(p.replace("\\\\", "\\")))
    except OSError:
        pass
    for lib in libs:
        vpk = (lib / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game"
               / "csgo" / "pak01_dir.vpk")
        if vpk.exists():
            return vpk
    return None


def load_or_build(cache_path: Path, allow_build: bool = True) -> ItemNames | None:
    """The saved tables, rebuilt from CS2 when the game was updated (and allowed:
    the caller passes allow_build=False while CS2 is running)."""
    saved = None
    try:
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        if saved.get("version") != CACHE_VERSION:
            saved = None
    except (OSError, ValueError):
        pass
    vpk_path = find_cs2_vpk()
    stamp = None
    if vpk_path is not None:
        st = vpk_path.stat()
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
    if saved and (stamp is None or saved.get("stamp") == stamp or not allow_build):
        return ItemNames(saved["tables"])
    if vpk_path is None or not allow_build:
        return None
    try:
        import vpk

        pak = vpk.open(str(vpk_path))
        zh = parse_tokens(pak.get_file("resource/csgo_schinese.txt").read()
                          .decode("utf-8-sig", errors="replace"))
        ko = parse_tokens(pak.get_file("resource/csgo_koreana.txt").read()
                          .decode("utf-8-sig", errors="replace"))
    except Exception as e:  # noqa: BLE001 (format change, missing file: stay off)
        log.warning("CS2 item names not loaded: %s", e)
        return ItemNames(saved["tables"]) if saved else None
    tables = build_tables(zh, ko)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"version": CACHE_VERSION, "stamp": stamp,
                                      "tables": tables}, ensure_ascii=False), encoding="utf-8")
    names = ItemNames(tables)
    log.info("CS2 item names: %d (weapons %d, skins/stickers %d, conditions %d)",
             len(names), len(names.left), len(names.right), len(names.wear))
    return names
