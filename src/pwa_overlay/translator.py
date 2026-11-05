"""Translation chain: dictionary -> persistent cache -> Claude (batched, async).

`Translator.lookup()` never blocks. A miss is queued for a background worker that
sends the screen's new strings to Claude in one request and stores the results in
`data/translation_cache.json`. `version` increments whenever new translations land
so the app can re-render without re-running OCR.

Only OCR'd *text* is sent; screenshots never leave the machine. Strings that cannot
or should not be translated (nicknames, garbage) are remembered as KEEP so they are
never sent again, and failures back off persistently (5 min -> 1 h -> 24 h).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .dictionary import Dictionary, Match, has_cjk, normalize, soft_normalize

log = logging.getLogger(__name__)

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_PH_RE = re.compile(r"\{(\d+)\}")
_HANGUL = re.compile(r"[가-힣]")
_SENTENCE = re.compile(r"[。！？!?；;，,]")
_CJK_CHARS = re.compile(r"[㐀-鿿]")
# canonical cache key: drop decoration that OCR reads inconsistently
_KEY_TRAIL = re.compile(r"[;:,.。…>]+$")
_KEY_LEAD = re.compile(r"^(?:[+•.]|\d+\.)\s*")  # normalize() already turned • into .
_KEY_MIDDOT = re.compile(r"(?<=[㐀-鿿])[·•・.](?=[㐀-鿿])")
BACKOFF_STEPS = (300, 3600, 86400)  # seconds: 5 min, 1 h, 24 h
ID_BASE = 100  # request line ids start here (see ClaudeBackend.build_prompt)


def mask_numbers(text: str) -> tuple[str, list[str]]:
    """'个人天赋点：5/20' -> ('个人天赋点：{0}/{1}', ['5', '20'])."""
    nums: list[str] = []

    def sub(m: re.Match) -> str:
        nums.append(m.group(0))
        return "{%d}" % (len(nums) - 1)

    return _NUM_RE.sub(sub, text), nums


def unmask(template: str, nums: list[str]) -> str | None:
    """Fill placeholders back in; None if the template doesn't use exactly them."""
    used = {int(i) for i in _PH_RE.findall(template)}
    if used != set(range(len(nums))):
        return None
    return _PH_RE.sub(lambda m: nums[int(m.group(1))], template)


_NAME_PH = re.compile(r"\{n(\d+)\}")  # nickname slots in learned templates


def compile_template(zh_t: str):
    """Regex for a learned template like '玩家 {n0} 加入了房间{0}' (number slots
    stay literal: lines are matched after number masking). None if the fixed part
    has fewer than 3 Chinese characters (too loose to trust)."""
    text = soft_normalize(zh_t).strip()
    parts = re.split(r"(\{n\d+\})", text)
    literal = "".join(p for p in parts if not _NAME_PH.fullmatch(p))
    if len(_CJK_CHARS.findall(literal)) < 3 or not _NAME_PH.search(text):
        return None
    pattern, names = r"\s*", []
    for part in parts:
        m = _NAME_PH.fullmatch(part)
        if m:
            names.append(m.group(1))
            pattern += r"(.+?)"
        else:
            pattern += r"\s*".join(re.escape(c) for c in part if not c.isspace()) + r"\s*"
    return re.compile(pattern, re.IGNORECASE), names


def match_template(compiled, masked: str) -> dict[str, str] | None:
    regex, names = compiled
    m = regex.fullmatch(soft_normalize(masked).strip())
    if not m:
        return None
    values = {n: v.strip() for n, v in zip(names, m.groups())}
    return values if all(values.values()) else None


def fill_template(ko_t: str, values: dict[str, str]) -> str:
    return _NAME_PH.sub(lambda m: values.get(m.group(1), m.group(0)), ko_t)


def cache_key(masked: str) -> str:
    k = normalize(masked)
    k = _KEY_LEAD.sub("", k)
    k = _KEY_MIDDOT.sub("", k)
    return _KEY_TRAIL.sub("", k)


