"""Layout of long Korean text: wrap into empty space below instead of eliding."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402

from pwa_overlay.analyzer import Line, translate_lines  # noqa: E402
from pwa_overlay.dictionary import Match  # noqa: E402
from pwa_overlay.overlay import _fit_single, draw_items, paint_items  # noqa: E402

FONT = "Malgun Gothic"
DESC_KO = "설명: 사용하면 플랫폼 하이라이트 사용 횟수가 1회 늘어납니다"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QGuiApplication.instance() or QGuiApplication([])


class Table:
    def __init__(self, table):
        self.table = table

    def lookup(self, text, **_):
        ko = self.table.get(text)
        return Match(ko, "exact") if ko else None


def detail_panel():
    """A narrow item-detail panel: three short rows, a long description, a button
    on the same row to the right, and empty panel space below."""
    frame = np.full((300, 420, 3), 28, np.uint8)
    rows = [(20, "数量：6"), (46, "来源：任务奖励"), (72, "描述：使用后平台完美时刻次数加1")]
    lines = []
    for y, text in rows:
        w = 14 * len(text)
        frame[y + 3:y + 15, 20:20 + w] = 210
        lines.append(Line(18, y, 22 + w, y + 18, text, 0.99))
    frame[72:92, 300:360] = 90  # the "使用" button on the description's row
    frame[77:87, 315:345] = 230
    lines.append(Line(312, 74, 348, 90, "使用", 0.99))
    return frame, lines


def test_long_line_wraps_down_instead_of_eliding():
    px, width, height, mode = _fit_single(FONT, DESC_KO, 240, 20, 270, 16, avail_h=120)
    assert mode == "wrap" and height > 20 and px >= 11
    # with no room below it still elides rather than drawing over the next row
    assert _fit_single(FONT, DESC_KO, 240, 20, 270, 16, avail_h=20)[3] == "elide"


def test_description_in_a_detail_panel(tmp_path):
    frame, lines = detail_panel()
    t = Table({"数量：6": "수량: 6", "来源：任务奖励": "출처: 미션 보상",
               "描述：使用后平台完美时刻次数加1": DESC_KO, "使用": "사용"})
    result = translate_lines(frame, lines, t)
    items = draw_items(result.items, lines, frame.shape[1], 0, 0, frame.shape[0])
    desc = next(d for d in items if d.ko == DESC_KO)
    assert desc.room_r <= 312          # never widens over the button
    assert desc.room_b == frame.shape[0]  # nothing below: may wrap down

    rgb = np.ascontiguousarray(frame[:, :, ::-1])
    h, w = frame.shape[:2]
    img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
    p = QPainter(img)
    paint_items(p, items, 0, 0, FONT, debug=True)
    p.end()
    out = tmp_path / "panel.png"
    img.save(str(out))
    # the painted description occupies more than its one-line box (it wrapped)
    painted = np.frombuffer(img.constBits().asstring(img.sizeInBytes()), np.uint8)
    painted = painted.reshape(h, img.bytesPerLine())[:, : w * 3].reshape(h, w, 3)
    below = painted[95:140, 20:300]
    assert (np.abs(below.astype(int) - 28).max(axis=2) > 60).sum() > 50
