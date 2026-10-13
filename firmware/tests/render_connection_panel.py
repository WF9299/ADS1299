"""Render the Monitor's new page for visual QA, without real device/network operations."""
import os
import sys
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "windows" if sys.platform == "win32" else "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.p300_monitor import MonitorWindow, QtWidgets, QtCore

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
window = MonitorWindow("0.0.0.0", 1234, auto_start=False)
window.resize(1760, 1080)
window.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
window.tabs.setCurrentIndex(5)
window.show()
app.processEvents()
target = Path(__file__).resolve().parents[1] / "build" / "qa" / "monitor_connection.png"
target.parent.mkdir(parents=True, exist_ok=True)
window.grab().save(str(target))
print(target)
window.close()
