"""Render the tray's '한' badge into a multi-size .ico for the exe and shortcut."""
import os
import sys
from pathlib import Path

# real Windows platform: the offscreen one has no system fonts (the glyph renders as a box)
from PIL import Image  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402

app = QGuiApplication(sys.argv)
size = 256
img = QImage(size, size, QImage.Format.Format_ARGB32)
img.fill(QColor(0, 0, 0, 0))
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setBrush(QColor(234, 74, 170))
p.setPen(QColor(0, 0, 0, 0))
p.drawRoundedRect(8, 8, size - 16, size - 16, 56, 56)
p.setPen(QColor(255, 255, 255))
f = QFont("Malgun Gothic")
f.setPixelSize(160)
f.setBold(True)
p.setFont(f)
p.drawText(img.rect(), int(Qt.AlignmentFlag.AlignCenter), "한")
p.end()
out = Path(__file__).with_name("icon.png")
img.save(str(out))
Image.open(out).save(out.with_suffix(".ico"),
                     sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written:", out.with_suffix(".ico"))