class TranslationCache:
    """canonical masked zh -> entry, persisted as JSON (survives restarts).
    Entry: {"zh", "ko" (None = KEEP: never translate), "by", "at", "uncertain"}.
    Template entries ("template": true) hold a sentence with the player names
    replaced by {n0}, {n1}...: any line of that shape is translated from them, and
    no nickname is ever stored."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._removed: set[str] = set()
        self.data: dict[str, dict] = self._read()
        self._compiled: list | None = None

    def match_template(self, masked: str) -> str | None:
        """Korean (number slots still masked) for a line that fits a learned
        template, with the names from the line filled in."""
        with self.lock:
            if self._compiled is None:
                self._compiled = []
                for e in self.data.values():
                    if e.get("template") and e.get("ko"):
                        c = compile_template(e["zh"])
                        if c:
                            self._compiled.append((c, e["ko"]))
            compiled = list(self._compiled)
        for c, ko_t in compiled:
            values = match_template(c, masked)
            if values is not None:
                return fill_template(ko_t, values)
        return None

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            log.exception("translation cache unreadable, starting empty: %s", self.path)
            self.path.replace(self.path.with_suffix(".broken.json"))
            return {}
        data: dict[str, dict] = {}
        for e in sorted(raw.values(), key=lambda e: e.get("at", "")):  # newest wins
            if isinstance(e, dict) and isinstance(e.get("zh"), str):
                data[cache_key(e["zh"])] = e
        return data

    def get(self, masked: str) -> dict | None:
        with self.lock:
            return self.data.get(cache_key(masked))

    def items(self) -> list[tuple[str, dict]]:
        with self.lock:
            return list(self.data.items())

    def _write(self) -> None:
        # merge with the file first, so a hand edit made meanwhile isn't overwritten
        disk = self._read()
        for k, v in self.data.items():
            if k not in disk or disk[k].get("at", "") <= v.get("at", ""):
                disk[k] = v
        for k in self._removed:
            disk.pop(k, None)
        self.data = disk
        self._compiled = None
        snapshot = json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(snapshot, encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file

    def put_many(self, entries: list[tuple], source: str) -> None:
        """entries: (masked zh, masked ko or None for KEEP, uncertain[, template])."""
        with self.lock:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            for masked_zh, masked_ko, uncertain, *rest in entries:
                k = cache_key(masked_zh)
                self._removed.discard(k)
                e = {"zh": masked_zh, "ko": masked_ko, "by": source, "at": now,
                     "uncertain": bool(uncertain)}
                if rest and rest[0]:
                    e["template"] = True
                self.data[k] = e
            self._write()

    @staticmethod
    def valid_entry(e) -> bool:
        """A shareable cache entry: Chinese source; Korean (or None = KEEP) with no
        Chinese left and the same number placeholders."""
        if not isinstance(e, dict) or not isinstance(e.get("zh"), str) or not has_cjk(e["zh"]):
            return False
        ko = e.get("ko")
        if ko is None:
            return not e.get("template")
        ok = (isinstance(ko, str) and bool(ko.strip()) and not has_cjk(ko)
              and set(_PH_RE.findall(ko)) == set(_PH_RE.findall(e["zh"])))
        if e.get("template"):
            ok = ok and set(_NAME_PH.findall(ko)) == set(_NAME_PH.findall(e["zh"])) \
                and compile_template(e["zh"]) is not None
        return ok

    def merge_file(self, path: Path) -> tuple[int, int, int]:
        """Merge a shared translation_cache.json into this one. New sentences are
        added; for one both sides have, the more recent translation wins; invalid
        entries are skipped. Raises ValueError if the file isn't a cache at all.
        Returns (added, updated, skipped)."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise ValueError(f"{Path(path).name}: 읽을 수 없는 파일입니다 ({e})") from e
        if not isinstance(raw, dict) or not raw:
            raise ValueError(f"{Path(path).name}: 번역 캐시 파일이 아닙니다")
        added = updated = skipped = 0
        with self.lock:
            for e in raw.values():
                if not self.valid_entry(e):
                    skipped += 1
                    continue
                k = cache_key(e["zh"])
                mine = self.data.get(k)
                if mine is None:
                    added += 1
                elif e.get("at", "") > mine.get("at", ""):
                    updated += 1
                else:
                    continue
                entry = {"zh": e["zh"], "ko": e.get("ko"), "by": str(e.get("by", "shared")),
                         "at": str(e.get("at", "")), "uncertain": bool(e.get("uncertain"))}
                if e.get("template"):
                    entry["template"] = True
                self.data[k] = entry
                self._removed.discard(k)
            if added + updated == 0 and skipped == len(raw):
                raise ValueError(f"{Path(path).name}: 쓸 수 있는 번역이 없습니다")
            if added or updated:
                self._write()
        return added, updated, skipped

    def shareable(self) -> dict[str, dict]:
        """Entries fit to give to other people: valid translations only. KEEP entries
        are left out: they are mostly other players' nicknames seen on screen, and
        only matter on this PC (to avoid asking about them again)."""
        with self.lock:
            return {k: e for k, e in self.data.items()
                    if self.valid_entry(e) and e.get("ko") is not None}

    def export_to(self, path: Path) -> int:
        """Write a copy of the cache for sharing (see shareable())."""
        data = self.shareable()
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                              encoding="utf-8")
        return len(data)

    def remove(self, keys: list[str] | None = None, uncertain_only: bool = False) -> int:
        """Drop entries: the given keys, or every uncertain one, or everything."""
        with self.lock:
            if keys is not None:
                drop = [k for k in keys if k in self.data]
            elif uncertain_only:
                drop = [k for k, e in self.data.items() if e.get("uncertain")]
            else:
                drop = list(self.data)
            for k in drop:
                self.data.pop(k, None)
            self._removed |= set(drop)
            self._write()
            return len(drop)

    def __len__(self) -> int:
        return len(self.data)


