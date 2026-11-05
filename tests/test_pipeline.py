"""End-to-end: render a fake UI with Chinese text, OCR it, translate it."""

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from pwa_overlay.analyzer import Analyzer, signature_changed

FONT = "C:/Windows/Fonts/msyh.ttc"


def render(lines, size=(900, 420), bg=(30, 33, 40), fg=(225, 225, 225)):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    for (x, y, text, px) in lines:
        d.text((x, y), text, font=ImageFont.truetype(FONT, px), fill=fg)
    return np.array(img)[:, :, ::-1].copy()  # RGB -> BGR


@pytest.fixture(scope="module")
def analyzer(dictionary):
    return Analyzer(dictionary)


def test_ui_labels_translated(analyzer):
    frame = render([
        (40, 30, "平台设置", 18),
        (40, 80, "我的战绩", 18),
        (40, 130, "退出平台", 18),
        (300, 30, "是否将平台最小化到托盘?", 18),
        (300, 130, "不再提醒", 14),
        (40, 220, "Steam 76561198000000000", 16),
    ])
    result = analyzer.analyze(frame)
    got = {i.ko for i in result.items}
    assert {"플랫폼 설정", "내 전적", "플랫폼 종료", "플랫폼을 트레이로 최소화할까요?",
            "다시 알리지 않음"} <= got, (got, result.untranslated)
    # colors sampled from the frame (RGB)
    item = next(i for i in result.items if i.ko == "플랫폼 설정")
    assert max(abs(a - b) for a, b in zip(item.bg, (30, 33, 40))) < 12
    assert sum(item.fg) / 3 > 150


def test_wrapped_paragraph_translated_as_one(analyzer):
    frame = render([
        (40, 40, "系统检测您尚未登录Steam/蒸汽平台，", 18),
        (40, 66, "请您开启并登录后重试", 18),
    ])
    result = analyzer.analyze(frame)
    assert any(i.line_count == 2 and i.ko.startswith("Steam에 로그인") for i in result.items), \
        ([(i.zh, i.ko) for i in result.items], result.untranslated)


def test_signature_detects_change(analyzer):
    frame = render([(40, 30, "平台设置", 18)])
    item = analyzer.analyze(frame).items[0]
    assert not signature_changed(frame, item)
    other = render([(40, 30, "我的战绩", 18)])
    assert signature_changed(other, item)
