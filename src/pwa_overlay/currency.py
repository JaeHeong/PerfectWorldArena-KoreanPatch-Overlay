"""Yuan amounts (¥128) shown with an approximate won value: "¥128 (약 2.6만 원)".

Display only: the translation cache keeps the original text. The CNY→KRW rate comes
from frankfurter.dev (European Central Bank reference rates, free, no key), at most
every 12 hours, and is kept in data/fx_rate.json so it also works offline.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

RATE_URL = "https://api.frankfurter.dev/v1/latest?base=CNY&symbols=KRW"
MAX_AGE_S = 12 * 3600
RETRY_S = 3600          # after a failed fetch, try again at most hourly

# ¥128, ￥1,299, ¥ 12.5, ¥1.5만 (W = 万 is translated to 만); not one already converted
_AMOUNT = re.compile(r"([¥￥])\s*(\d[\d,]*(?:\.\d+)?)(\s*만)?(?![\d.,])(?!\s*\(약)")


def format_krw(won: float) -> str:
    if won < 10_000:
        step = 100 if won >= 1000 else 10
        return f"약 {max(step, int(round(won / step)) * step):,}원"
    if won < 100_000_000:
        man = won / 10_000
        s = f"{man:.1f}".rstrip("0").rstrip(".") if man < 10 else f"{round(man):,}"
        return f"약 {s}만 원"
    eok = won / 100_000_000
    return f"약 {f'{eok:.1f}'.rstrip('0').rstrip('.')}억 원"


def add_krw(text: str, rate: float | None) -> str:
    """'가치 ¥128' -> '가치 ¥128 (약 2.6만 원)'. Unchanged without a rate."""
    if not rate or ("¥" not in text and "￥" not in text):
        return text

    def sub(m: re.Match) -> str:
        try:
            value = float(m.group(2).replace(",", ""))
        except ValueError:
            return m.group(0)
        if m.group(3):
            value *= 10_000
        return f"{m.group(0)} ({format_krw(value * rate)})"

    return _AMOUNT.sub(sub, text)


class FxRate:
    """The CNY→KRW rate, refreshed in the background, remembered on disk."""

    def __init__(self, path: Path):
        self.path = path
        self.rate: float | None = None
        self.date = ""
        self.fetched = 0.0
        self._busy = False
        self._last_try = 0.0
        try:
            d = json.loads(path.read_text(encoding="utf-8-sig"))
            if self._sane(d.get("rate")):
                self.rate, self.date = float(d["rate"]), str(d.get("date", ""))
                self.fetched = float(d.get("fetched", 0))
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _sane(rate) -> bool:
        return isinstance(rate, (int, float)) and 50 < rate < 1000

    def refresh_async(self) -> None:
        """Fetch a new rate in the background if the saved one is old (cheap to call
        often: returns at once when nothing is due)."""
        now = time.time()
        if self._busy or now - self.fetched < MAX_AGE_S or now - self._last_try < RETRY_S:
            return
        self._busy, self._last_try = True, now
        threading.Thread(target=self._fetch, name="fx-rate", daemon=True).start()

    def _fetch(self) -> None:
        try:
            req = urllib.request.Request(RATE_URL, headers={"User-Agent": "PWAKoreanOverlay"})
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (fixed https URL)
                d = json.loads(r.read().decode("utf-8"))
            rate = d["rates"]["KRW"]
            if not self._sane(rate):
                raise ValueError(f"unexpected rate {rate!r}")
            self.rate, self.date, self.fetched = float(rate), str(d.get("date", "")), time.time()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"rate": self.rate, "date": self.date,
                                             "fetched": self.fetched}), encoding="utf-8")
            log.info("CNY→KRW rate %.2f (%s)", self.rate, self.date)
        except Exception as e:  # noqa: BLE001 (offline etc.: keep the saved rate)
            log.warning("exchange rate not updated: %s", e)
        finally:
            self._busy = False