class FailureLog:
    """Lines that could not be translated, with persistent backoff. Cleared whenever
    the dictionary/glossary changes (a new term may fix them)."""

    def __init__(self, path: Path, dict_hash: str):
        self.path = path
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                if raw.get("dict_hash") == dict_hash:
                    self.items = raw.get("items", {})
            except (OSError, ValueError):
                pass
        self.dict_hash = dict_hash

    def blocked(self, key: str) -> bool:
        with self.lock:
            e = self.items.get(key)
            return bool(e and e["retry_at"] > time.time())

    def record(self, keys: list[str]) -> None:
        with self.lock:
            for k in keys:
                e = self.items.get(k, {"count": 0})
                e["count"] += 1
                step = BACKOFF_STEPS[min(e["count"], len(BACKOFF_STEPS)) - 1]
                e["retry_at"] = time.time() + step
                self.items[k] = e
            self._save()

    def clear(self, keys: list[str] | None = None) -> None:
        with self.lock:
            if keys is None:
                self.items.clear()
            for k in keys or []:
                self.items.pop(k, None)
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"dict_hash": self.dict_hash, "items": self.items},
                                            ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.exception("could not save %s", self.path)


SYSTEM_PROMPT = """\
You translate the user interface of Perfect World Arena (完美世界竞技平台), a Chinese \
Counter-Strike 2 matchmaking platform, into natural Korean for Korean players.

- Lines come from OCR of the screen. The screen context (in on-screen reading order, \
including Latin text like SEASON or RANK) shows what the lines belong to and can \
reveal misread characters.
- Lines marked [OCR uncertain] probably contain misread characters; translate the \
most plausible intended UI text, not the literal characters.
- Translate only the lines under "Translate these lines". Return exactly one \
translation per [id], using that number as "id"; never translate the context.
- Player, team and clan nicknames, IDs, and unreadable fragments that are not real \
UI text: return ko = "" (they stay as they are on screen).
- If a line contains a player, team or clan nickname inside a sentence (system \
messages like "玩家 某某 已加入房间"), also return zh_template and ko_template: the \
line and its translation with each nickname replaced by {n0}, {n1}... in order \
(keep {0}, {1} number slots as they are; zh_template must be the line exactly, \
except the nicknames). Item, skin or event names in quotes are NOT nicknames. For \
every other line return both as "".
- Write Korean only: never leave Chinese characters in the output, not even one \
(e.g. 国服 -> 중국 서버, never "국服").
- "W" after a number is 万 (10,000): 100W -> 100만, 500W -> 500만.
- 完美 as a name (完美平台, 完美开箱, 完美电竞, 完美APP) is the platform brand: write \
"완미", not "완벽". 反恐精英 is Counter-Strike: write "CS".
- Keep placeholders such as {0} and {1} exactly as they are, each used once; they \
stand for numbers.
- Keep Latin text, brand names, Steam and IDs unchanged.
- Buttons, tabs and labels: short Korean UI wording (noun phrases, no 합니다/해요 \
endings). A label marked (≤N자) should fit in about N Korean characters: prefer the \
shortest natural UI term, but never drop meaning to fit. Sentences and descriptions: \
polite, natural Korean (합니다체).
- Use the glossary's Korean for those Chinese terms, unless the line clearly means \
something else."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "ko": {"type": "string"},
                               "zh_template": {"type": "string"},
                               "ko_template": {"type": "string"}},
                "required": ["id", "ko", "zh_template", "ko_template"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def output_schema(n: int) -> dict:
    """OUTPUT_SCHEMA with ids limited to this request's lines (ID_BASE..ID_BASE+n-1),
    so the model can't answer with its own numbering."""
    import copy

    schema = copy.deepcopy(OUTPUT_SCHEMA)
    item = schema["properties"]["translations"]["items"]
    item["properties"]["id"] = {"type": "integer", "enum": [ID_BASE + i for i in range(n)]}
    return schema


