"""Yuan amounts drawn with an approximate won value."""

import json

from pwa_overlay.currency import FxRate, add_krw, format_krw


def test_format_rounds_to_readable_units():
    assert format_krw(84) == "약 80원"
    assert format_krw(2_345) == "약 2,300원"
    assert format_krw(25_680) == "약 2.6만 원"
    assert format_krw(30_000) == "약 3만 원"
    assert format_krw(1_254_000) == "약 125만 원"
    assert format_krw(250_000_000) == "약 2.5억 원"


def test_amounts_in_text():
    r = 200.62
    assert add_krw("가치 ¥128", r) == "가치 ¥128 (약 2.6만 원)"
    assert add_krw("참고 가치 ￥1,299", r) == "참고 가치 ￥1,299 (약 26만 원)"
    assert add_krw("¥12.5 / ¥3", r) == "¥12.5 (약 2,500원) / ¥3 (약 600원)"
    assert add_krw("¥1.5만", r) == "¥1.5만 (약 301만 원)"
    once = add_krw("가치 ¥128", r)
    assert add_krw(once, r) == once                    # never doubled
    assert add_krw("가치 ¥128", None) == "가치 ¥128"   # no rate yet: unchanged
    assert add_krw("레벨 128", r) == "레벨 128"


def test_saved_rate_is_used_offline(tmp_path):
    p = tmp_path / "fx_rate.json"
    p.write_text(json.dumps({"rate": 200.5, "date": "2026-11-04", "fetched": 1.0}),
                 encoding="utf-8")
    fx = FxRate(p)
    assert fx.rate == 200.5
    p.write_text(json.dumps({"rate": 0.002}), encoding="utf-8")   # nonsense is ignored
    assert FxRate(p).rate is None
