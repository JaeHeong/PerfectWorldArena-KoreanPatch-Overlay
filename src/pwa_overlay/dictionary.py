"""zh -> ko lookup built from the old patch's mapping JSON plus a user dictionary.

Lookup order for an OCR'd string:
  1. exact match on a normalized key
  2. template match (entries with ${var} placeholders)
  3. fuzzy match (OCR often gets a character or two wrong) - never across a
     different number or a different negation (不/未/无...), which would flip meaning
  4. phrase substitution: replace known phrases inside the string; accepted only
     if no Chinese characters remain afterwards

The glossary sent to Claude comes only from curated sources (data/glossary.json and
the user dictionary), never from the bulk mappings, whose entries are often
page-specific labels.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

log = logging.getLogger(__name__)

CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

_PUNCT = str.maketrans(
    {
        "，": ",", "。": ".", "！": "!", "？": "?", "：": ":", "；": ";",
        "（": "(", "）": ")", "【": "[", "】": "]", "「": '"', "」": '"',
        "“": '"', "”": '"', "‘": "'", "’": "'", "、": ",", "～": "~",
        "—": "-", "－": "-", "／": "/", "　": " ", "…": "...", "·": ".", "•": ".",
    }
)
_WS_RE = re.compile(r"\s+")
_VAR_RE = re.compile(r"\$\{(\w+)\}")
_DIGIT_FOLD = str.maketrans({"S": "5", "s": "5", "I": "1", "l": "1", "O": "0", "o": "0"})
_NEGATION = set("不未无没非否勿别禁免")
_NAME_SLOT_BEFORE = ("玩家", "用户", "队内", "转交给", "队友", "好友")


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def soft_normalize(text: str) -> str:
    """Half-width punctuation and ASCII, case and spaces kept."""
    out = []
    for ch in text.translate(_PUNCT):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:  # full-width ASCII block
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return "".join(out)


def normalize(text: str) -> str:
    """Canonical form used for matching: half-width punctuation, no whitespace,
    lower-case latin, full-width alphanumerics folded to ASCII."""
    return _WS_RE.sub("", soft_normalize(text)).lower()


def _digit_runs(text: str) -> list[str]:
    return re.findall(r"\d+", text.translate(_DIGIT_FOLD))


def _negations(text: str) -> list[str]:
    return sorted(c for c in text if c in _NEGATION)


@dataclass
class Template:
    regex: re.Pattern
    target: list[str]
    var_names: list[str]
    name_slots: set[str]

    def apply(self, text: str, translate_slot) -> str | None:
        m = self.regex.fullmatch(soft_normalize(text).strip())
        if not m:
            return None
        values = {}
        for name, raw in zip(self.var_names, m.groups()):
            value = raw.strip()
            if name not in self.name_slots and has_cjk(value):
                ko = translate_slot(value)
                if ko is None or has_cjk(ko):
                    return None  # let the cache/Claude handle the whole line
                value = ko
            values[name] = value
        return "".join(_VAR_RE.sub(lambda v: values.get(v.group(1), ""), part)
                       for part in self.target)


@dataclass
class Match:
    ko: str
    method: str  # exact | template | fuzzy | phrase | cache | fuzzy-ocr | carry
    score: float = 100.0


class Dictionary:
    def __init__(self, fuzzy_cutoff: float = 86.0):
        self.fuzzy_cutoff = fuzzy_cutoff
        self.exact: dict[str, str] = {}
        self.templates: list[Template] = []
        self._keys: list[str] = []
        self._phrases: list[tuple[str, str]] = []
        self._glossary: dict[str, str] = {}   # curated terms only
        self._source: dict[str, str] = {}     # key -> file it came from (conflict log)

    # ---- building -----------------------------------------------------
    def add(self, zh: str, ko: str, source: str = "", glossary: bool = False) -> None:
        key = normalize(zh)
        if not key or not has_cjk(key):
            return
        old = self.exact.get(key)
        if old is not None and old != ko:
            # glossary/user_dict override mappings on purpose; mapping-vs-mapping is a bug
            level = logging.INFO if source in ("user_dict.json", "glossary.json") \
                else logging.WARNING
            log.log(level, "dictionary conflict for %r: %r (%s) replaced by %r (%s)",
                    zh, old, self._source.get(key, "?"), ko, source or "?")
        self.exact[key] = ko
        self._source[key] = source
        if glossary and len(key) >= 2:
            self._glossary[key] = ko

    def add_template(self, source: list[str], target: list[str]) -> None:
        pattern, names, name_slots = r"\s*", [], set()
        prev_literal = ""
        for part in source:
            var = _VAR_RE.fullmatch(part.strip())
            if var:
                name = var.group(1)
                names.append(name)
                if prev_literal.rstrip().endswith(_NAME_SLOT_BEFORE):
                    name_slots.add(name)
                pattern += r"(.+?)"
            else:
                lit = soft_normalize(part).strip()
                prev_literal = lit
                pattern += r"\s*".join(re.escape(c) for c in lit if not c.isspace()) + r"\s*"
        if names and has_cjk(pattern):
            self.templates.append(Template(re.compile(pattern, re.IGNORECASE), target,
                                           names, name_slots))

    def load_mapping_file(self, path: Path, glossary: bool = False) -> int:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name}: {e.lineno}번째 줄 {e.colno}번째 칸 - {e.msg}") from e
        entries = data.get("mappings", data) if isinstance(data, dict) else data
        n = 0
        if isinstance(entries, dict):  # plain {"zh": "ko"} user dictionary / glossary
            for zh, ko in entries.items():
                if isinstance(ko, dict):  # a group, e.g. "OCR 오인식": {...}: no glossary
                    for z, k in ko.items():
                        if isinstance(z, str) and isinstance(k, str):
                            self.add(z, k, path.name, glossary=False)
                            n += 1
                elif isinstance(zh, str) and isinstance(ko, str) and not zh.startswith("_"):
                    self.add(zh, ko, path.name, glossary=glossary)
                    n += 1
            return n
        for e in entries:
            if isinstance(e.get("zh"), str) and isinstance(e.get("ko"), str):
                self.add(e["zh"], e["ko"], path.name)
                n += 1
            elif isinstance(e.get("source"), list) and isinstance(e.get("target"), list):
                self.add_template(e["source"], e["target"])
                n += 1
        return n

    def finalize(self) -> None:
        self._keys = list(self.exact)
        # longest first so "开始匹配" wins over "开始"
        self._phrases = sorted(
            ((k, v) for k, v in self.exact.items() if len(k) >= 2),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )
        self._glossary_sorted = sorted(self._glossary.items(), key=lambda kv: len(kv[0]),
                                       reverse=True)

    @classmethod
    def from_dir(cls, mappings_dir: Path, user_dict: Path | None = None,
                 fuzzy_cutoff: float = 86.0, glossary: Path | None = None) -> "Dictionary":
        """Raises ValueError (with file name and position) on a malformed JSON file."""
        d = cls(fuzzy_cutoff)
        for p in sorted(mappings_dir.glob("*.json")):
            d.load_mapping_file(p)
        glossary = glossary if glossary is not None else mappings_dir.parent / "glossary.json"
        if glossary.exists():
            d.load_mapping_file(glossary, glossary=True)
        if user_dict and user_dict.exists():
            d.load_mapping_file(user_dict, glossary=True)  # last: user entries override
        d.finalize()
        return d

    def __len__(self) -> int:
        return len(self.exact) + len(self.templates)

    # ---- lookup -------------------------------------------------------
    def glossary_for(self, texts: list[str], limit: int = 60) -> list[tuple[str, str]]:
        """Curated terms that appear inside any of the texts (keeps Claude's wording
        consistent with established terms)."""
        keys = [normalize(t) for t in texts]
        out: list[tuple[str, str]] = []
        for zh, ko in getattr(self, "_glossary_sorted", []):
            if any(zh in k for k in keys):
                out.append((zh, ko))
                if len(out) >= limit:
                    break
        return out

    def lookup(self, text: str, allow_phrase: bool = True, **_ignored) -> Match | None:
        key = normalize(text)
        if not key or not has_cjk(key):
            return None
        if key in self.exact:
            return Match(self.exact[key], "exact")
        for t in self.templates:
            out = t.apply(text, self._translate_slot)
            if out is not None:
                return Match(out, "template")
        fuzzy = self._fuzzy(key)
        if fuzzy:
            return fuzzy
        return self._phrase(key) if allow_phrase else None

    def _translate_slot(self, value: str) -> str | None:
        key = normalize(value)
        if key in self.exact:
            return self.exact[key]
        m = self._fuzzy(key) or self._phrase(key)
        return m.ko if m else None

    def _fuzzy(self, key: str) -> Match | None:
        if len(key) < 3 or not self._keys:
            return None  # 1-2 char strings are too ambiguous to fuzz
        hit = process.extractOne(key, self._keys, scorer=fuzz.ratio,
                                 score_cutoff=self.fuzzy_cutoff)
        if not hit:
            return None
        matched, score, _ = hit
        # guard against short keys matching inside long strings
        if abs(len(matched) - len(key)) > max(2, len(key) // 5):
            return None
        # never across a different number ("3秒后" vs "5秒后") or negation (不/未/无...)
        if _digit_runs(key) != _digit_runs(matched):
            return None
        if _negations(key) != _negations(matched):
            return None
        if Levenshtein.distance(key, matched) > max(1, len(key) // 10):
            return None
        return Match(self.exact[matched], "fuzzy", score)

    def _phrase(self, key: str) -> Match | None:
        rest, parts, i = key, [], 0
        replaced = False
        while i < len(rest):
            for zh, ko in self._phrases:
                if rest.startswith(zh, i):
                    parts.append(" " + ko + " ")
                    i += len(zh)
                    replaced = True
                    break
            else:
                parts.append(rest[i])
                i += 1
        out = _WS_RE.sub(" ", "".join(parts)).strip()
        if replaced and not has_cjk(out):
            return Match(out, "phrase", 0.0)
        return None