class BatchError(Exception):
    """A response that can't be used as a whole (refusal, truncation, bad JSON).
    The batch is split and retried; a single line that still fails is backed off."""


class DisabledError(Exception):
    """Machine translation can't work (bad key, wrong model, no credit)."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


@dataclass
class Usage:
    calls: list[float] = field(default_factory=list)  # timestamps (sliding windows)
    day: str = ""
    day_calls: int = 0
    day_in: int = 0
    day_out: int = 0

    def add(self, tokens_in: int, tokens_out: int, price_in: float, price_out: float) -> str:
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_calls, self.day_in, self.day_out = today, 0, 0, 0
        self.calls.append(time.time())
        self.day_calls += 1
        self.day_in += tokens_in
        self.day_out += tokens_out
        cost = self.day_in / 1e6 * price_in + self.day_out / 1e6 * price_out
        return (f"Claude usage today: {self.day_calls} calls, in {self.day_in} / "
                f"out {self.day_out} tokens, ~${cost:.4f}")

    def count_since(self, seconds: float) -> int:
        now = time.time()
        self.calls = [t for t in self.calls if t > now - 86400]
        return sum(1 for t in self.calls if t > now - seconds)


@dataclass
class _Pending:
    masked: str
    context: tuple[str, ...]
    uncertain: bool = False
    budget: int = 0  # (≤N자) hint for short labels, 0 = none


def _label_budget(text: str) -> int:
    """Short labels get a Korean length hint; sentences don't."""
    n = len(_CJK_CHARS.findall(text))
    if n == 0 or n > 8 or _SENTENCE.search(text):
        return 0
    return max(2, round(n * 1.5))


class ClaudeBackend:
    """One request per batch of new strings."""

    PRICE_IN, PRICE_OUT = 0.10, 0.50  # claude-haiku-5-5, $ per million tokens

    def __init__(self, model: str, thinking: str = "disabled"):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=2, timeout=20.0)
        self.model = model
        self.thinking = thinking
        self.usage = Usage()

    @staticmethod
    def build_prompt(texts: list[str], glossary: list[tuple[str, str]], screen: list[str],
                     uncertain: list[bool], budgets: list[int], retry: bool) -> str:
        def target(i: int) -> str:
            # ids start at ID_BASE so they can't collide with the model numbering the
            # context lines itself from 0
            return (f"[{ID_BASE + i}] {texts[i]}" + ("   [OCR uncertain]" if uncertain[i] else "")
                    + (f"   (≤{budgets[i]}자)" if budgets[i] else ""))

        # Context and targets in separate blocks: with both in one list, a model asked
        # for one line tends to translate the whole screen with its own numbering.
        keys = {cache_key(t) for t in texts}
        seen, context = set(), []
        for line in screen:
            line = line.strip()
            k = cache_key(mask_numbers(line)[0])
            if not line or k in seen or k in keys or any(line in t for t in texts):
                continue  # duplicates, the targets themselves, pieces of a whole sentence
            seen.add(k)
            context.append(line)
        parts = []
        if glossary:
            parts.append("Glossary (Chinese = preferred Korean):\n" +
                         "\n".join(f"{zh} = {ko}" for zh, ko in glossary))
        if context:
            parts.append("Screen context, in reading order (do NOT translate these):\n"
                         + "\n".join(context))
        parts.append("Translate these lines:\n" + "\n".join(target(i) for i in range(len(texts))))
        if retry:
            parts.append("A previous attempt left Chinese characters in the Korean. "
                         "Translate every Chinese word fully into Korean.")
        return "\n\n".join(parts)

    def translate(self, texts: list[str], glossary: list[tuple[str, str]],
                  screen: list[str], uncertain: list[bool] | None = None,
                  budgets: list[int] | None = None, retry: bool = False) -> dict[int, str]:
        uncertain = uncertain or [False] * len(texts)
        budgets = budgets or [0] * len(texts)
        content = self.build_prompt(texts, glossary, screen, uncertain, budgets, retry)
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=16000,  # only generated tokens are billed; avoids truncated JSON
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"} if self.thinking == "adaptive"
            else {"type": "disabled"},
            output_config={"effort": "low",
                           "format": {"type": "json_schema",
                                      "schema": output_schema(len(texts))}},
            messages=[{"role": "user", "content": content}],
        )
        u = resp.usage
        log.info("Claude call: %d lines, stop=%s, in=%d out=%d | %s", len(texts),
                 resp.stop_reason, u.input_tokens, u.output_tokens,
                 self.usage.add(u.input_tokens, u.output_tokens, self.PRICE_IN, self.PRICE_OUT))
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise BatchError(resp.stop_reason)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BatchError(f"bad JSON: {e}") from e
        out: dict[int, object] = {}
        for t in data.get("translations", []):
            i = t.get("id")
            if not (isinstance(t.get("ko"), str) and isinstance(i, int)
                    and 0 <= i - ID_BASE < len(texts)):
                continue
            zt, kt = t.get("zh_template") or "", t.get("ko_template") or ""
            out[i - ID_BASE] = (t["ko"], zt, kt) if zt and kt else t["ko"]
        return out


