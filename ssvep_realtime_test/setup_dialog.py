"""Settings and ruler calibration; neither screen emits a flashing stimulus."""
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QFont, QPainter
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)
from .config import Settings, parse_candidates, parse_channels, square_pixels
from .display import screen_key


def decimal(value, low, high, decimals=2):
    widget = QDoubleSpinBox()
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setValue(value)
    return widget


class CalibrationDialog(QDialog):
    def __init__(self, config, screen):
        super().__init__()
        self.config = Settings(**config.to_dict())
        self.target_screen = screen
        self.setWindowTitle("5 cm 标尺校准")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setGeometry(screen.geometry())

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        dpr = self.devicePixelRatioF()
        sx, sy = square_pixels(self.config, self.width() * dpr, self.height() * dpr)
        p.setPen(Qt.white)
        p.drawRect(QRectF((self.width() - sx / dpr) / 2, (self.height() - sy / dpr) / 2, sx / dpr, sy / dpr))
        p.setFont(QFont("Microsoft YaHei", 14))
        p.drawText(QRectF(30, 35, self.width() - 60, 180), Qt.AlignHCenter | Qt.TextWordWrap,
                   "用直尺测量白框边线中心间距，调至宽、高各 50 mm。\n"
                   "← / → 调宽；↓ / ↑ 调高；按住 Shift 加快。\n"
                   "Enter 保存本显示器校准；ESC 取消。此页面不会闪烁。\n"
                   f"当前 {sx} × {sy} 物理像素；系数 {self.config.scale_x:.3f} / {self.config.scale_y:.3f}")
        p.end()

    def keyPressEvent(self, event):
        key = event.key()
        step = 0.01 if event.modifiers() & Qt.ShiftModifier else 0.001
        if key in (Qt.Key_Left, Qt.Key_Right):
            self.config.scale_x = min(5, max(0.2, self.config.scale_x + (step if key == Qt.Key_Right else -step)))
        elif key in (Qt.Key_Up, Qt.Key_Down):
            self.config.scale_y = min(5, max(0.2, self.config.scale_y + (step if key == Qt.Key_Up else -step)))
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.config.calibrated = True
            self.config.screen_fingerprint = screen_key(self.target_screen)
            self.accept()
        elif key == Qt.Key_Escape:
            self.reject()
        self.update()


