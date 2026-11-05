"""Stand-in window for manual/live testing of the overlay (not a pytest test).

    uv run python tests/fake_client.py
    uv run pwa-overlay --target python.exe --title PWA-FAKE-CLIENT --always --debug
"""
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

PAGES = [
    ["平台设置", "我的战绩", "退出平台", "是否将平台最小化到托盘?", "不再提醒"],
    ["关闭", "确认", "取消", "继续", "比赛结束", "这一行不在词典里"],
]

app = QApplication(sys.argv)
w = QWidget()
w.setWindowTitle("PWA-FAKE-CLIENT")
w.setStyleSheet("QWidget{background:#1e2128;color:#e1e1e1;font-family:'Microsoft YaHei';"
                "font-size:18px} QPushButton{background:#3a6df0;border:0;padding:8px 18px}")
lay = QVBoxLayout(w)
widgets = []
state = {"page": 0, "ticks": 0}


def show_page(i):
    for x in widgets:
        x.deleteLater()
    widgets.clear()
    for j, text in enumerate(PAGES[i]):
        x = QPushButton(text) if j % 2 else QLabel(text)
        lay.addWidget(x)
        widgets.append(x)


def step():
    state["ticks"] += 1
    if state["ticks"] % 2 == 0:
        state["page"] ^= 1
        show_page(state["page"])
    else:
        w.move(w.x() + 120, w.y() + 60)  # test window tracking


show_page(0)
w.resize(520, 420)
w.move(200, 150)
w.show()
w.raise_()
w.activateWindow()
if "--static" not in sys.argv:
    QTimer.singleShot(0, lambda: None)
    t = QTimer()
    t.timeout.connect(step)
    t.start(5000)
QTimer.singleShot(int(sys.argv[sys.argv.index("--quit") + 1]) * 1000 if "--quit" in sys.argv
                  else 10**9, app.quit)
sys.exit(app.exec())