class Translator:
    def __init__(self, dictionary: Dictionary, cache_path: Path,
                 backend: ClaudeBackend | None = None, batch_size: int = 40,
                 batch_wait_s: float = 0.25, max_calls_per_hour: int = 120,
                 max_calls_per_day: int = 1500):
        self.dictionary = dictionary
        self.cache = TranslationCache(cache_path)
        self.failures = FailureLog(cache_path.with_name("translation_failures.json"),
                                   self._dict_hash(dictionary))
        self.backend = backend
        self.batch_size = batch_size
        self.batch_wait_s = batch_wait_s
        self.max_calls_per_hour = max_calls_per_hour
        self.max_calls_per_day = max_calls_per_day
        self.version = 0                 # bumps when new translations arrive
        self.status = "사전만 사용" if backend is None else "Claude 번역 사용"
        self._pending: dict[str, _Pending] = {}   # cache key -> request
        self._seen_once: dict[str, float] = {}    # stability gate for shaky OCR reads
        self._blocked_until = 0.0                 # global pause (rate limit, network)
        self._cv = threading.Condition()
        self._stop = False
        self._worker: threading.Thread | None = None
        if backend is not None:
            self._worker = threading.Thread(target=self._run, name="translate", daemon=True)
            self._worker.start()

    @staticmethod
    def _dict_hash(d: Dictionary) -> str:
        h = hashlib.sha1()
        for k in sorted(d.exact):
            h.update(k.encode("utf-8"))
            h.update(d.exact[k].encode("utf-8"))
        return h.hexdigest()[:16]

    def set_dictionary(self, d: Dictionary) -> None:
        """Swap in a reloaded dictionary without losing the queue or the cache."""
        self.dictionary = d
        new_hash = self._dict_hash(d)
        if new_hash != self.failures.dict_hash:
            self.failures.dict_hash = new_hash
            self.failures.clear()
        self.version += 1

    @property
    def machine_translation(self) -> bool:
        return self.backend is not None

    # ---- called from the OCR / UI threads --------------------------------
    def lookup(self, text: str, enqueue: bool = True, allow_phrase: bool = True,
               context: tuple[str, ...] = (), score: float | None = None,
               uncertain: bool = False, chars: tuple[tuple[str, float], ...] | None = None,
               dict_only: bool = False) -> Match | None:
        m = self.dictionary.lookup(text,
                                   allow_phrase=allow_phrase and not self.machine_translation)
        if m:
            return m
        if not has_cjk(text):
            return None
        masked, nums = mask_numbers(text.strip())
        # user_dict / mapping entries written with {0} placeholders
        d = self.dictionary.exact.get(normalize(masked))
        if d is not None and nums:
            filled = unmask(d, nums)
            if filled is not None:
                return Match(filled, "exact")
        if dict_only:
            return None
        entry = self.cache.get(masked)
        if entry is not None:
            if entry.get("ko") is None:
                return None  # KEEP: a nickname or fragment; leave it as it is
            filled = unmask(entry["ko"], nums)
            if filled is not None:
                return Match(filled, "cache")
        ko_t = self.cache.match_template(masked)
        if ko_t is not None:
            filled = unmask(ko_t, nums)
            if filled is not None:
                return Match(filled, "template")
        if uncertain and chars:
            m = self._confident_char_match(chars)
            if m:
                return m
        if self.machine_translation and enqueue:
            self._enqueue(masked, context, uncertain)
        return None

    def _confident_char_match(self, chars) -> Match | None:
        """An OCR line with a few low-confidence characters: if the characters the OCR
        is sure of pin down exactly one known string of the same length, use it."""
        norm = [normalize(c) for c, _ in chars]
        if any(len(c) != 1 for c in norm) or len(norm) < 4:
            return None
        sure = [i for i, (_, conf) in enumerate(chars) if conf >= 0.5]
        if len(sure) == len(norm) or len(sure) < max(3, int(len(norm) * 0.6)):
            return None
        found: dict[str, str] = {}
        for k, ko in self.dictionary.exact.items():
            if len(k) == len(norm) and all(k[i] == norm[i] for i in sure):
                found[k] = ko
        for k, e in self.cache.items():
            if e.get("ko") and "{" not in e["ko"] and len(k) == len(norm) \
                    and all(k[i] == norm[i] for i in sure):
                found.setdefault(k, e["ko"])
        if len(found) == 1:
            return Match(next(iter(found.values())), "fuzzy-ocr")
        return None

    def _enqueue(self, masked: str, context: tuple[str, ...], uncertain: bool) -> None:
        key = cache_key(masked)
        if not key or self.failures.blocked(key):
            return
        n_cjk = len(_CJK_CHARS.findall(masked))
        if n_cjk < 2 and not ("{" in masked or re.search(r"[A-Za-z]{2,}", masked)):
            return  # a lone character is a fragment; "ROLL房" / "{0}天" are real labels
        with self._cv:
            if key in self._pending:
                return
            if uncertain and n_cjk <= 8:
                # a shaky read must still be there a moment later before it costs a
                # call (animation garble vanishes; a static page keeps it)
                first = self._seen_once.get(key)
                now = time.monotonic()
                if first is None or now - first > 30:
                    self._seen_once[key] = now
                    self._schedule_recheck()
                    return
            self._pending[key] = _Pending(masked, context, uncertain, _label_budget(masked))
            self._cv.notify()

    RECHECK_S = 2.5
    _recheck: threading.Timer | None = None

    def _schedule_recheck(self) -> None:
        """Bump `version` shortly, so the app re-runs lookups on the last OCR result
        (no new OCR needed): deferred shaky lines that are still there get queued."""
        if self._recheck is not None and self._recheck.is_alive():
            return

        def bump():
            self.version += 1

        self._recheck = threading.Timer(self.RECHECK_S, bump)
        self._recheck.daemon = True
        self._recheck.start()

    def pending_count(self) -> int:
        with self._cv:
            return len(self._pending)

    def forget(self, texts: list[str]) -> int:
        """'Translate this screen again': drop cached translations and failure
        records for these strings so they are re-queued."""
        keys = [cache_key(mask_numbers(t.strip())[0]) for t in texts if has_cjk(t)]
        self.failures.clear(keys)
        n = self.cache.remove(keys)
        self.version += 1
        return n

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    # ---- worker -----------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
            wait = self._blocked_until - time.monotonic()
            time.sleep(max(self.batch_wait_s, wait))  # let the screen queue up / back off
            if self.backend is None:
                return
            if not self._within_caps():
                with self._cv:
                    self._pending.clear()  # re-queued by later OCR once the window frees
                time.sleep(30)
                continue
            with self._cv:
                keys = list(self._pending)[: self.batch_size]
                batch = [self._pending[k] for k in keys]
            try:
                self._translate_batch(keys, batch)
            except DisabledError as e:
                log.error("machine translation disabled: %s", e.status)
                self.status = e.status
                self.backend = None
            except Exception as e:  # noqa: BLE001 (any failure: back off, keep running)
                try:
                    self._on_error(keys, e)
                except DisabledError as d:
                    log.error("machine translation disabled: %s", d.status)
                    self.status = d.status
                    self.backend = None
            finally:
                with self._cv:
                    for k in keys:
                        self._pending.pop(k, None)

    def _within_caps(self) -> bool:
        u = self.backend.usage
        if u.count_since(3600) >= self.max_calls_per_hour:
            self.status = "번역 한도 도달(시간당) → 사전/캐시만"
            return False
        if u.count_since(86400) >= self.max_calls_per_day:
            self.status = "번역 한도 도달(하루) → 사전/캐시만"
            return False
        return True

    def _call(self, texts, glossary, screen, uncertain, budgets, retry=False, depth=0):
        """Translate with recovery: a refused/truncated/garbled batch is split in half.
        Returns ({index: ko}, [indices that failed for good])."""
        try:
            return self.backend.translate(texts, glossary, screen, uncertain, budgets,
                                          retry), []
        except BatchError as e:
            if len(texts) == 1 or depth >= 3:
                log.warning("batch failed (%s): %s", e, texts)
                return {}, list(range(len(texts)))
            mid = len(texts) // 2
            a, fa = self._call(texts[:mid], glossary, screen, uncertain[:mid], budgets[:mid],
                               retry, depth + 1)
            b, fb = self._call(texts[mid:], glossary, screen, uncertain[mid:], budgets[mid:],
                               retry, depth + 1)
            return {**a, **{mid + i: v for i, v in b.items()}}, fa + [mid + i for i in fb]

    def _translate_batch(self, keys: list[str], batch: list[_Pending]) -> None:
        texts = [p.masked for p in batch]
        uncertain = [p.uncertain for p in batch]
        budgets = [p.budget for p in batch]
        screen = list(batch[0].context)[:60]
        glossary = self._glossary(texts, screen)
        result, failed = self._call(texts, glossary, screen, uncertain, budgets)
        templates = {}
        for i, v in list(result.items()):
            if isinstance(v, tuple):
                result[i], templates[i] = v[0], (v[1], v[2])

        # Hangul mixed with leftover Chinese: one cheap retry, those lines only
        mixed = [i for i, ko in result.items() if has_cjk(ko) and _HANGUL.search(ko)]
        if mixed:
            again, _ = self._call([texts[i] for i in mixed], glossary, [],
                                  [uncertain[i] for i in mixed], [budgets[i] for i in mixed],
                                  retry=True)
            for j, i in enumerate(mixed):
                if j in again:
                    v = again[j]
                    result[i] = v[0] if isinstance(v, tuple) else v

        good: list[tuple[str, str | None, bool]] = []
        bad: list[str] = [keys[i] for i in failed]
        for i, masked in enumerate(texts):
            if i in failed:
                continue
            ko = result.get(i)
            if ko is None:
                bad.append(keys[i])
                continue
            ko = ko.strip()
            if ko == "" or normalize(ko) == normalize(masked):
                if self._may_keep(masked):
                    good.append((masked, None, uncertain[i]))  # KEEP: never send again
                else:  # a real UI line came back blank: try again later, never KEEP
                    log.warning("blank translation for a UI line: %s", masked)
                    bad.append(keys[i])
                continue
            no_korean = len(_CJK_CHARS.findall(masked)) >= 4 and not _HANGUL.search(ko)
            if has_cjk(ko) or no_korean or \
                    set(_PH_RE.findall(ko)) != set(_PH_RE.findall(masked)):
                # no_korean: a real sentence answered with e.g. another line's Latin text
                log.warning("unusable translation for %s -> %r", masked, ko)
                bad.append(keys[i])
                continue
            tpl = self._valid_template(masked, ko, templates.get(i))
            if not tpl and _NAME_PH.search(ko):
                bad.append(keys[i])   # a slot with nothing to fill it from
                continue
            if tpl:
                # store the shape, not the line: the nickname never reaches the cache
                good.append((tpl[0], tpl[1], uncertain[i], True))
            else:
                good.append((masked, ko, uncertain[i]))
        if bad:
            self.failures.record(bad)
        if good:
            self.cache.put_many(good, self.backend.model)
            self.version += 1
        self.status = (f"Claude 번역 사용 · 캐시 {len(self.cache)}개 · "
                       f"오늘 {self.backend.usage.day_calls}회")

    @staticmethod
    def _valid_template(masked: str, ko: str, tpl) -> tuple[str, str] | None:
        """A nickname template is kept only if it reproduces the line it came from,
        has a real fixed part, and uses the same slots on both sides."""
        if not tpl:
            return None
        zh_t, ko_t = tpl[0].strip(), tpl[1].strip()
        names = set(_NAME_PH.findall(zh_t))
        if not names or names != set(_NAME_PH.findall(ko_t)) or has_cjk(ko_t):
            return None
        if not (set(_PH_RE.findall(zh_t)) == set(_PH_RE.findall(ko_t))
                == set(_PH_RE.findall(masked))):
            return None
        compiled = compile_template(zh_t)
        if compiled is None or match_template(compiled, masked) is None:
            log.info("template rejected (does not reproduce the line): %s", zh_t)
            return None
        return zh_t, ko_t

    def _may_keep(self, masked: str) -> bool:
        """Only name-like lines may be left untranslated for good: no numbers, no
        known game term inside, and short."""
        if _PH_RE.search(masked) or len(_CJK_CHARS.findall(masked)) > 12:
            return False
        return not self.dictionary.glossary_for([masked])

    def _glossary(self, texts: list[str], screen: list[str]) -> list[tuple[str, str]]:
        """Curated terms in these lines, plus short labels already translated on this
        screen, so the same word reads the same everywhere."""
        out = dict(self.dictionary.glossary_for(texts))
        joined = [normalize(t) for t in texts]
        for line in screen:
            masked, nums = mask_numbers(line.strip())
            k = cache_key(masked)
            if 2 <= len(k) <= 8 and not nums and k not in out \
                    and any(k in j for j in joined):
                e = self.cache.get(masked)
                if e and e.get("ko"):
                    out[k] = e["ko"]
        return list(out.items())[:60]

    def _on_error(self, keys: list[str], e: Exception) -> None:
        anthropic = self.backend.anthropic
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            raise DisabledError("API 키 오류 → 사전만 사용") from e
        if isinstance(e, anthropic.NotFoundError):
            raise DisabledError("모델 이름 오류(mt_model) → 사전만 사용") from e
        status = getattr(e, "status_code", None)
        if status == 402 or (status == 400 and "credit" in str(e).lower()):
            raise DisabledError("API 크레딧 부족 → 사전만 사용") from e
        if isinstance(e, anthropic.RateLimitError):
            retry_after = 60.0
            try:
                retry_after = float(e.response.headers.get("retry-after", "60"))
            except (AttributeError, TypeError, ValueError):
                pass
            self._blocked_until = time.monotonic() + min(retry_after, 300.0)
            self.status = "API 요청 한도 → 잠시 후 재시도"
        elif isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)) or \
                (status is not None and status >= 500):
            self._blocked_until = time.monotonic() + 30.0
            self.status = "네트워크/서버 오류 → 잠시 후 재시도"
        else:
            self.failures.record(keys)  # a request this content can't make
            self.status = "번역 오류 → 해당 문장은 나중에 재시도"
        log.warning("translation batch failed (%s): %s", type(e).__name__, e)