class SetupDialog(QDialog):
    def __init__(self, config, screens):
        super().__init__()
        self.config = Settings(**config.to_dict())
        self.screens = screens
        self.setWindowTitle("SSVEP 单目标实时测试")
        self.resize(720, 810)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        introduction = QLabel("中央 5 × 5 cm 黑白闪烁 · ESC 退出\nEEG 由 P300 Monitor 采集并通过本机 HTTP 转发。")
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        form = QFormLayout()
        layout.addLayout(form)
        self.screen = QComboBox()
        for i, item in enumerate(screens):
            g = item.geometry()
            dpr = item.devicePixelRatio()
            self.screen.addItem(f"{i + 1}: {item.name()}  {round(g.width() * dpr)}×{round(g.height() * dpr)}  系统 {item.refreshRate():g} Hz")
        self.screen.setCurrentIndex(min(config.screen_index, len(screens) - 1))
        form.addRow("刺激显示器", self.screen)
        self.frequency = decimal(config.frequency, 6, 80)
        form.addRow("初始刺激 / 切频中心 Hz", self.frequency)
        self.diagonal = decimal(config.diagonal_inches, 10, 100)
        form.addRow("屏幕对角线 / 英寸（16:9）", self.diagonal)
        self.refresh = decimal(config.expected_refresh, 20, 1000)
        form.addRow("预期刷新率 / Hz", self.refresh)
        self.candidates = QLineEdit(",".join(f"{x:g}" for x in config.candidates))
        self._custom_candidates_text = ",".join(f"{x:g}" for x in (config.legacy_candidates or config.candidates))
        self._displayed_profile = config.analysis_profile
        if tuple(config.candidates) == parse_candidates("8:30:0.5"):
            self.candidates.setText("8:30:0.5")
        self.candidates.setCursorPosition(0)
        self.candidates.setPlaceholderText("8:30:0.5 或 10,12,15,17,20")
        form.addRow("候选频率 / Hz", self.candidates)
        self.channels = QLineEdit(",".join(map(str, config.channels)))
        form.addRow("参与分析的通道（1–8）", self.channels)
        self.window = decimal(config.window_seconds, 1, 10)
        self.update_interval = decimal(config.update_seconds, 0.1, 2)
        form.addRow("滑动窗 / 秒", self.window)
        form.addRow("结果更新间隔 / 秒", self.update_interval)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(config.port)
        form.addRow("本机 HTTP 端口", self.port)
        self.notch = QCheckBox()
        self.notch.setChecked(config.notch)
        self.mask_165 = QCheckBox("屏蔽16.5 Hz排名（柱图保留真实ρ，仅供诊断）")
        self.mask_165.setChecked(config.exclude_165)
        self.profile = QComboBox()
        self.profile.addItem("MATLAB CCA · 6–90 Hz · 四谐波 · 逐窗零相位", "matlab")
        self.profile.addItem("6–45 Hz · 两谐波（刺激≤22 Hz）", "focused")
        self.profile.addItem("6–90 Hz · 三谐波（宽带对照）", "wide")
        self.profile.setCurrentIndex(self.profile.findData(config.analysis_profile))
        form.addRow("分析模式", self.profile)
        form.addRow("工频处理", self.notch)
        form.addRow("候选屏蔽", self.mask_165)
        self.fallback_range = decimal(config.fullscale_uv, 1000, 10_000_000, 0)
        form.addRow("旧 Monitor 回退满量程 / µV", self.fallback_range)
        self.do_calibration = QCheckBox("先采静态基线，再标定当前频率的通道")
        self.do_calibration.setChecked(config.calibrate_before_run)
        form.addRow("开始前标定", self.do_calibration)
        self.raw = QCheckBox("保存原始 EEG，供离线核对")
        self.raw.setChecked(config.record_raw)
        form.addRow("原始数据", self.raw)
        calibration = QPushButton("用直尺校准 5 × 5 cm（静态白框）")
        calibration.clicked.connect(self.calibrate)
        layout.addWidget(calibration)
        self.geometry_label = QLabel()
        self.geometry_label.setWordWrap(True)
        layout.addWidget(self.geometry_label)
        self.screen.currentIndexChanged.connect(self.update_geometry)
        self.diagonal.valueChanged.connect(self.update_geometry)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.port.valueChanged.connect(self.update_summary)
        self.window.valueChanged.connect(self.update_summary)
        self.profile.currentIndexChanged.connect(self.update_summary)
        self.frequency.valueChanged.connect(self.update_summary)
        self.mask_165.toggled.connect(self.update_summary)
        self.update_summary()
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        start = buttons.addButton("开始全屏测试", QDialogButtonBox.AcceptRole)
        start.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.update_geometry()

    def update_summary(self):
        matlab = self.profile.currentData() == "matlab"
        if matlab and self._displayed_profile != "matlab":
            self._custom_candidates_text = self.candidates.text()
        elif not matlab and self._displayed_profile == "matlab":
            self.candidates.setText(self._custom_candidates_text)
        self._displayed_profile = self.profile.currentData()
        self.candidates.setReadOnly(matlab)
        self.do_calibration.setEnabled(not matlab)
        self.notch.setText("49.5–50.5 Hz Butterworth二阶带阻（双向）" if matlab else "50 Hz 窄陷波（Q=35）")
        if matlab:
            center = self.frequency.value()
            self.candidates.setText(",".join(f"{center + .5 * k:g}" for k in range(-8, 9)))
        opening = (f"CCA分析随当前刺激频率±4 Hz，0.5 Hz步进，共17点；当前设置中心{self.frequency.value():g} Hz。\n"
                   f"←/→ 或点击底部频率条切换，Home回中心。过渡0.5秒，再积累{self.window.value():g}秒新窗。\n"
                   "MATLAB参考分类不要求静态标定；最大相关系数不是可靠检出或概率。\n") if matlab else (
                   f"静态基线约 {self.window.value() * self.config.baseline_windows:g} 秒，刺激标定约 {self.window.value() * self.config.response_windows:g} 秒；随后持续测试。\n")
        if self.mask_165.isChecked() and self.frequency.value() == 16.5:
            opening += "注意：当前刺激16.5 Hz被屏蔽，不能参与目标识别。\n"
        controls = "B显示CCA柱图，D质量/频谱，数字1–8切通道，ESC退出。\n"
        ending = "实际量程优先使用Monitor元信息。" if matlab else "R重新标定；未标定时仅显示候选，实际量程优先使用Monitor元信息。"
        self.note.setText(opening + f"点击开始后监听：127.0.0.1:{self.port.value()}/api/bci/data。\n" + controls + ending)

    def update_geometry(self):
        screen = self.screens[self.screen.currentIndex()]
        if self.config.screen_fingerprint != screen_key(screen) or self.config.diagonal_inches != self.diagonal.value():
            self.config.calibrated = False
            self.config.scale_x = self.config.scale_y = 1.0
        self.config.diagonal_inches = self.diagonal.value()
        g, dpr = screen.geometry(), screen.devicePixelRatio()
        sx, sy = square_pixels(self.config, g.width() * dpr, g.height() * dpr)
        self.geometry_label.setText(f"当前 50 mm 对应约 {sx} × {sy} 物理像素；" + ("此显示器已校准" if self.config.calibrated else "尚未尺校准"))

    def calibrate(self):
        dialog = CalibrationDialog(self.config, self.screens[self.screen.currentIndex()])
        dialog.winId()
        dialog.windowHandle().setScreen(dialog.target_screen)
        dialog.showFullScreen()
        if dialog.exec_() == QDialog.Accepted:
            self.config = dialog.config
            self.update_geometry()

    def values(self):
        config = Settings(**self.config.to_dict())
        config.frequency = self.frequency.value()
        config.frequency_center = config.frequency
        config.candidates = parse_candidates(self.candidates.text())
        config.legacy_candidates = parse_candidates(self._custom_candidates_text if self.profile.currentData() == "matlab" else self.candidates.text())
        config.channels = parse_channels(self.channels.text())
        config.screen_index = self.screen.currentIndex()
        config.diagonal_inches = self.diagonal.value()
        config.expected_refresh = self.refresh.value()
        config.window_seconds = self.window.value()
        config.update_seconds = self.update_interval.value()
        config.port = self.port.value()
        config.notch = self.notch.isChecked()
        config.analysis_profile = self.profile.currentData()
        config.harmonics = {"matlab": 4, "focused": 2, "wide": 3}[config.analysis_profile]
        config.fullscale_uv = self.fallback_range.value()
        config.calibrate_before_run = self.do_calibration.isChecked()
        config.record_raw = self.raw.isChecked()
        config.exclude_165 = self.mask_165.isChecked()
        return config.validate()

    def accept(self):
        try:
            self.config = self.values()
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "设置无效", str(exc))
            return
        super().accept()
