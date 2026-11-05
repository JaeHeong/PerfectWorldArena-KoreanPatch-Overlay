"""Transparent, click-through, always-on-top window that paints the Korean text.

The window exists only while there is something to draw: it is hidden whenever the
client is not the active window (so no topmost layered window sits over a game),
and it covers only the client windows' area, never the whole desktop.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                         QPen)
from PyQt6.QtWidgets import QWidget

from . import win32

MIN_PX = 11          # smallest font for single-line labels before wrapping/eliding
MIN_PX_PARAGRAPH = 10
MAX_WIDEN = 1.6      # single-line items may grow to this much of their width...
ART_ALPHA = 210      # plate opacity over artwork (must still hide the Chinese)


@dataclass(frozen=True)
class DrawItem:
    """Screen-space item (physical pixels)."""
    x0: int
    y0: int
    x1: int
    y1: int
    ko: str
    bg: tuple[int, int, int]
    fg: tuple[int, int, int]
    line_count: int = 1
    font_px: int = 0      # preferred font size from the original ink height (0 = from box)
    art: bool = False     # text sits on artwork: translucent plate + outlined text
    room_l: int = -1      # ...but only into free space up to these x limits
    room_r: int = -1
    room_b: int = -1      # long text may wrap downward into empty space up to this y


def free_space(fill: tuple[int, int, int, int], lines, width: int,
               height: int = 1 << 30, widen: bool = True) -> tuple[int, int, int]:
    """Where an item may grow: sideways up to the nearest text on the same row, and
    down to the nearest text below it (`lines` = all OCR lines of the window,
    translated or not), else the window edges."""
    fx0, fy0, fx1, fy1 = fill
    left, right = (0, width) if widen else (fx0, fx1)
    if widen:
        for l in lines:
            overlap = min(fy1, l.y1) - max(fy0, l.y0)
            if overlap < 0.5 * min(fy1 - fy0, l.y1 - l.y0):
                continue
            if l.x1 <= fx0 + 2:
                left = max(left, l.x1 + 2)
            elif l.x0 >= fx1 - 2:
                right = min(right, l.x0 - 2)
    bottom = height
    for l in lines:
        if l.y0 >= fy1 - 2 and l.x1 > left and l.x0 < right:
            bottom = min(bottom, l.y0 - 2)
    return left, right, max(bottom, fy1)


def draw_items(items, lines, width: int, dx: int = 0, dy: int = 0,
               height: int = 1 << 30) -> list["DrawItem"]:
    """Window-relative analyzer Items -> screen-space DrawItems (offset by dx, dy)."""
    out = []
    for i in items:
        fx0, fy0, fx1, fy1 = i.fill
        l, r, b = free_space(i.fill, lines, width, height, widen=i.line_count == 1)
        out.append(DrawItem(dx + fx0, dy + fy0, dx + fx1, dy + fy1, i.ko, i.bg, i.fg,
                            i.line_count, i.font_px, i.art, dx + l, dx + r, dy + b))
    return out


@lru_cache(maxsize=4096)
def _font(family: str, px: int) -> QFont:
    f = QFont(family)
    f.setPixelSize(px)
    return f


def _wrapped_height(family: str, px: int, width: int, text: str) -> int:
    return QFontMetrics(_font(family, px)).boundingRect(
        QRect(0, 0, width, 100000), int(Qt.TextFlag.TextWordWrap), text).height()


@lru_cache(maxsize=4096)
def _fit_single(family: str, text: str, w: int, h: int, max_w: int, base_px: int,
                avail_h: int = 0):
    """-> (px, width, height, mode) with mode 'line' | 'wrap' | 'elide'.
    One line if it fits (shrinking to MIN_PX); otherwise wrap, using the box height
    plus any empty space below (avail_h); elide only when even that is too small."""
    avail_h = max(h, avail_h)
    px = max(MIN_PX, min(base_px, int(h * 0.9)))
    while True:
        need = QFontMetrics(_font(family, px)).horizontalAdvance(text) + 6
        if need <= max_w or px <= MIN_PX:
            break
        px -= 1
    if need <= max_w:
        return px, need, h, "line"
    start = max(MIN_PX, min(base_px, int(h * 0.9)))
    for wpx in range(start, MIN_PX - 1, -1):
        th = _wrapped_height(family, wpx, max_w - 4, text) + 4
        if th <= avail_h:
            return wpx, max_w, max(h, th), "wrap"
    return MIN_PX - 1, max_w, h, "elide"


@lru_cache(maxsize=2048)
def _fit_paragraph(family: str, text: str, w: int, h: int, lines: int, base_px: int,
                   avail_h: int = 0) -> tuple[int, int]:
    """-> (px, height). Shrinks to MIN_PX_PARAGRAPH, then grows into the empty space
    below (avail_h) rather than letting the end of the sentence spill out."""
    px = max(MIN_PX_PARAGRAPH, min(base_px or int(h / max(lines, 1) * 0.82),
                                   int(h / max(lines, 1) * 0.9)))
    floor = max(MIN_PX_PARAGRAPH, int(px * 0.8))  # shrink a little, then use space below
    while px > floor and _wrapped_height(family, px, w, text) > h + 2:
        px -= 1
    limit = max(h, avail_h)
    while px > MIN_PX_PARAGRAPH and _wrapped_height(family, px, w, text) + 2 > limit:
        px -= 1
    th = _wrapped_height(family, px, w, text) + 2
    # never cut it to the box: text taller than its plate is centred and loses its
    # first and last lines under the neighbouring items
    return px, max(h, th)


def _contrast_pen(c: tuple[int, int, int]) -> QColor:
    lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    return QColor(0, 0, 0, 220) if lum > 128 else QColor(255, 255, 255, 220)


def _draw_outlined(p: QPainter, rect: QRect, flags: int, text: str, font: QFont,
                   fg: tuple[int, int, int]) -> None:
    """Text with a 2px halo so it stays readable over artwork."""
    fm = QFontMetrics(font)
    br = fm.boundingRect(rect, flags, text)
    path = QPainterPath()
    # single line is all we outline (art labels/titles); place it at the centred baseline
    x = br.x()
    y = br.y() + fm.ascent()
    path.addText(QPointF(x, y), font, text)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.strokePath(path, QPen(_contrast_pen(fg), 3))
    p.fillPath(path, QBrush(QColor(*fg)))


def paint_items(p: QPainter, items: list[DrawItem], ox: int, oy: int,
                family: str, debug: bool = False) -> None:
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    for it in items:
        x, y = it.x0 - ox, it.y0 - oy
        w, h = max(1, it.x1 - it.x0), max(1, it.y1 - it.y0)
        base = it.font_px or int(h * 0.82)
        if it.line_count <= 1:
            room_l = (it.room_l if it.room_l >= 0 else it.x0) - ox
            room_r = (it.room_r if it.room_r >= 0 else it.x1) - ox
            room_l, room_r = min(room_l, x), max(room_r, x + w)
            # 1-2 character labels ("玩" -> "플레이") need more than a ratio allows
            grow = max(int(w * MAX_WIDEN), w + 3 * h)
            max_w = w if it.art else min(grow, room_r - room_l)
            avail_h = (it.room_b - oy - y) if it.room_b >= 0 and not it.art else h
            px, need, height, mode = _fit_single(family, it.ko, w, h, max(w, max_w), base,
                                                 avail_h)
            if mode == "wrap":   # long text (descriptions): start where the original did
                max_w = max(w, min(max_w, room_r - x))
                px, need, height, mode = _fit_single(family, it.ko, w, h, max_w, base,
                                                     avail_h)
            width = max(w, min(need, max_w))
            if mode == "wrap":
                left = max(room_l, min(x, room_r - width))
            else:
                left = x + w // 2 - width // 2              # centred on the original text
                left = max(room_l, min(left, room_r - width))  # ...shifted inward at edges
            rect = QRect(left, y, width, height)
            font = _font(family, px)
            text = it.ko
            if mode == "wrap":
                flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | \
                    Qt.TextFlag.TextWordWrap
            else:
                flags = Qt.AlignmentFlag.AlignCenter
                if mode == "elide":
                    text = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight,
                                                         width - 4)
        else:
            avail_h = (it.room_b - oy - y) if it.room_b >= 0 else h
            px, height = _fit_paragraph(family, it.ko, w, h, it.line_count, it.font_px,
                                        avail_h)
            mode = "paragraph"
            rect = QRect(x, y, w, height)
            font = _font(family, px)
            text = it.ko
            flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | \
                Qt.TextFlag.TextWordWrap
        if it.art:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            plate = QPainterPath()
            plate.addRoundedRect(QRectF(rect), 4, 4)
            p.fillPath(plate, QColor(*it.bg, ART_ALPHA))
        else:
            p.fillRect(rect, QColor(*it.bg))
        p.setFont(font)
        if it.art and it.line_count <= 1 and mode == "line":
            _draw_outlined(p, rect, int(flags), text, font, it.fg)
        else:
            p.setPen(QColor(*it.fg))
            p.drawText(rect, int(flags), text)
        if debug:
            p.setPen(QPen(QColor(255, 60, 60), 1))
            p.drawRect(rect.adjusted(0, 0, -1, -1))


class OverlayWindow(QWidget):
    def __init__(self, font_family: str, exclude_from_capture: bool = True,
                 debug: bool = False):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool
                         | Qt.WindowType.WindowTransparentForInput
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.font_family = font_family
        self.debug = debug
        self.items: list[DrawItem] = []
        self._exclude = exclude_from_capture
        self.capture_excluded = False

    def cover(self, rect: QRect) -> None:
        """Cover only this screen area (the union of the client windows)."""
        if rect != self.geometry():
            self.setGeometry(rect)

    def showEvent(self, event):  # noqa: N802 (Qt naming)
        super().showEvent(event)
        self.capture_excluded = win32.make_overlay_window(int(self.winId()), self._exclude)

    def set_items(self, items: list[DrawItem]) -> None:
        if items == self.items:
            return
        self.items = items
        self.update()

    def present(self, items: list[DrawItem]) -> None:
        """Show the window only when there is something to draw."""
        self.set_items(items)
        if items and not self.isVisible():
            self.show()
        elif not items and self.isVisible():
            self.hide()

    def park(self) -> None:
        """Nothing to show (client inactive, game in front): no window at all."""
        if self.isVisible():
            self.set_items([])
            self.hide()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        geo = self.geometry()
        paint_items(p, self.items, geo.x(), geo.y(), self.font_family, self.debug)
        p.end()