def make_translator(dictionary: Dictionary, root: Path, enabled: bool, model: str,
                    thinking: str = "disabled", max_calls_per_hour: int = 120,
                    max_calls_per_day: int = 1500) -> Translator:
    backend = None
    if enabled:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") \
                or load_key_from_registry():
            backend = ClaudeBackend(model, thinking)
        else:
            log.warning("ANTHROPIC_API_KEY not found: dictionary-only mode")
    t = Translator(dictionary, root / "data" / "translation_cache.json", backend,
                   max_calls_per_hour=max_calls_per_hour,
                   max_calls_per_day=max_calls_per_day)
    if enabled and backend is None:
        t.status = "API 키 없음 → 사전만 사용"
    # the exe consumes its copy of the shared cache; a source checkout keeps the
    # git-tracked original
    merge_shared_cache(t.cache, root / "data" / SHARED_CACHE_NAME,
                       delete_after=bool(getattr(sys, "frozen", False)))
    return t


SHARED_CACHE_NAME = "translation_cache.shared.json"


def merge_shared_cache(cache: TranslationCache, shared: Path,
                       delete_after: bool = False) -> None:
    """The published, nickname-free cache (data/translation_cache.shared.json) is
    merged into the local one at startup, so a new release or `git pull` brings
    its translations; local translations that are newer are kept (merge, never
    overwrite). With delete_after the copy is removed once merged."""
    if not shared.exists():
        return
    try:
        added, updated, _ = cache.merge_file(shared)
    except ValueError as e:
        log.warning("shared translation cache not used: %s", e)
        return
    log.info("shared translation cache: +%d new, %d updated", added, updated)
    if delete_after:
        try:
            shared.unlink()
        except OSError:
            log.warning("could not delete %s", shared)


def load_key_from_registry() -> bool:
    """`setx ANTHROPIC_API_KEY ...` only reaches processes started afterwards from a
    fresh parent; read the user (then system) environment from the registry so the
    overlay works however it was launched. Sets os.environ for the SDK."""
    try:
        import winreg
    except ImportError:
        return False
    places = [(winreg.HKEY_CURRENT_USER, r"Environment"),
              (winreg.HKEY_LOCAL_MACHINE,
               r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")]
    for hive, path in places:
        try:
            with winreg.OpenKey(hive, path) as k:
                value, _ = winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            os.environ["ANTHROPIC_API_KEY"] = value.strip()
            log.info("ANTHROPIC_API_KEY loaded from the Windows environment settings")
            return True
    return False
