"""P300 TCP EEG monitor with waveforms, FFT, impedance, and recording tools."""

from __future__ import annotations

import argparse
import json
import queue
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.p300_common import (
    CHANNELS,
    BatchStreamParser,
    EegBatch,
    batch_to_record,
    export_jsonl_to_csv,
    export_jsonl_to_json,
)
from tools.p300_impedance import (
    ImpedanceCalibration,
    ImpedanceClient,
    ImpedanceFlags,
    ImpedanceModelStatus,
    ImpedanceResultData,
    ImpedanceStatus,
    build_impedance_report,
)
from tools.p300_http_push import (
    HttpJsonSender,
    HttpPacketAggregator,
    HttpPushSettings,
)
from tools.p300_filters import (
    FILTER_MODE_LABELS,
    SCIPY_AVAILABLE,
    FilterMode,
    FilterUnavailableError,
    RealtimeEegFilter,
)
from tools.p300_self_test import (
    CONFIG_INTERNAL_SHORT,
    CONFIG_NORMAL,
    InternalShortAnalyzer,
    SelfTestClient,
    SelfTestGrade,
    SelfTestResultData,
    SelfTestStatus,
)
from tools.p300_bias import (
    BiasControlClient,
    BiasFlags,
    BiasResultData,
    BiasSetOutcome,
    BiasStatus,
    recommend_bias_mask,
)
from tools.p300_display_reference import (
    DisplayReferenceProcessor,
    ReferenceMode,
)
from tools.p300_signal_quality import (
    DisplaySignalQualityAnalyzer,
    QualityGrade,
    SignalQualityAnalyzer,
    mains_suppression_db,
)
from tools.p300_serial import SerialReceiver
from tools.p300_discovery import DiscoveryServer
from tools.p300_tx_control import TxControlClient, TxResultData, TxSetOutcome, TxStatus

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg
except ImportError:  # Keep --help and the protocol/data model importable without GUI extras.
    QtCore = QtGui = QtWidgets = pg = None


DEFAULT_IMPEDANCE_PORT = 1235
DEFAULT_SELF_TEST_PORT = 1235
DEFAULT_BIAS_PORT = 1235
DEFAULT_CALIBRATION_PATH = Path(__file__).with_name("p300_impedance_calibration.json")
DEFAULT_HTTP_SETTINGS_PATH = Path(__file__).with_name("p300_http_push_settings.json")
CHANNEL_COLORS = (
    "#ff5050", "#50ff50", "#50a0ff", "#ffff50",
    "#50ffff", "#ff50ff", "#ff9a50", "#80ff80",
)
DISPLAY_DC_SECONDS = 2.0
DISPLAY_MIN_HALF_RANGE_UV = 5.0
UI_UPDATE_MS = 50


def is_recordable_batch(batch: EegBatch) -> bool:
    """Only NORMAL EEG batches may enter the formal JSONL recording."""
    return batch.config_id == CONFIG_NORMAL


def apply_marker_override(batch: EegBatch, enabled: bool) -> EegBatch:
    """Return a host-output batch whose NORMAL EEG markers are all one."""
    if not enabled or batch.config_id != CONFIG_NORMAL:
        return batch
    return EegBatch(
        stream_id=batch.stream_id,
        config_id=batch.config_id,
        start_seq=batch.start_seq,
        timestamp_us=batch.timestamp_us,
        samples=batch.samples,
        markers=np.ones_like(batch.markers),
    )


class MonitorDataModel:
    """GUI-independent rolling data store and delivery statistics."""

    def __init__(self, history_seconds: float = 5.0) -> None:
        self.history_seconds = float(history_seconds)
        self.config: dict = {"sampling_rate": 1000, "channels": CHANNELS}
        self.history_samples = max(1, int(self.history_seconds * 1000))
        self._times: deque[float] = deque(maxlen=self.history_samples)
        self._values: deque[np.ndarray] = deque(maxlen=self.history_samples)
        self.marker_events: deque[tuple[float, int]] = deque(maxlen=512)
        self.last_stream_id: int | None = None
        self.last_start_seq: int | None = None
        self.lost_samples = 0
        self.stream_interruptions = 0
        self.received_samples = 0
        self.received_batches = 0
        self.last_marker_code = 0
        self.last_record: dict | None = None
        self.last_batch_monotonic: float | None = None
        self.last_config_id: int | None = None

    @property
    def sampling_rate(self) -> int:
        return int(self.config.get("sampling_rate", self.config.get("sample_rate_hz", 1000)))

    def set_config(self, config: dict) -> None:
        self.config = {**self.config, **config}
        self.config["sampling_rate"] = max(1, self.sampling_rate)
        self._resize_history()

    def set_history_seconds(self, seconds: float) -> None:
        self.history_seconds = max(1.0, float(seconds))
        self._resize_history()

    def _resize_history(self) -> None:
        self.history_samples = max(1, int(self.history_seconds * self.sampling_rate))
        self._times = deque(self._times, maxlen=self.history_samples)
        self._values = deque(self._values, maxlen=self.history_samples)

    def ingest(
        self,
        batch: EegBatch,
        *,
        display_values: np.ndarray | None = None,
        record: dict | None = None,
    ) -> dict:
        if self.last_config_id is not None and self.last_config_id != batch.config_id:
            self._times.clear()
            self._values.clear()
            self.marker_events.clear()
            self.last_marker_code = 0
        self.last_config_id = batch.config_id
        if self.last_stream_id != batch.stream_id:
            if self.last_stream_id is not None:
                self.stream_interruptions += 1
            self.last_stream_id = batch.stream_id
            self.last_start_seq = None
        if self.last_start_seq is not None:
            expected = (self.last_start_seq + batch.samples.shape[0]) & 0xFFFFFFFF
            gap = (batch.start_seq - expected) & 0xFFFFFFFF
            if 0 < gap < 0x80000000:
                self.lost_samples += gap
        self.last_start_seq = batch.start_seq

        if record is None:
            record = batch_to_record(batch, self.config)
        raw_values = np.asarray(record["data"], dtype=np.float64)
        plotted_values = raw_values if display_values is None else np.asarray(
            display_values, dtype=np.float64
        )
        if plotted_values.shape != raw_values.shape:
            raise ValueError(
                f"display_values shape {plotted_values.shape} does not match {raw_values.shape}"
            )
        if not np.all(np.isfinite(plotted_values)):
            raise ValueError("display_values contain NaN or Inf")
        rate = record["sampling_rate"]
        for index, (values, marker) in enumerate(
            zip(plotted_values, record["markers"], strict=True)
        ):
            timestamp = record["timestamp"] + index / rate
            self._times.append(timestamp)
            self._values.append(np.asarray(values, dtype=np.float64))
            if marker:
                self.marker_events.append((timestamp, int(marker)))
                self.last_marker_code = int(marker)
        self.received_samples += len(record["data"])
        self.received_batches += 1
        self.last_record = record
        self.last_batch_monotonic = time.monotonic()
        return record

    def clear_display_history(self, *, preserve_markers: bool = False) -> None:
        self._times.clear()
        self._values.clear()
        if not preserve_markers:
            self.marker_events.clear()
            self.last_marker_code = 0

    def plot_data(self, window_seconds: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        if not self._values:
            return np.empty(0), np.empty((0, CHANNELS))
        times = np.asarray(self._times)
        values = np.vstack(self._values)
        if window_seconds is not None and len(times):
            keep = times >= times[-1] - float(window_seconds)
            times, values = times[keep], values[keep]
        return times, values

    def reset_statistics(self) -> None:
        self.lost_samples = 0
        self.stream_interruptions = 0
        self.received_samples = 0
        self.received_batches = 0


class JsonlWriter(threading.Thread):
    def __init__(self, path: Path) -> None:
        super().__init__(name="p300-jsonl-writer", daemon=True)
        self.path = path
        self.queue: queue.Queue[dict | None] = queue.Queue(maxsize=2000)
        self.dropped = 0
        self.written = 0

    def submit(self, record: dict) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        self.queue.put(None)
        self.join(timeout=3.0)

    def run(self) -> None:
        with self.path.open("a", encoding="utf-8") as output:
            while True:
                record = self.queue.get()
                if record is None:
                    output.flush()
                    return
                output.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
                self.written += 1


class TcpReceiver(threading.Thread):
    """Single-client TCP server that emits structured state, peer, config, and batch events."""

    def __init__(self, host: str, port: int, events: queue.Queue) -> None:
        super().__init__(name="p300-tcp-receiver", daemon=True)
        self.host, self.port, self.events = host, int(port), events
        self._stop_event = threading.Event()
        self.connection: socket.socket | None = None
        self.server: socket.socket | None = None
        self.bound_port: int | None = None
        self.dropped_events = 0
        self.parser_errors = 0
        self.event_epoch = None

    def stop(self) -> None:
        self._stop_event.set()
        for sock in (self.connection, self.server):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def _emit(self, event: tuple) -> None:
        if self.event_epoch is not None:
            event = ("receiver_epoch", self.event_epoch, event)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1

    def _state(self, state: str, message: str, **extra: object) -> None:
        self._emit(("receiver", {"state": state, "message": message, **extra}))

    def run(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                self.server = server
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, self.port))
                server.listen(1)
                server.settimeout(0.5)
                self.bound_port = int(server.getsockname()[1])
                self._state("listening", f"监听 {self.host}:{self.bound_port}", port=self.bound_port)
                while not self._stop_event.is_set():
                    try:
                        connection, address = server.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    self.connection = connection
                    self._emit(("peer", {"connected": True, "host": address[0], "port": address[1]}))
                    self._state("connected", f"已连接 {address[0]}:{address[1]}")
                    parser = BatchStreamParser()
                    config_emitted = False
                    try:
                        with connection:
                            connection.settimeout(0.5)
                            while not self._stop_event.is_set():
                                try:
                                    chunk = connection.recv(4096)
                                except TimeoutError:
                                    continue
                                if not chunk:
                                    break
                                batches = parser.feed(chunk)
                                if parser.config is not None and not config_emitted:
                                    self._emit(("config", parser.config))
                                    config_emitted = True
                                for batch in batches:
                                    self._emit(("batch", parser.config or {}, batch))
                    except ValueError as error:
                        self.parser_errors += 1
                        self._state("error", f"数据帧错误: {error}")
                    except OSError as error:
                        if not self._stop_event.is_set():
                            self._state("error", f"连接错误: {error}")
                    finally:
                        self.connection = None
                        self._emit(("peer", {"connected": False, "host": address[0], "port": address[1]}))
                        if not self._stop_event.is_set():
                            self._state("listening", f"连接断开，继续监听 {self.host}:{self.bound_port}")
        except OSError as error:
            if not self._stop_event.is_set():
                self._state("error", f"TCP服务启动失败: {error}")
        finally:
            self.connection = None
            self.server = None
            self._emit(("receiver", {"state": "stopped", "message": "监听已停止"}))


def _display_dc_removed(values: np.ndarray, rate: int) -> np.ndarray:
    """Remove a two-second rolling mean for display only."""
    if not len(values):
        return values
    width = max(1, int(DISPLAY_DC_SECONDS * rate))
    cumulative = np.vstack((np.zeros((1, values.shape[1])), np.cumsum(values, axis=0)))
    end = np.arange(1, len(values) + 1)
    start = np.maximum(0, end - width)
    baseline = (cumulative[end] - cumulative[start]) / (end - start)[:, None]
    return values - baseline


if QtWidgets is not None:
    pg.setConfigOptions(antialias=False, background="k", foreground="w")

    class MonitorWindow(QtWidgets.QMainWindow):
        def __init__(
            self,
            host: str,
            port: int,
            impedance_port: int = DEFAULT_IMPEDANCE_PORT,
            calibration_path: Path | str = DEFAULT_CALIBRATION_PATH,
            http_settings_path: Path | str = DEFAULT_HTTP_SETTINGS_PATH,
            *,
            auto_start: bool = True,
            discovery_port: int = 1236,
        ) -> None:
            super().__init__()
            self.host = host
            self.port = int(port)
            self.discovery_port = int(discovery_port)
            if not 1 <= self.discovery_port <= 65535:
                raise ValueError("discovery_port must be in 1..65535")
            self.impedance_port = int(impedance_port)
            self.calibration_path = Path(calibration_path)
            self.http_settings_path = Path(http_settings_path)
            self.calibration, self.calibration_warning = ImpedanceCalibration.load_or_default(
                self.calibration_path
            )
            self.http_settings, self.http_settings_warning = HttpPushSettings.load_or_default(
                self.http_settings_path
            )
            self.model = MonitorDataModel(history_seconds=5.0)
            self.display_filter = RealtimeEegFilter(FilterMode.RAW, CHANNELS)
            self.display_reference = DisplayReferenceProcessor()
            self.quality_analyzer = SignalQualityAnalyzer()
            self.display_quality_analyzer = DisplaySignalQualityAnalyzer()
            self._last_quality_snapshot = None
            self._last_display_quality_snapshot = None
            self.filter_error: str | None = None
            self.events: queue.Queue = queue.Queue(maxsize=4000)
            self.receiver: TcpReceiver | None = None
            self.data_transport = "wifi"
            self.discovery = None
            self.active_receiver_epoch = None
            self.tx_client = None
            self.tx_actual_frames = None
            self.tx_pending = False
            self.tx_last_request_id = None
            self.writer: JsonlWriter | None = None
            self.current_record_path: Path | None = None
            self.force_markers_enabled = False
            self.http_aggregator = HttpPacketAggregator()
            self.http_sender: HttpJsonSender | None = None
            self.impedance_client: ImpedanceClient | None = None
            self.self_test_client: SelfTestClient | None = None
            self.bias_client: BiasControlClient | None = None
            self.bias_actual_mask: int | None = None
            self.bias_draft_mask = 0xFF
            self.bias_pending = False
            self.bias_last_request_id: int | None = None
            self.last_impedance_result: ImpedanceResultData | None = None
            self.peer_ip: str | None = None
            self.peer_port: int | None = None
            self.connected = False
            self.has_valid_eeg = False
            self.impedance_active = False
            self.self_test_active = False
            self.self_test_pending = False
            self.self_test_pending_target: int | None = None
            self.self_test_analyzer = InternalShortAnalyzer()
            self.restore_failed = False
            self.crc_errors = 0
            self._last_status_time = time.monotonic()
            self._last_status_samples = 0
            self._last_self_test_refresh = 0.0
            self._last_quality_refresh = 0.0
            self._sps = 0.0
            self._splitter_initialized = False
            self.marker_lines: list[list] = [[] for _ in range(CHANNELS)]
            self.pause_labels: list = []

            self.setWindowTitle("ESP32-C5 ADS1299 P300 Monitor")
            self.resize(1760, 980)
            self.setMinimumSize(1500, 860)
            self.setStyleSheet(
                "QWidget { font-family: 'Microsoft YaHei UI'; font-size: 12pt; }"
                "QPushButton { min-height: 30px; padding: 4px 8px; }"
                "QGroupBox { font-weight: bold; margin-top: 8px; }"
                "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
                "QHeaderView::section { font-size: 11pt; font-weight: bold; padding: 5px; }"
                "QToolTip { font-size: 12pt; }"
            )
            self._build_ui()
            self._set_state("stopped", "监听尚未启动")
            if self.http_settings_warning:
                self.http_status.setText(
                    f"HTTP配置异常，已恢复默认值：{self.http_settings_warning}"
                )
                self.http_status.setStyleSheet("color:#d9a441")
            if self.calibration_warning:
                if self.calibration.load_warning:
                    self.calibration_status.setText(
                        f"旧版未校准文件已按4.99kΩ/4.7nF模型升级，建议另存version 2：{self.calibration_warning}"
                    )
                    self.calibration_status.setStyleSheet("color:#d9a441")
                else:
                    self.calibration_status.setText(f"校准文件异常，已使用version 2默认值：{self.calibration_warning}")
                    self.calibration_status.setStyleSheet("color:#ff6b6b")
            if auto_start:
                self._start_receiver()
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._drain_events)
            self.timer.start(UI_UPDATE_MS)

        def showEvent(self, event) -> None:
            super().showEvent(event)
            if self._splitter_initialized:
                return
            total = max(1, self.main_splitter.width())
            side_width = min(700, max(620, int(total * 0.40)))
            self.main_splitter.setSizes([max(1, total - side_width), side_width])
            self._splitter_initialized = True

        def _build_ui(self) -> None:
            central = QtWidgets.QWidget()
            root = QtWidgets.QHBoxLayout(central)
            root.setContentsMargins(8, 8, 8, 8)
            root.setSpacing(10)
            self.setCentralWidget(central)
            self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            self.main_splitter.setChildrenCollapsible(False)
            root.addWidget(self.main_splitter)

            wave_container = QtWidgets.QWidget()
            wave_layout = QtWidgets.QVBoxLayout(wave_container)
            wave_layout.setContentsMargins(0, 0, 0, 0)
            wave_layout.setSpacing(4)
            self.main_splitter.addWidget(wave_container)

            toolbar = QtWidgets.QHBoxLayout()
            toolbar.addWidget(QtWidgets.QLabel("显示窗口"))
            self.window_combo = QtWidgets.QComboBox()
            for seconds in (1, 2, 5):
                self.window_combo.addItem(f"{seconds} 秒", seconds)
            self.window_combo.setCurrentIndex(1)
            self.window_combo.currentIndexChanged.connect(self._change_window)
            toolbar.addWidget(self.window_combo)
            toolbar.addSpacing(8)
            toolbar.addWidget(QtWidgets.QLabel("显示/FFT滤波"))
            self.filter_combo = QtWidgets.QComboBox()
            for mode in FilterMode:
                self.filter_combo.addItem(FILTER_MODE_LABELS[mode], mode.value)
            if not SCIPY_AVAILABLE:
                for index in range(1, self.filter_combo.count()):
                    item = self.filter_combo.model().item(index)
                    if item is not None:
                        item.setEnabled(False)
                        item.setToolTip("需要安装SciPy")
                self.filter_combo.setToolTip(
                    "SciPy不可用；请安装requirements-p300-tools.txt后启用滤波"
                )
            self.filter_combo.currentIndexChanged.connect(self._change_filter_mode)
            toolbar.addWidget(self.filter_combo)
            self.filter_status = QtWidgets.QLabel()
            toolbar.addWidget(self.filter_status)
            toolbar.addSpacing(8)
            toolbar.addWidget(QtWidgets.QLabel("显示参考"))
            self.reference_combo = QtWidgets.QComboBox()
            self.reference_combo.addItem("原始参考", ReferenceMode.RAW.value)
            self.reference_combo.addItem("CAR公共平均", ReferenceMode.CAR.value)
            self.reference_combo.addItem("中位数参考", ReferenceMode.MEDIAN.value)
            self.reference_combo.currentIndexChanged.connect(self._change_reference_mode)
            toolbar.addWidget(self.reference_combo)
            toolbar.addSpacing(12)
            self.force_marker_button = QtWidgets.QPushButton("Marker覆盖：关闭")
            self.force_marker_button.setCheckable(True)
            self.force_marker_button.setToolTip(
                "开启后，仅将上位机后续NORMAL EEG批次的Marker全部覆盖为1"
            )
            self.force_marker_button.toggled.connect(self._set_force_markers_enabled)
            toolbar.addWidget(self.force_marker_button)
            self.force_marker_hint = QtWidgets.QLabel("仅上位机覆盖，固件原始Marker未修改")
            self.force_marker_hint.setStyleSheet("color:#d58a00; font-weight:bold")
            self.force_marker_hint.setVisible(False)
            toolbar.addWidget(self.force_marker_hint)
            toolbar.addStretch(1)
            wave_layout.addLayout(toolbar)

            self.self_test_banner = QtWidgets.QLabel(
                "ADS1299 内部短接自检：显示/FFT未滤波，当前数据不代表人体EEG；正式记录/阻抗/Marker已禁用"
            )
            self.self_test_banner.setAlignment(QtCore.Qt.AlignCenter)
            self.self_test_banner.setStyleSheet(
                "QLabel { color:#201600; background:#f0ad36; font-weight:bold; padding:7px; border-radius:4px; }"
            )
            self.self_test_banner.setVisible(False)
            wave_layout.addWidget(self.self_test_banner)
            self._update_filter_status()

            self.plots, self.curves = [], []
            plot_font = QtGui.QFont()
            plot_font.setFamily("Microsoft YaHei UI")
            plot_font.setPointSize(10)
            for channel in range(CHANNELS):
                plot = pg.PlotWidget()
                plot.setTitle(f"CH{channel + 1} (µV, DC removed)", size="11pt")
                plot.showGrid(x=True, y=True, alpha=0.25)
                plot.setMouseEnabled(x=False, y=False)
                plot.setMenuEnabled(False)
                for axis_name in ("left", "bottom"):
                    plot.getAxis(axis_name).setTickFont(plot_font)
                if channel != CHANNELS - 1:
                    plot.hideAxis("bottom")
                else:
                    plot.setLabel(
                        "bottom", "相对时间", units="s", **{"font-size": "11pt"}
                    )
                curve = plot.plot(pen=pg.mkPen(CHANNEL_COLORS[channel], width=1.2))
                curve.setClipToView(True)
                curve.setDownsampling(auto=True, method="peak")
                pause = pg.TextItem("EEG 暂停 / 阻抗检测", color="#ffd166", anchor=(0.5, 0.5))
                pause.setVisible(False)
                plot.addItem(pause)
                self.pause_labels.append(pause)
                self.plots.append(plot)
                self.curves.append(curve)
                wave_layout.addWidget(plot, 1)

            self.side_panel = QtWidgets.QWidget()
            self.side_panel.setMinimumWidth(680)
            side_layout = QtWidgets.QVBoxLayout(self.side_panel)
            side_layout.setContentsMargins(0, 0, 0, 0)
            self.main_splitter.addWidget(self.side_panel)
            self.main_splitter.setStretchFactor(0, 1)
            self.main_splitter.setStretchFactor(1, 0)
            self.main_splitter.setSizes([1060, 700])

            connection = QtWidgets.QGroupBox("EEG 输入：TCP / USB 串口")
            connection_layout = QtWidgets.QGridLayout(connection)
            self.bind_edit = QtWidgets.QLineEdit(self.host)
            self.port_edit = QtWidgets.QSpinBox()
            self.port_edit.setRange(0, 65535)
            self.port_edit.setValue(self.port)
            self.listen_button = QtWidgets.QPushButton("开始监听")
            self.listen_button.clicked.connect(self._toggle_receiver)
            self.remote_label = QtWidgets.QLabel("远端：--")
            self.device_label = QtWidgets.QLabel("设备：--")
            connection_layout.addWidget(QtWidgets.QLabel("Bind"), 0, 0)
            connection_layout.addWidget(self.bind_edit, 0, 1)
            connection_layout.addWidget(QtWidgets.QLabel("端口"), 0, 2)
            connection_layout.addWidget(self.port_edit, 0, 3)
            connection_layout.addWidget(self.listen_button, 1, 0, 1, 2)
            connection_layout.addWidget(self.remote_label, 1, 2, 1, 2)
            connection_layout.addWidget(self.device_label, 2, 0, 1, 4)
            self.tx_control_button = QtWidgets.QPushButton("发送分组：重新查询")
            self.tx_control_button.clicked.connect(self._toggle_tx_control)
            self.tx_control_status = QtWidgets.QLabel("发送分组未确认")
            self.tx_control_status.setWordWrap(True)
            connection_layout.addWidget(self.tx_control_button, 3, 0, 1, 2)
            connection_layout.addWidget(self.tx_control_status, 3, 2, 1, 2)
            self._update_tx_control()
            side_layout.addWidget(connection)

            self.big_status = QtWidgets.QLabel("STOPPED")
            self.big_status.setAlignment(QtCore.Qt.AlignCenter)
            status_font = QtGui.QFont()
            status_font.setPointSize(15)
            status_font.setBold(True)
            self.big_status.setFont(status_font)
            self.detail_status = QtWidgets.QLabel("--")
            self.detail_status.setWordWrap(True)
            side_layout.addWidget(self.big_status)
            side_layout.addWidget(self.detail_status)

            metrics = QtWidgets.QGroupBox("链路与采集诊断")
            metrics_layout = QtWidgets.QGridLayout(metrics)
            self.metric_labels: dict[str, QtWidgets.QLabel] = {}
            metric_names = (
                ("sps", "实际 SPS"), ("age", "数据年龄"),
                ("lost", "序号丢样"), ("crc", "帧错误"),
                ("stream", "stream_id"), ("interrupt", "有意中断"),
                ("marker", "最近 Marker"), ("queue", "事件队列"),
                ("tx", "发送分组"),
            )
            for index, (key, title) in enumerate(metric_names):
                row, column = divmod(index, 2)
                label = QtWidgets.QLabel("--")
                label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                metrics_layout.addWidget(QtWidgets.QLabel(title), row, column * 2)
                metrics_layout.addWidget(label, row, column * 2 + 1)
                self.metric_labels[key] = label
            side_layout.addWidget(metrics)

            self.tabs = QtWidgets.QTabWidget()
            self.tabs.addTab(self._build_fft_tab(), "频谱")
            self.tabs.addTab(self._build_impedance_tab(), "阻抗")
            self.tabs.addTab(self._build_self_test_tab(), "内部短接")
            self.tabs.addTab(self._build_record_tab(), "记录")
            self.tabs.addTab(self._build_quality_tab(), "质量")
            from tools.p300_connection_ui import ConnectionPanel
            self.network_panel = ConnectionPanel(self)
            connection_scroll = QtWidgets.QScrollArea()
            connection_scroll.setWidgetResizable(True)
            connection_scroll.setWidget(self.network_panel)
            self.tabs.addTab(connection_scroll, "连接/配网")
            side_layout.addWidget(self.tabs, 1)

        def _build_fft_tab(self) -> QtWidgets.QWidget:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("通道"))
            self.fft_channel = QtWidgets.QComboBox()
            self.fft_channel.addItems([f"CH{index + 1}" for index in range(CHANNELS)])
            row.addWidget(self.fft_channel)
            self.fft_peak = QtWidgets.QLabel("主峰：--")
            row.addWidget(self.fft_peak)
            row.addStretch(1)
            layout.addLayout(row)
            self.spectrum = pg.PlotWidget(title="0–60 Hz FFT")
            self.spectrum.setTitle("0–60 Hz FFT", size="12pt")
            self.spectrum.setLabel(
                "bottom", "Frequency", units="Hz", **{"font-size": "11pt"}
            )
            self.spectrum.setLabel(
                "left", "Magnitude", units="µV", **{"font-size": "11pt"}
            )
            spectrum_font = QtGui.QFont()
            spectrum_font.setFamily("Microsoft YaHei UI")
            spectrum_font.setPointSize(10)
            self.spectrum.getAxis("bottom").setTickFont(spectrum_font)
            self.spectrum.getAxis("left").setTickFont(spectrum_font)
            self.spectrum.showGrid(x=True, y=True, alpha=0.25)
            self.spectrum_curve = self.spectrum.plot(pen=pg.mkPen("#f0bc3d", width=2))
            layout.addWidget(self.spectrum, 1)
            self.latest_values = QtWidgets.QLabel("CH1–CH8：--")
            self.latest_values.setWordWrap(True)
            layout.addWidget(self.latest_values)
            return tab

        def _build_impedance_tab(self) -> QtWidgets.QWidget:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            row = QtWidgets.QHBoxLayout()
            self.impedance_button = QtWidgets.QPushButton("检测一次（8通道）")
            self.impedance_button.setEnabled(False)
            self.impedance_button.clicked.connect(self._start_impedance)
            self.impedance_save_result = QtWidgets.QPushButton("保存结果 JSON")
            self.impedance_save_result.setEnabled(False)
            self.impedance_save_result.clicked.connect(self._save_impedance_result)
            row.addWidget(self.impedance_button)
            row.addWidget(self.impedance_save_result)
            layout.addLayout(row)
            self.impedance_status = QtWidgets.QLabel("等待有效EEG连接")
            self.impedance_status.setWordWrap(True)
            layout.addWidget(self.impedance_status)

            self.impedance_table = QtWidgets.QTableWidget(CHANNELS, 6)
            self.impedance_table.setHorizontalHeaderLabels(
                ["通道", "状态", "电极 kΩ", "原始 kΩ", "31.25Hz µV", "校准"]
            )
            self.impedance_table.verticalHeader().setVisible(False)
            self.impedance_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.impedance_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.impedance_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
            for channel in range(CHANNELS):
                self._set_table_item(channel, 0, f"CH{channel + 1}")
                for column in range(1, 6):
                    self._set_table_item(channel, column, "--")
            layout.addWidget(self.impedance_table, 1)

            calibration_group = QtWidgets.QGroupBox("两点校准：板载4.99 kΩ不更换")
            self.calibration_group = calibration_group
            calibration_layout = QtWidgets.QGridLayout(calibration_group)
            self.calibration_channel = QtWidgets.QComboBox()
            self.calibration_channel.addItems([f"CH{index + 1}" for index in range(CHANNELS)])
            self.calibration_low = QtWidgets.QDoubleSpinBox()
            self.calibration_high = QtWidgets.QDoubleSpinBox()
            for spin, value in ((self.calibration_low, 0.0), (self.calibration_high, 0.0)):
                spin.setRange(0.0, 10000.0)
                spin.setDecimals(3)
                spin.setValue(value)
                spin.setSuffix(" kΩ")
                spin.setSpecialValueText("未测量")
                spin.setToolTip("填入接入对应已知电阻后，固件返回的原始kΩ值")
            self.apply_calibration_button = QtWidgets.QPushButton("应用10k/47k两点校准")
            self.apply_calibration_button.clicked.connect(self._apply_two_point_calibration)
            self.apply_calibration_button.setEnabled(False)
            def update_calibration_inputs():
                self.apply_calibration_button.setEnabled(self.calibration_high.value()>self.calibration_low.value()>0)
            self.calibration_low.valueChanged.connect(update_calibration_inputs)
            self.calibration_high.valueChanged.connect(update_calibration_inputs)
            self.load_calibration_button = QtWidgets.QPushButton("加载校准")
            self.load_calibration_button.clicked.connect(self._load_calibration_dialog)
            self.save_calibration_button = QtWidgets.QPushButton("保存校准")
            self.save_calibration_button.clicked.connect(self._save_calibration_dialog)
            calibration_layout.addWidget(self.calibration_channel, 0, 0)
            calibration_layout.addWidget(self.calibration_low, 0, 1)
            calibration_layout.addWidget(self.calibration_high, 0, 2)
            calibration_layout.addWidget(self.apply_calibration_button, 1, 0, 1, 3)
            calibration_layout.addWidget(self.load_calibration_button, 2, 0)
            calibration_layout.addWidget(self.save_calibration_button, 2, 1)
            self.calibration_status = QtWidgets.QLabel(str(self.calibration_path))
            self.calibration_status.setWordWrap(True)
            calibration_layout.addWidget(self.calibration_status, 3, 0, 1, 3)
            self.calibration_explanation = QtWidgets.QLabel("断开人体电极，在外部 EEGn 与 REF 之间分别接入 10 kΩ、47 kΩ，检测后填入对应固件原始值；板载 4.99 kΩ 不更换。")
            self.calibration_explanation.setWordWrap(True)
            calibration_layout.addWidget(self.calibration_explanation, 4, 0, 1, 3)
            layout.addWidget(calibration_group)

            bias_group = QtWidgets.QGroupBox("BIAS / 右腿驱动通道")
            bias_layout = QtWidgets.QGridLayout(bias_group)
            self.bias_checkboxes: list[QtWidgets.QCheckBox] = []
            for channel in range(CHANNELS):
                checkbox = QtWidgets.QCheckBox(f"CH{channel + 1}")
                checkbox.setChecked(True)
                checkbox.toggled.connect(self._bias_draft_changed)
                self.bias_checkboxes.append(checkbox)
                bias_layout.addWidget(checkbox, channel // 4, channel % 4)
            self.bias_query_button = QtWidgets.QPushButton("查询当前")
            self.bias_query_button.clicked.connect(self._query_bias_mask)
            self.bias_recommend_button = QtWidgets.QPushButton("采用阻抗推荐")
            self.bias_recommend_button.clicked.connect(self._recommend_bias_mask)
            self.bias_all_button = QtWidgets.QPushButton("全选")
            self.bias_all_button.clicked.connect(self._select_all_bias_channels)
            self.bias_apply_button = QtWidgets.QPushButton("应用到固件")
            self.bias_apply_button.clicked.connect(self._apply_bias_mask)
            bias_layout.addWidget(self.bias_query_button, 2, 0, 1, 2)
            bias_layout.addWidget(self.bias_recommend_button, 2, 2, 1, 2)
            bias_layout.addWidget(self.bias_all_button, 3, 0, 1, 2)
            bias_layout.addWidget(self.bias_apply_button, 3, 2, 1, 2)
            self.bias_status = QtWidgets.QLabel("等待连接后自动查询固件实际掩码")
            self.bias_status.setWordWrap(True)
            self.bias_actual_label = QtWidgets.QLabel("设备实际：未知")
            self.bias_actual_label.setWordWrap(True)
            self.bias_draft_label = QtWidgets.QLabel(
                "待应用 0xFF：CH1, CH2, CH3, CH4, CH5, CH6, CH7, CH8"
            )
            self.bias_draft_label.setWordWrap(True)
            bias_layout.addWidget(self.bias_actual_label, 4, 0, 1, 4)
            bias_layout.addWidget(self.bias_draft_label, 5, 0, 1, 4)
            bias_layout.addWidget(self.bias_status, 6, 0, 1, 4)
            layout.addWidget(bias_group)
            self._update_bias_controls()
            return tab

        def _build_self_test_tab(self) -> QtWidgets.QWidget:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            self.self_test_button = QtWidgets.QPushButton("开启内部短接自检")
            self.self_test_button.setEnabled(False)
            self.self_test_button.clicked.connect(self._toggle_self_test)
            layout.addWidget(self.self_test_button)
            self.self_test_overall = QtWidgets.QLabel("总体：等待内部短接数据")
            self.self_test_overall.setAlignment(QtCore.Qt.AlignCenter)
            self.self_test_overall.setStyleSheet("font-weight:bold; padding:5px;")
            layout.addWidget(self.self_test_overall)
            self.self_test_status = QtWidgets.QLabel(
                "手动开启后，使用连续1000点计算去均值RMS、峰峰值和50Hz幅值"
            )
            self.self_test_status.setWordWrap(True)
            layout.addWidget(self.self_test_status)
            self.self_test_table = QtWidgets.QTableWidget(CHANNELS, 5)
            self.self_test_table.setHorizontalHeaderLabels(
                ["通道", "RMS µV", "峰峰值 µV", "50Hz µV", "判定"]
            )
            self.self_test_table.verticalHeader().setVisible(False)
            self.self_test_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.self_test_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.self_test_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
            for channel in range(CHANNELS):
                for column, text in enumerate((f"CH{channel + 1}", "--", "--", "--", "等待")):
                    self._set_self_test_table_item(channel, column, text)
            layout.addWidget(self.self_test_table, 1)
            limits = QtWidgets.QLabel(
                "PASS：RMS≤1、P-P≤10、50Hz≤1 µV；WARN上限：5 / 50 / 5 µV；任一超限为FAIL"
            )
            limits.setWordWrap(True)
            layout.addWidget(limits)
            return tab

        def _build_record_tab(self) -> QtWidgets.QWidget:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            self.record_button = QtWidgets.QPushButton("开始 JSONL 记录")
            self.record_button.clicked.connect(self._toggle_recording)
            self.record_status = QtWidgets.QLabel("记录：关闭")
            self.record_status.setWordWrap(True)
            self.record_path_label = QtWidgets.QLabel("文件：--")
            self.record_path_label.setWordWrap(True)
            self.export_json_button = QtWidgets.QPushButton("JSONL → JSON")
            self.export_json_button.clicked.connect(self._export_json)
            self.export_csv_button = QtWidgets.QPushButton("JSONL → CSV")
            self.export_csv_button.clicked.connect(self._export_csv)
            self.reset_stats_button = QtWidgets.QPushButton("清零诊断计数")
            self.reset_stats_button.clicked.connect(self.model.reset_statistics)

            http_group = QtWidgets.QGroupBox("HTTP JSON 推送（200点 / 200 ms）")
            http_layout = QtWidgets.QGridLayout(http_group)
            self.http_ip_edit = QtWidgets.QLineEdit(self.http_settings.server_ip)
            self.http_port_edit = QtWidgets.QSpinBox()
            self.http_port_edit.setRange(1, 65535)
            self.http_port_edit.setValue(self.http_settings.port)
            self.http_endpoint_edit = QtWidgets.QLineEdit(self.http_settings.endpoint)
            self.http_button = QtWidgets.QPushButton("开始 HTTP 推送")
            self.http_button.clicked.connect(self._toggle_http_push)
            self.http_status = QtWidgets.QLabel("HTTP：关闭")
            self.http_status.setWordWrap(True)
            self.http_stats = QtWidgets.QLabel("成功 0，失败 0，丢弃 0，样本 0，队列 0")
            self.http_stats.setWordWrap(True)
            self.http_detail = QtWidgets.QLabel("最近响应：--")
            self.http_detail.setWordWrap(True)
            http_layout.addWidget(QtWidgets.QLabel("服务器 IP"), 0, 0)
            http_layout.addWidget(self.http_ip_edit, 0, 1)
            http_layout.addWidget(QtWidgets.QLabel("端口"), 0, 2)
            http_layout.addWidget(self.http_port_edit, 0, 3)
            http_layout.addWidget(QtWidgets.QLabel("路径"), 1, 0)
            http_layout.addWidget(self.http_endpoint_edit, 1, 1, 1, 3)
            http_layout.addWidget(self.http_button, 2, 0, 1, 4)
            http_layout.addWidget(self.http_status, 3, 0, 1, 4)
            http_layout.addWidget(self.http_stats, 4, 0, 1, 4)
            http_layout.addWidget(self.http_detail, 5, 0, 1, 4)

            layout.addWidget(self.record_button)
            layout.addWidget(self.record_status)
            layout.addWidget(self.record_path_label)
            layout.addWidget(http_group)
            layout.addWidget(self.export_json_button)
            layout.addWidget(self.export_csv_button)
            layout.addWidget(self.reset_stats_button)
            layout.addStretch(1)
            return tab

        def _build_quality_tab(self) -> QtWidgets.QWidget:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            badges = QtWidgets.QHBoxLayout()
            self.quality_overall = QtWidgets.QLabel("原始采集：等待1000点")
            self.quality_overall.setAlignment(QtCore.Qt.AlignCenter)
            self.quality_overall.setStyleSheet("font-weight:bold; padding:7px;")
            self.display_quality_overall = QtWidgets.QLabel("当前显示工频：等待1000点")
            self.display_quality_overall.setAlignment(QtCore.Qt.AlignCenter)
            self.display_quality_overall.setStyleSheet("font-weight:bold; padding:7px;")
            badges.addWidget(self.quality_overall)
            badges.addWidget(self.display_quality_overall)
            layout.addLayout(badges)
            self.quality_status = QtWidgets.QLabel(
                "原始状态用于判断采集链质量；显示50 Hz只验证当前参考/滤波效果；"
                "记录和HTTP始终保存原始EEG。每500 ms更新。"
            )
            self.quality_status.setWordWrap(True)
            layout.addWidget(self.quality_status)
            self.quality_table = QtWidgets.QTableWidget(CHANNELS, 7)
            self.quality_table.setHorizontalHeaderLabels(
                [
                    "通道",
                    "原始状态",
                    "RMS µV",
                    "P-P µV",
                    "原始50Hz",
                    "显示50Hz",
                    "抑制dB",
                ]
            )
            self.quality_table.verticalHeader().setVisible(False)
            self.quality_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.quality_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
            self.quality_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.quality_table.setAlternatingRowColors(True)
            self.quality_table.verticalHeader().setDefaultSectionSize(34)
            self.quality_table.setMinimumHeight(8 * 34 + 42)
            quality_header = self.quality_table.horizontalHeader()
            quality_header.setMinimumSectionSize(68)
            quality_header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            quality_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
            for column in range(2, 7):
                quality_header.setSectionResizeMode(column, QtWidgets.QHeaderView.Stretch)
            for channel in range(CHANNELS):
                for column, text in enumerate(
                    (f"CH{channel + 1}", "等待", "--", "--", "--", "--", "--")
                ):
                    self._set_quality_table_item(channel, column, text)
            self.quality_table.currentCellChanged.connect(self._quality_selection_changed)
            layout.addWidget(self.quality_table, 1)

            detail_group = QtWidgets.QGroupBox("所选通道详情")
            detail_layout = QtWidgets.QVBoxLayout(detail_group)
            self.quality_detail = QtWidgets.QLabel("CH1：等待连续质量窗口")
            self.quality_detail.setWordWrap(True)
            self.quality_detail.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            detail_layout.addWidget(self.quality_detail)
            layout.addWidget(detail_group)
            self.quality_table.selectRow(0)

            quality_limits = QtWidgets.QLabel(
                "原始良好：|DC|≤50 mV、RMS≤100 µV、P-P≤500 µV、50Hz≤10 µV、无削顶；"
                "显示工频：≤10 µV良好，≤50 µV警告。"
            )
            quality_limits.setWordWrap(True)
            layout.addWidget(quality_limits)

            reference_group = QtWidgets.QGroupBox("CAR/中位数参考参与通道（仅影响显示和FFT）")
            reference_layout = QtWidgets.QGridLayout(reference_group)
            self.reference_checkboxes: list[QtWidgets.QCheckBox] = []
            for channel in range(CHANNELS):
                checkbox = QtWidgets.QCheckBox(f"CH{channel + 1}")
                checkbox.setChecked(True)
                checkbox.toggled.connect(self._reference_mask_changed)
                self.reference_checkboxes.append(checkbox)
                reference_layout.addWidget(checkbox, channel // 4, channel % 4)
            self.reference_all_button = QtWidgets.QPushButton("全部参与")
            self.reference_all_button.clicked.connect(self._select_all_reference_channels)
            self.reference_good_button = QtWidgets.QPushButton("排除BAD通道")
            self.reference_good_button.clicked.connect(self._exclude_bad_reference_channels)
            reference_layout.addWidget(self.reference_all_button, 2, 0, 1, 2)
            reference_layout.addWidget(self.reference_good_button, 2, 2, 1, 2)
            self.reference_status = QtWidgets.QLabel("参考通道：CH1–CH8")
            self.reference_status.setWordWrap(True)
            reference_layout.addWidget(self.reference_status, 3, 0, 1, 4)
            layout.addWidget(reference_group)
            return tab

        def _toggle_http_push(self) -> None:
            if self.http_sender is not None:
                self._stop_http_push()
            else:
                self._start_http_push()

        def _start_http_push(self) -> None:
            if self.self_test_active or self.self_test_pending or self.bias_pending:
                return
            try:
                settings = HttpPushSettings(
                    self.http_ip_edit.text(),
                    self.http_port_edit.value(),
                    self.http_endpoint_edit.text(),
                ).normalized()
                settings.save(self.http_settings_path)
                sender = HttpJsonSender(settings)
                sender.start()
            except (OSError, ValueError, RuntimeError) as error:
                self.http_status.setText(f"HTTP启动失败：{error}")
                self.http_status.setStyleSheet("color:#ff6b6b")
                return
            self.http_settings = settings
            self.http_settings_warning = None
            self.http_aggregator.reset()
            self.http_sender = sender
            self.http_button.setText("停止 HTTP 推送")
            self._update_http_controls()
            self._refresh_http_status()

        def _stop_http_push(self) -> None:
            self.http_aggregator.reset()
            sender = self.http_sender
            if sender is not None:
                sender.stop()
                snapshot = sender.snapshot()
                self.http_stats.setText(
                    f"成功 {snapshot.successful_packets}，失败 {snapshot.failed_packets}，"
                    f"丢弃 {snapshot.dropped_packets}，样本 {snapshot.sent_samples}，队列 0"
                )
            self.http_sender = None
            self.http_button.setText("开始 HTTP 推送")
            self._update_http_controls()
            self._refresh_http_status()

        def _submit_http_batch(self, batch: EegBatch, record: dict) -> None:
            if self.http_sender is None:
                return
            if self.self_test_active or self.self_test_pending or not is_recordable_batch(batch):
                self.http_aggregator.reset()
                return
            payload = self.http_aggregator.ingest(batch, record)
            if payload is not None:
                self.http_sender.submit(payload)

        def _update_http_controls(self) -> None:
            active = self.http_sender is not None
            blocked = self.self_test_active or self.self_test_pending or self.bias_pending or self.tx_pending
            self.http_button.setEnabled(active or not blocked)
            self.http_ip_edit.setEnabled(not active)
            self.http_port_edit.setEnabled(not active)
            self.http_endpoint_edit.setEnabled(not active)

        def _refresh_http_status(self) -> None:
            sender = self.http_sender
            if sender is None:
                if self.http_settings_warning:
                    self.http_status.setText(
                        f"HTTP配置异常，已恢复默认值：{self.http_settings_warning}"
                    )
                    self.http_status.setStyleSheet("color:#d9a441")
                else:
                    self.http_status.setText("HTTP：关闭")
                    self.http_status.setStyleSheet("")
                return

            snapshot = sender.snapshot()
            self.http_stats.setText(
                f"成功 {snapshot.successful_packets}，失败 {snapshot.failed_packets}，"
                f"丢弃 {snapshot.dropped_packets}，样本 {snapshot.sent_samples}，"
                f"队列 {snapshot.queue_depth}"
            )
            if snapshot.last_error:
                self.http_detail.setText(f"最近错误：{snapshot.last_error}")
            elif snapshot.last_status_code is not None:
                latency = "--" if snapshot.last_latency_ms is None else f"{snapshot.last_latency_ms:.0f} ms"
                self.http_detail.setText(
                    f"最近响应：HTTP {snapshot.last_status_code}，耗时 {latency}"
                )
            else:
                self.http_detail.setText("最近响应：--")

            if self.self_test_active or self.self_test_pending:
                message, color = "HTTP：内部短接暂停，保持开启", "#d9a441"
            elif not self.connected or not self.has_valid_eeg:
                message, color = "HTTP：已开启，等待NORMAL EEG设备", "#d9a441"
            elif snapshot.last_error:
                message, color = f"HTTP：发送异常，继续等待恢复；{sender.settings.url}", "#ff6b6b"
            else:
                message, color = f"HTTP：发送中；{sender.settings.url}", "#2c9f68"
            self.http_status.setText(message)
            self.http_status.setStyleSheet(f"color:{color}")

        def _set_table_item(self, row: int, column: int, text: str, color: str | None = None) -> None:
            item = self.impedance_table.item(row, column)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.impedance_table.setItem(row, column, item)
            item.setText(text)
            item.setBackground(QtGui.QBrush(QtGui.QColor(color or "transparent")))

        def _set_self_test_table_item(
            self, row: int, column: int, text: str, color: str | None = None
        ) -> None:
            item = self.self_test_table.item(row, column)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.self_test_table.setItem(row, column, item)
            item.setText(text)
            item.setBackground(QtGui.QBrush(QtGui.QColor(color or "transparent")))

        def _set_quality_table_item(
            self, row: int, column: int, text: str, color: str | None = None
        ) -> None:
            item = self.quality_table.item(row, column)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.quality_table.setItem(row, column, item)
            item.setText(text)
            item.setBackground(QtGui.QBrush(QtGui.QColor(color or "transparent")))
            item.setForeground(
                QtGui.QBrush(QtGui.QColor("white" if color is not None else "black"))
            )

        def _quality_selection_changed(self, *_args) -> None:
            self._update_quality_detail()

        def _update_quality_detail(self) -> None:
            row = max(0, self.quality_table.currentRow())
            raw_snapshot = self._last_quality_snapshot
            display_snapshot = self._last_display_quality_snapshot
            if raw_snapshot is None or not raw_snapshot.ready or row >= len(raw_snapshot.channels):
                self.quality_detail.setText(f"CH{row + 1}：等待连续质量窗口")
                return
            raw = raw_snapshot.channels[row]
            reasons = "、".join(raw.reasons) or "无"
            if (
                display_snapshot is not None
                and display_snapshot.ready
                and row < len(display_snapshot.channels)
            ):
                display = display_snapshot.channels[row]
                suppression = mains_suppression_db(
                    raw.mains_50hz_uv, display.mains_50hz_uv
                )
                display_text = (
                    f"显示RMS {display.rms_uv:.2f} µV，显示P-P "
                    f"{display.peak_to_peak_uv:.2f} µV，显示50 Hz "
                    f"{display.mains_50hz_uv:.2f} µV，抑制 {suppression:.1f} dB"
                )
            else:
                display_text = "显示链：等待连续1000点"
            reference_name = {
                ReferenceMode.RAW: "原始参考",
                ReferenceMode.CAR: "CAR",
                ReferenceMode.MEDIAN: "中位数参考",
            }[self.display_reference.mode]
            self.quality_detail.setText(
                f"CH{row + 1}｜DC {raw.dc_mv:.2f} mV，峰值 "
                f"{raw.peak_fs_percent:.2f}%FS，削顶 {raw.clipped_samples}；"
                f"原始RMS {raw.rms_uv:.2f} µV，原始P-P "
                f"{raw.peak_to_peak_uv:.2f} µV，原始50 Hz "
                f"{raw.mains_50hz_uv:.2f} µV；{display_text}；"
                f"原因：{reasons}；显示={reference_name} + "
                f"{FILTER_MODE_LABELS[self.display_filter.mode]}"
            )

        def _set_state(self, state: str, message: str) -> None:
            styles = {
                "stopped": ("STOPPED", "#555b66"),
                "listening": ("LISTENING", "#247ba0"),
                "connected": ("CONNECTED", "#247ba0"),
                "eeg": ("EEG STREAM", "#2a9d65"),
                "impedance": ("IMPEDANCE / EEG PAUSED", "#c58b00"),
                "self_test": ("INTERNAL SHORT / NOT EEG", "#c58b00"),
                "error": ("ERROR", "#c44536"),
            }
            title, color = styles.get(state, (state.upper(), "#555b66"))
            self.big_status.setText(title)
            self.big_status.setStyleSheet(
                f"QLabel {{ color:white; background:{color}; border-radius:4px; padding:8px; }}"
            )
            self.detail_status.setText(message)

        def _toggle_receiver(self) -> None:
            if self.receiver is not None and self.receiver.is_alive():
                self._stop_receiver()
            else:
                self._start_receiver()

        def _start_receiver(self) -> None:
            if self.receiver is not None and self.receiver.is_alive():
                return
            self.host = self.bind_edit.text().strip() or "0.0.0.0"
            self.port = int(self.port_edit.value())
            self.active_receiver_epoch = object()
            if self.data_transport == "serial":
                session = self.network_panel.session
                if session is None or not session.connected:
                    self._set_state("error", "请到连接/配网页连接 USB，再应用串口模式")
                    return
                self.receiver = SerialReceiver(session, self.events)
            else:
                self.receiver = TcpReceiver(self.host, self.port, self.events)
                self.discovery = DiscoveryServer(
                    self.network_panel.pairing,
                    lambda: getattr(self.receiver, "bound_port", None) or self.port,
                    self.events,
                    port=self.discovery_port,
                )
                self.discovery.event_epoch = self.active_receiver_epoch
                self.discovery.start()
            self.receiver.event_epoch = self.active_receiver_epoch
            self.receiver.start()
            self.listen_button.setText("停止串口 EEG" if self.data_transport == "serial" else "停止监听")
            self.bind_edit.setEnabled(False)
            self.port_edit.setEnabled(False)
            self._set_state("listening", f"正在启动 {self.host}:{self.port}")

        def _stop_receiver(self) -> None:
            self.active_receiver_epoch = None
            self._stop_bias_client()
            if self.tx_client is not None:
                self.tx_client.stop()
                self.tx_client.join(timeout=1)
                self.tx_client = None
            self.tx_actual_frames = None
            self.tx_pending = False
            if self.discovery is not None:
                self.discovery.stop()
                self.discovery.join(timeout=1)
                self.discovery = None
            if self.receiver is not None:
                self.receiver.stop()
                self.receiver.join(timeout=2.0)
            self.receiver = None
            self.connected = False
            self.peer_ip = None
            self.has_valid_eeg = False
            self.http_aggregator.reset()
            self.display_filter.reset("disconnect")
            self.quality_analyzer.reset("disconnect")
            self.display_quality_analyzer.reset("disconnect")
            self.bias_actual_mask = None
            self.bias_pending = False
            self.bias_last_request_id = None
            self.bias_actual_label.setText("设备实际：未知")
            self.bias_actual_label.setStyleSheet("")
            self.bias_status.setText("连接已断开；固件实际BIAS掩码未知")
            self._set_self_test_active(False)
            self.listen_button.setText("开始监听")
            self.bind_edit.setEnabled(True)
            self.port_edit.setEnabled(True)
            self.remote_label.setText("远端：--")
            self._set_state("stopped", "监听已停止")
            self._update_impedance_button()
            self._update_self_test_button()
            self._update_record_controls()

        def _pause_transport_output(self) -> None:
            if self.writer is not None:
                self._toggle_recording()
            self._stop_http_push()
            self._stop_receiver()

        def _activate_transport(self, mode: str) -> None:
            self.data_transport = mode
            self._start_receiver()

        def _control_exchange(self):
            if self.data_transport == "serial":
                session = self.network_panel.session
                if session is None or not session.connected:
                    raise RuntimeError("USB session unavailable")
                return session.control_exchange
            return None

        def _set_tx_provisional_from_config(self) -> None:
            self.tx_actual_frames = None
            frames = int(self.model.config.get("tx_group_frames", 1))
            self.tx_control_status.setText(f"数据配置：{frames}帧 / {20*frames} ms（需查询确认）")
            self._update_tx_control()

        def _update_tx_control(self) -> None:
            enabled = (self.connected and self.has_valid_eeg and self.peer_ip is not None
                and not self.tx_pending and not self.bias_pending and not self.self_test_pending
                and not self.impedance_active and not self.restore_failed
                and self.writer is None and self.http_sender is None)
            self.tx_control_button.setEnabled(enabled)
            if self.tx_actual_frames not in (1, 2):
                self.tx_control_button.setText("发送分组：重新查询")
            else:
                target = 3-self.tx_actual_frames
                self.tx_control_button.setText(f"切换为 {target}帧 / {20*target} ms")

        def _toggle_tx_control(self) -> None:
            if not self.tx_control_button.isEnabled():
                return
            if self.tx_client is not None:
                self.tx_client.stop()
                self.tx_client.join(timeout=.1)
            self.tx_client = TxControlClient(self.peer_ip, events=self.events, exchange=self._control_exchange())
            self.tx_pending = True
            self.tx_last_request_id = (self.tx_client.start_query() if self.tx_actual_frames is None
                else self.tx_client.start_set(3-self.tx_actual_frames))
            self._update_tx_control()
            self._update_impedance_button()
            self._update_self_test_button()
            self._update_record_controls()

        def _handle_tx_control_event(self, event: dict) -> None:
            if event.get("request_id") != self.tx_last_request_id:
                return
            if event["state"] == "started":
                return
            self.tx_pending = False
            result = event.get("result")
            if isinstance(result, TxSetOutcome):
                result = result.result
            if isinstance(result, TxResultData) and result.status == TxStatus.OK and result.verified and result.actual_frames in (1, 2):
                self.tx_actual_frames = result.actual_frames
                self.model.config.update(tx_group_frames=result.actual_frames, tx_group_period_ms=20*result.actual_frames)
                self.tx_control_status.setText(f"设备确认：{result.actual_frames}帧 / {20*result.actual_frames} ms")
            else:
                self.tx_actual_frames = None
                self.tx_control_status.setText(event.get("message", "发送分组未确认，请重新查询"))
            self._update_tx_control()
            self._update_impedance_button()
            self._update_self_test_button()
            self._update_record_controls()

        def _change_window(self) -> None:
            seconds = float(self.window_combo.currentData())
            self.model.set_history_seconds(max(5.0, seconds))
            self._refresh_plots()

        def _update_filter_status(self) -> None:
            if self.self_test_active:
                text = "内部短接：未滤波；正式记录/HTTP暂停"
                color = "#d58a00"
            elif self.filter_error:
                text = self.filter_error
                color = "#ff6b6b"
            elif self.display_filter.mode == FilterMode.RAW:
                text = "波形仅去直流；FFT/记录/HTTP：原始EEG"
                color = "#888888"
            else:
                text = (
                    f"波形/FFT：{FILTER_MODE_LABELS[self.display_filter.mode]}；"
                    "记录/HTTP：原始EEG"
                )
                color = "#4aa3df"
            if not self.self_test_active and self.display_reference.mode != ReferenceMode.RAW:
                name = "CAR" if self.display_reference.mode == ReferenceMode.CAR else "中位数参考"
                text = f"{text}；显示参考={name}（原始输出不变）"
            self.filter_status.setText(text)
            self.filter_status.setStyleSheet(f"color:{color}; font-weight:bold")

        def _set_filter_combo_mode(self, mode: FilterMode) -> None:
            index = self.filter_combo.findData(mode.value)
            if index < 0 or self.filter_combo.currentIndex() == index:
                return
            self.filter_combo.blockSignals(True)
            self.filter_combo.setCurrentIndex(index)
            self.filter_combo.blockSignals(False)

        def _clear_plot_data(self) -> None:
            for curve in self.curves:
                curve.setData([], [])
            self.spectrum_curve.setData([], [])
            self.fft_peak.setText("主峰：--")
            self.latest_values.setText("  ".join(f"CH{index + 1} --" for index in range(CHANNELS)))

        def _change_filter_mode(self) -> None:
            try:
                mode = FilterMode(self.filter_combo.currentData())
            except (TypeError, ValueError):
                mode = FilterMode.RAW
            if mode != FilterMode.RAW and not SCIPY_AVAILABLE:
                self.filter_error = "SciPy不可用，显示滤波已回退原始模式"
                mode = FilterMode.RAW
                self._set_filter_combo_mode(mode)
            else:
                self.filter_error = None
            self.display_filter.set_mode(mode)
            self.display_quality_analyzer.reset("filter-mode")
            self.model.clear_display_history()
            self._clear_marker_lines()
            self._clear_plot_data()
            self._update_filter_status()

        def _reset_display_pipeline(self, reason: str) -> None:
            self.display_filter.reset(reason)
            self.display_quality_analyzer.reset(reason)
            self.model.clear_display_history(preserve_markers=True)
            self._clear_marker_lines()
            self._clear_plot_data()

        def _change_reference_mode(self) -> None:
            try:
                mode = ReferenceMode(self.reference_combo.currentData())
            except (TypeError, ValueError):
                mode = ReferenceMode.RAW
            if self.display_reference.set_mode(mode):
                self._reset_display_pipeline("reference-mode")
            self._update_filter_status()

        def _reference_checkbox_mask(self) -> int:
            return sum(
                (1 << channel) for channel, checkbox in enumerate(self.reference_checkboxes)
                if checkbox.isChecked()
            )

        def _reference_mask_changed(self) -> None:
            mask = self._reference_checkbox_mask()
            if mask.bit_count() < 2:
                self.reference_status.setText("至少选择2个参考通道；当前选择尚未应用")
                self.reference_status.setStyleSheet("color:#ff6b6b")
                return
            if self.display_reference.set_channel_mask(mask):
                self._reset_display_pipeline("reference-mask")
            names = ", ".join(f"CH{index + 1}" for index in range(CHANNELS) if mask & (1 << index))
            self.reference_status.setText(f"参考通道：{names}")
            self.reference_status.setStyleSheet("")

        def _set_reference_checkbox_mask(self, mask: int) -> None:
            for channel, checkbox in enumerate(self.reference_checkboxes):
                checkbox.blockSignals(True)
                checkbox.setChecked(bool(mask & (1 << channel)))
                checkbox.blockSignals(False)
            self._reference_mask_changed()

        def _select_all_reference_channels(self) -> None:
            self._set_reference_checkbox_mask(0xFF)

        def _exclude_bad_reference_channels(self) -> None:
            snapshot = self.quality_analyzer.snapshot(force=True)
            if not snapshot.ready:
                self.reference_status.setText("质量窗口尚未就绪，无法自动排除BAD通道")
                self.reference_status.setStyleSheet("color:#d9a441")
                return
            mask = sum(
                (1 << channel) for channel, metrics in enumerate(snapshot.channels)
                if metrics.grade != QualityGrade.BAD
            )
            if mask.bit_count() < 2:
                self.reference_status.setText("非BAD通道少于2个，未改变参考通道")
                self.reference_status.setStyleSheet("color:#ff6b6b")
                return
            self._set_reference_checkbox_mask(mask)

        def _filter_display_values(self, batch: EegBatch, record: dict) -> np.ndarray:
            raw_values = np.asarray(record["data"], dtype=np.float64)
            if batch.config_id != CONFIG_NORMAL:
                if self.display_filter.active:
                    self.display_filter.reset("config")
                return raw_values
            referenced_values = self.display_reference.process(raw_values, batch.config_id)
            if self.display_filter.mode == FilterMode.RAW:
                return referenced_values
            try:
                return self.display_filter.process(
                    referenced_values,
                    sample_rate_hz=float(record["sampling_rate"]),
                    stream_id=batch.stream_id,
                    config_id=batch.config_id,
                    start_seq=batch.start_seq,
                )
            except (FilterUnavailableError, ValueError) as error:
                self.filter_error = f"显示滤波已回退原始模式：{error}"
                self.display_filter.set_mode(FilterMode.RAW)
                self.display_quality_analyzer.reset("filter-error")
                self._set_filter_combo_mode(FilterMode.RAW)
                self.model.clear_display_history()
                self._clear_marker_lines()
                self._clear_plot_data()
                self._update_filter_status()
                return referenced_values

        def _clear_marker_lines(self) -> None:
            for channel, lines in enumerate(self.marker_lines):
                for line in lines:
                    self.plots[channel].removeItem(line)
                self.marker_lines[channel] = []

        def _set_force_markers_enabled(self, enabled: bool) -> None:
            self.force_markers_enabled = bool(enabled)
            if self.force_marker_button.isChecked() != self.force_markers_enabled:
                self.force_marker_button.blockSignals(True)
                self.force_marker_button.setChecked(self.force_markers_enabled)
                self.force_marker_button.blockSignals(False)
            if self.force_markers_enabled:
                self.force_marker_button.setText("Marker覆盖：全部为1")
                self.force_marker_button.setStyleSheet(
                    "QPushButton { color:#201600; background:#f0ad36; font-weight:bold; }"
                )
            else:
                self.force_marker_button.setText("Marker覆盖：关闭")
                self.force_marker_button.setStyleSheet("")
            self.force_marker_hint.setVisible(self.force_markers_enabled)
            self.model.marker_events.clear()
            self.model.last_marker_code = 0
            self._clear_marker_lines()
            self.http_aggregator.reset()
            self._refresh_diagnostics()

        def _ensure_bias_client(self) -> BiasControlClient:
            if not self.peer_ip:
                raise RuntimeError("没有可用的ESP32 TCP对端地址")
            if self.bias_client is not None and self.bias_client.device_host != self.peer_ip:
                self._stop_bias_client()
            if self.bias_client is None:
                self.bias_client = BiasControlClient(
                    self.peer_ip,
                    DEFAULT_BIAS_PORT,
                    timeout_s=0.3,
                    retries=2,
                    events=self.events,
                    exchange=self._control_exchange(),
                )
            return self.bias_client

        def _stop_bias_client(self) -> None:
            client = self.bias_client
            self.bias_client = None
            if client is not None:
                client.stop()
                client.join(timeout=1.0)

        def _bias_checkbox_mask(self) -> int:
            return sum(
                (1 << channel) for channel, checkbox in enumerate(self.bias_checkboxes)
                if checkbox.isChecked()
            )

        @staticmethod
        def _format_channel_mask(mask: int) -> str:
            names = [f"CH{channel + 1}" for channel in range(CHANNELS) if mask & (1 << channel)]
            return ", ".join(names) if names else "无"

        def _set_bias_checkbox_mask(self, mask: int) -> None:
            self.bias_draft_mask = int(mask) & 0xFF
            for channel, checkbox in enumerate(self.bias_checkboxes):
                checkbox.blockSignals(True)
                checkbox.setChecked(bool(self.bias_draft_mask & (1 << channel)))
                checkbox.blockSignals(False)
            self._bias_draft_changed()

        def _bias_draft_changed(self) -> None:
            self.bias_draft_mask = self._bias_checkbox_mask()
            self.bias_draft_label.setText(
                f"待应用 0x{self.bias_draft_mask:02X}："
                f"{self._format_channel_mask(self.bias_draft_mask)}"
            )
            if self.bias_draft_mask == 0:
                self.bias_status.setText("BIAS至少需要选择1个通道，当前草稿不能应用")
                self.bias_status.setStyleSheet("color:#ff6b6b")
            elif self.bias_actual_mask is not None:
                same = self.bias_draft_mask == self.bias_actual_mask
                self.bias_draft_label.setStyleSheet("" if same else "color:#d9a441")
            self._update_bias_controls()

        def _select_all_bias_channels(self) -> None:
            self._set_bias_checkbox_mask(0xFF)

        def _recommend_bias_mask(self) -> None:
            if self.last_impedance_result is None:
                self.bias_status.setText("尚无阻抗检测结果，无法生成BIAS推荐")
                self.bias_status.setStyleSheet("color:#d9a441")
                return
            mask = recommend_bias_mask(self.last_impedance_result, self.calibration)
            if mask == 0:
                self.bias_status.setText("阻抗结果中没有可推荐的有效通道，未改变草稿")
                self.bias_status.setStyleSheet("color:#ff6b6b")
                return
            self._set_bias_checkbox_mask(mask)
            self.bias_status.setText(
                f"阻抗推荐草稿 0x{mask:02X}：{self._format_channel_mask(mask)}；点击“应用到固件”才会生效"
            )
            self.bias_status.setStyleSheet("color:#d9a441")

        def _query_bias_mask(self) -> None:
            if not self.connected or not self.peer_ip or self.bias_pending:
                return
            try:
                request_id = self._ensure_bias_client().start_query()
            except (OSError, ValueError, RuntimeError) as error:
                self.bias_status.setText(f"BIAS查询启动失败：{error}")
                self.bias_status.setStyleSheet("color:#ff6b6b")
                return
            self.bias_pending = True
            self.bias_last_request_id = request_id
            self.bias_status.setText(f"请求 0x{request_id:08X}：正在查询固件实际BIAS掩码")
            self.bias_status.setStyleSheet("color:#247ba0")
            self._update_bias_controls()

        def _apply_bias_mask(self) -> None:
            target = self._bias_checkbox_mask()
            if target == 0 or not self.bias_apply_button.isEnabled():
                return
            if target.bit_count() <= 2:
                answer = QtWidgets.QMessageBox.question(
                    self,
                    "确认低通道数BIAS",
                    f"当前仅选择 {target.bit_count()} 个通道参与BIAS驱动。\n"
                    "共模抑制能力可能下降，是否继续？",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                if answer != QtWidgets.QMessageBox.Yes:
                    return
            try:
                request_id = self._ensure_bias_client().start_set(target)
            except (OSError, ValueError, RuntimeError) as error:
                self.bias_status.setText(f"BIAS设置启动失败：{error}")
                self.bias_status.setStyleSheet("color:#ff6b6b")
                return
            self.bias_pending = True
            self.bias_last_request_id = request_id
            self.bias_status.setText(
                f"请求 0x{request_id:08X}：正在应用 0x{target:02X}，等待固件写入/回读确认"
            )
            self.bias_status.setStyleSheet("color:#247ba0")
            self._update_bias_controls()

        def _handle_bias_event(self, event: dict) -> None:
            state = str(event.get("state", "error"))
            request_id = int(event.get("request_id", 0))
            if self.bias_last_request_id is None or request_id != self.bias_last_request_id:
                return
            if state == "started":
                return
            self.bias_pending = False
            if state != "result":
                self.bias_actual_mask = None
                self.bias_actual_label.setText("设备实际：未知")
                self.bias_actual_label.setStyleSheet("color:#ff6b6b")
                message = str(event.get("message", state))
                self.bias_status.setText(f"BIAS控制失败：{message}；实际掩码未知，请重新查询")
                self.bias_status.setStyleSheet("color:#ff6b6b")
                self.bias_last_request_id = None
                self._update_bias_controls()
                return

            payload = event["result"]
            outcome = payload if isinstance(payload, BiasSetOutcome) else None
            result = outcome.result if outcome is not None else payload
            if not isinstance(result, BiasResultData):
                self.bias_actual_mask = None
                self.bias_actual_label.setText("设备实际：未知")
                self.bias_actual_label.setStyleSheet("color:#ff6b6b")
                self.bias_status.setText("BIAS返回对象无效")
                self.bias_status.setStyleSheet("color:#ff6b6b")
                self.bias_last_request_id = None
                self._update_bias_controls()
                return
            flag_names = [flag.name for flag in BiasFlags if flag and result.flags & flag]
            reconciled = (
                f"，SET超时后已用QUERY 0x{result.request_id:08X}核对"
                if outcome is not None and outcome.reconciled else ""
            )
            if result.verified and result.actual_mask:
                self.bias_actual_mask = result.actual_mask
                self.bias_actual_label.setText(
                    f"设备实际 0x{result.actual_mask:02X}："
                    f"{self._format_channel_mask(result.actual_mask)}（VERIFIED）"
                )
                self.bias_actual_label.setStyleSheet("color:#287a4d")
            if result.status == BiasStatus.OK and result.verified:
                if outcome is None or outcome.applied:
                    self._set_bias_checkbox_mask(result.actual_mask)
                if outcome is not None and not outcome.applied:
                    message = (
                        f"SET未确认：目标0x{outcome.target_mask:02X}，固件实际0x{result.actual_mask:02X}"
                    )
                    color = "#ff6b6b"
                else:
                    message = (
                        f"固件确认 0x{result.actual_mask:02X}："
                        f"{self._format_channel_mask(result.actual_mask)}"
                    )
                    color = "#287a4d"
            else:
                if result.status == BiasStatus.RESTORE_FAILED:
                    self.restore_failed = True
                    self.bias_actual_mask = None
                    self.bias_actual_label.setText("设备实际：未知")
                    self.bias_actual_label.setStyleSheet("color:#ff6b6b")
                if result.status == BiasStatus.APPLY_FAILED and result.verified:
                    message = f"应用失败，已回滚并确认 0x{result.actual_mask:02X}"
                else:
                    message = f"固件返回 {result.status.name}，未产生新的已验证掩码"
                color = "#ff6b6b" if result.status != BiasStatus.BUSY else "#d9a441"
            self.bias_status.setText(
                f"请求 0x{request_id:08X}：{message}{reconciled}；"
                f"stream=0x{result.stream_id:08X}，flags={','.join(flag_names) or '0'}"
            )
            self.bias_status.setStyleSheet(f"color:{color}")
            self.bias_last_request_id = None
            self._update_impedance_button()
            self._update_self_test_button()
            self._update_record_controls()

        def _update_bias_controls(self) -> None:
            if not hasattr(self, "bias_query_button"):
                return
            safe = (
                self.connected and self.peer_ip is not None and not self.bias_pending
                and not self.impedance_active and not self.self_test_active
                and not self.self_test_pending and not self.restore_failed
                and not self.tx_pending
            )
            set_unlocked = (
                safe and self.has_valid_eeg and self.bias_actual_mask is not None
                and self.bias_draft_mask != 0
                and self.writer is None and self.http_sender is None
            )
            self.bias_query_button.setEnabled(safe)
            self.bias_apply_button.setEnabled(set_unlocked)
            self.bias_all_button.setEnabled(not self.bias_pending)
            self.bias_recommend_button.setEnabled(
                not self.bias_pending and self.last_impedance_result is not None
            )

        def _refresh_quality_metrics(self) -> None:
            snapshot = self.quality_analyzer.snapshot()
            display_snapshot = self.display_quality_analyzer.snapshot()
            self._last_quality_snapshot = snapshot
            self._last_display_quality_snapshot = display_snapshot
            colors = {
                QualityGrade.GOOD: "#287a4d",
                QualityGrade.WARN: "#9a7b16",
                QualityGrade.BAD: "#b23a48",
            }
            base_style = "font-weight:bold; padding:7px; border-radius:3px;"
            if snapshot.bypassed:
                self.quality_overall.setText("原始采集：内部短接暂停")
                self.quality_overall.setStyleSheet(
                    f"{base_style} color:#d9a441;"
                )
            elif snapshot.error:
                self.quality_overall.setText(f"原始采集：输入错误 - {snapshot.error}")
                self.quality_overall.setStyleSheet(
                    f"{base_style} color:#ff6b6b;"
                )
            elif not snapshot.ready:
                self.quality_overall.setText(
                    f"原始采集：收集中 {snapshot.sample_count}/1000"
                )
                self.quality_overall.setStyleSheet(base_style)
            else:
                color = colors[snapshot.overall]
                self.quality_overall.setText(f"原始采集：{snapshot.overall.value}")
                self.quality_overall.setStyleSheet(
                    f"{base_style} color:white; background:{color};"
                )

            if display_snapshot.bypassed:
                self.display_quality_overall.setText("当前显示工频：内部短接暂停")
                self.display_quality_overall.setStyleSheet(
                    f"{base_style} color:#d9a441;"
                )
            elif display_snapshot.error:
                self.display_quality_overall.setText(
                    f"当前显示工频：输入错误 - {display_snapshot.error}"
                )
                self.display_quality_overall.setStyleSheet(
                    f"{base_style} color:#ff6b6b;"
                )
            elif not display_snapshot.ready:
                self.display_quality_overall.setText(
                    f"当前显示工频：收集中 {display_snapshot.sample_count}/1000"
                )
                self.display_quality_overall.setStyleSheet(base_style)
            else:
                color = colors[display_snapshot.overall]
                self.display_quality_overall.setText(
                    f"当前显示工频：{display_snapshot.overall.value}"
                )
                self.display_quality_overall.setStyleSheet(
                    f"{base_style} color:white; background:{color};"
                )

            if not snapshot.ready:
                self._update_quality_detail()
                return
            for channel, metrics in enumerate(snapshot.channels):
                color = colors[metrics.grade]
                display_metrics = (
                    display_snapshot.channels[channel]
                    if display_snapshot.ready and channel < len(display_snapshot.channels)
                    else None
                )
                display_mains = (
                    f"{display_metrics.mains_50hz_uv:.2f}"
                    if display_metrics is not None else "--"
                )
                suppression = (
                    f"{mains_suppression_db(metrics.mains_50hz_uv, display_metrics.mains_50hz_uv):.1f}"
                    if display_metrics is not None else "--"
                )
                values = (
                    f"CH{channel + 1}",
                    metrics.grade.value,
                    f"{metrics.rms_uv:.2f}",
                    f"{metrics.peak_to_peak_uv:.2f}",
                    f"{metrics.mains_50hz_uv:.2f}",
                    display_mains,
                    suppression,
                )
                for column, value in enumerate(values):
                    self._set_quality_table_item(channel, column, value, color if column == 1 else None)
                reason_text = "、".join(metrics.reasons) or "无异常"
                self.quality_table.item(channel, 1).setToolTip(
                    f"CH{channel + 1} 原始状态：{metrics.grade.value}\n原因：{reason_text}"
                )
            self._update_quality_detail()

        def _drain_events(self) -> None:
            changed = False
            while True:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                if event[0] == "receiver_epoch":
                    if event[1] is not self.active_receiver_epoch:
                        continue
                    event = event[2]
                kind = event[0]
                if kind in {"network_done", "usb_status", "usb_error", "usb_sync"}:
                    self.network_panel.handle(kind, event[1])
                    continue
                if kind == "usb_log":
                    self.network_panel.handle(kind, event[1])
                    continue  # Diagnostic log never enters EEG/export.
                if kind == "discovery_error":
                    self.network_panel.operation_status.setText("配对发现不可用：" + event[1]["error"] + "；可经 USB 下发实际地址")
                    continue
                if kind == "discovery":
                    self.network_panel.handle(kind, event[1])
                    continue
                if kind == "receiver":
                    state = event[1]["state"]
                    if state == "stopped" and self.receiver is not None and self.receiver.is_alive():
                        continue
                    self._set_state(state if state in {"listening", "connected", "error", "stopped"} else "error", event[1]["message"])
                    if "port" in event[1]:
                        self.port_edit.setValue(int(event[1]["port"]))
                elif kind == "peer":
                    self.connected = bool(event[1]["connected"])
                    self.http_aggregator.reset()
                    if self.connected:
                        self.peer_ip = str(event[1]["host"])
                        self.peer_port = int(event[1]["port"])
                        self.restore_failed = False
                        self.remote_label.setText(f"远端：{self.peer_ip}:{self.peer_port}")
                    else:
                        self._stop_bias_client()
                        self.display_filter.reset("disconnect")
                        self.quality_analyzer.reset("disconnect")
                        self.display_quality_analyzer.reset("disconnect")
                        self.bias_actual_mask = None
                        self.bias_pending = False
                        self.bias_last_request_id = None
                        self.bias_actual_label.setText("设备实际：未知")
                        self.bias_actual_label.setStyleSheet("")
                        self.bias_status.setText("连接已断开；固件实际BIAS掩码未知")
                        self.peer_ip = None
                        self.peer_port = None
                        self.has_valid_eeg = False
                        self.self_test_pending = False
                        self.self_test_pending_target = None
                        self._set_self_test_active(False)
                        self.remote_label.setText("远端：--")
                    self._update_impedance_button()
                    self._update_self_test_button()
                    self._update_record_controls()
                    if self.connected:
                        self._query_bias_mask()
                elif kind == "config":
                    self.model.set_config(event[1])
                    self._set_tx_provisional_from_config()
                    device = event[1].get("device", event[1].get("device_id", "ESP32-C5-ADS1299"))
                    self.device_label.setText(f"设备：{device}")
                elif kind == "batch":
                    self.model.set_config(event[1])
                    raw_batch = event[2]
                    batch = apply_marker_override(raw_batch, self.force_markers_enabled)
                    record = batch_to_record(batch, self.model.config)
                    self.quality_analyzer.ingest_batch(
                        raw_batch,
                        int(event[1].get("sampling_rate", event[1].get("sample_rate_hz", 1000))),
                        float(event[1].get("vref_v", 4.5)),
                        float(event[1].get("pga_gain", 12.0)),
                    )
                    display_values = self._filter_display_values(batch, record)
                    self.display_quality_analyzer.ingest_batch(
                        raw_batch,
                        int(event[1].get("sampling_rate", event[1].get("sample_rate_hz", 1000))),
                        display_values,
                    )
                    record = self.model.ingest(
                        batch,
                        display_values=display_values,
                        record=record,
                    )
                    self.self_test_analyzer.ingest_batch(
                        raw_batch,
                        float(event[1].get("vref_v", 4.5)),
                        float(event[1].get("pga_gain", 12.0)),
                    )
                    self.has_valid_eeg = True
                    self._set_self_test_active(batch.config_id == CONFIG_INTERNAL_SHORT)
                    if self.self_test_pending_target == batch.config_id:
                        self.self_test_pending = False
                        self.self_test_pending_target = None
                        self.self_test_status.setText(
                            f"{'USB' if self.data_transport == 'serial' else 'TCP'}权威确认：config_id={batch.config_id}，stream_id=0x{batch.stream_id:08X}"
                        )
                    if self.writer is not None and is_recordable_batch(batch):
                        self.writer.submit(record)
                    self._submit_http_batch(batch, record)
                    if not self.impedance_active:
                        if self.self_test_active:
                            self._set_state("self_test", f"内部短接数据接收正常；{'USB' if self.data_transport == 'serial' else 'TCP'} config_id=2")
                        else:
                            self._set_state("eeg", f"EEG数据接收正常；{'USB' if self.data_transport == 'serial' else 'TCP'} config_id=1")
                    changed = True
                    self._update_impedance_button()
                    self._update_self_test_button()
                    self._update_record_controls()
                elif kind == "impedance":
                    self._handle_impedance_event(event[1])
                elif kind == "self_test":
                    self._handle_self_test_event(event[1])
                elif kind == "bias":
                    self._handle_bias_event(event[1])
                elif kind == "tx_control":
                    self._handle_tx_control_event(event[1])
            if changed and not self.impedance_active:
                self._refresh_plots()
            self._refresh_diagnostics()
            if time.monotonic() - self._last_self_test_refresh >= 0.5:
                self._last_self_test_refresh = time.monotonic()
                self._refresh_self_test_metrics()
            if time.monotonic() - self._last_quality_refresh >= 0.5:
                self._last_quality_refresh = time.monotonic()
                self._refresh_quality_metrics()

        def _refresh_plots(self) -> None:
            seconds = float(self.window_combo.currentData())
            times, values = self.model.plot_data(seconds)
            if not len(times):
                return
            relative_times = times - times[-1]
            display_values = _display_dc_removed(values, self.model.sampling_rate)
            for channel, curve in enumerate(self.curves):
                channel_values = display_values[:, channel]
                curve.setData(relative_times, channel_values)
                self.plots[channel].setXRange(-seconds, 0.0, padding=0.0)
                low, high = np.percentile(channel_values, [2.0, 98.0])
                center = float(np.median(channel_values))
                half_range = max(DISPLAY_MIN_HALF_RANGE_UV, float(high - low) * 0.65)
                self.plots[channel].setYRange(center - half_range, center + half_range, padding=0.0)
                for line in self.marker_lines[channel]:
                    self.plots[channel].removeItem(line)
                lines = []
                if not self.force_markers_enabled:
                    for timestamp, code in self.model.marker_events:
                        if timestamp < times[0]:
                            continue
                        line = pg.InfiniteLine(
                            pos=timestamp - times[-1], angle=90,
                            pen=pg.mkPen("#ef476f", width=1.2), label=f"M{code}",
                            labelOpts={"color": "#ef476f", "position": 0.88},
                        )
                        self.plots[channel].addItem(line)
                        lines.append(line)
                self.marker_lines[channel] = lines
                self.pause_labels[channel].setPos(-seconds / 2.0, center)

            selected = self.fft_channel.currentIndex()
            centered = values[:, selected] - np.mean(values[:, selected])
            if len(centered) >= 8:
                window = np.hanning(len(centered))
                frequencies = np.fft.rfftfreq(len(centered), d=1.0 / self.model.sampling_rate)
                magnitude = np.abs(np.fft.rfft(centered * window)) * 2.0 / max(1.0, window.sum())
                keep = frequencies <= 60.0
                self.spectrum_curve.setData(frequencies[keep], magnitude[keep])
                non_dc = np.flatnonzero((frequencies > 0.5) & keep)
                if len(non_dc):
                    peak = non_dc[int(np.argmax(magnitude[non_dc]))]
                    self.fft_peak.setText(f"主峰：{frequencies[peak]:.2f} Hz / {magnitude[peak]:.2f} µV")
            latest = values[-1]
            self.latest_values.setText(
                "  ".join(f"CH{index + 1} {latest[index]:+.2f}µV" for index in range(CHANNELS))
            )

        def _refresh_diagnostics(self) -> None:
            now = time.monotonic()
            elapsed = now - self._last_status_time
            if elapsed >= 0.5:
                sample_delta = self.model.received_samples - self._last_status_samples
                instantaneous = sample_delta / elapsed
                self._sps = instantaneous if self._sps == 0.0 else 0.7 * self._sps + 0.3 * instantaneous
                self._last_status_time = now
                self._last_status_samples = self.model.received_samples
            age = "--"
            if self.model.last_batch_monotonic is not None:
                age = f"{(now - self.model.last_batch_monotonic) * 1000.0:.0f} ms"
            receiver_errors = self.receiver.parser_errors if self.receiver is not None else 0
            receiver_drops = self.receiver.dropped_events if self.receiver is not None else 0
            if self.self_test_active:
                marker_status = "禁用（自检）"
            elif self.force_markers_enabled and self.model.last_config_id == CONFIG_NORMAL:
                marker_status = "1（强制）"
            else:
                marker_status = str(self.model.last_marker_code or "--")
            tx_group_frames = int(self.model.config.get("tx_group_frames", 1))
            tx_group_period_ms = int(
                self.model.config.get("tx_group_period_ms", 20 * tx_group_frames)
            )
            values = {
                "sps": f"{self._sps:.0f}",
                "age": age,
                "lost": str(self.model.lost_samples),
                "crc": str(self.crc_errors + receiver_errors),
                "stream": "--" if self.model.last_stream_id is None else f"0x{self.model.last_stream_id:08X}",
                "interrupt": str(self.model.stream_interruptions),
                "marker": marker_status,
                "queue": f"{self.events.qsize()} / drop {receiver_drops}",
                "tx": f"{tx_group_frames}帧 / {tx_group_period_ms} ms",
            }
            for key, value in values.items():
                self.metric_labels[key].setText(value)
            if self.writer is not None:
                if self.self_test_active or self.self_test_pending:
                    self.record_status.setText(
                        f"记录：会话保持，自检批次暂停写入；已写 {self.writer.written}，丢弃 {self.writer.dropped}"
                    )
                else:
                    self.record_status.setText(
                        f"记录：开启，批次 {self.writer.written}，写队列 {self.writer.queue.qsize()}，丢弃 {self.writer.dropped}"
                    )
            self._refresh_http_status()
            self._update_tx_control()

        def _update_impedance_button(self) -> None:
            enabled = (
                self.connected and self.has_valid_eeg and self.peer_ip is not None
                and not self.impedance_active and not self.restore_failed
                and not self.self_test_active and not self.self_test_pending
                and not self.tx_pending
                and not self.bias_pending
            )
            self.impedance_button.setEnabled(enabled)

        def _set_impedance_active(self, active: bool) -> None:
            self.impedance_active = active
            if self.impedance_active:
                self.http_aggregator.reset()
            for label in self.pause_labels:
                label.setVisible(active)
            self._update_impedance_button()
            self._update_self_test_button()
            self._update_record_controls()
            self._update_bias_controls()

        def _set_self_test_active(self, active: bool) -> None:
            changed = self.self_test_active != bool(active)
            self.self_test_active = bool(active)
            self.self_test_banner.setVisible(self.self_test_active)
            self.self_test_button.setText(
                "关闭内部短接，恢复正常EEG" if self.self_test_active else "开启内部短接自检"
            )
            if changed:
                for channel in range(CHANNELS):
                    for column, text in enumerate((f"CH{channel + 1}", "--", "--", "--", "等待")):
                        self._set_self_test_table_item(channel, column, text)
                self.self_test_overall.setText("总体：等待内部短接数据")
                self.self_test_overall.setStyleSheet("font-weight:bold; padding:5px;")
            if changed and not self.self_test_active:
                self.self_test_analyzer.reset()
            if changed:
                self.http_aggregator.reset()
            self._update_filter_status()
            self._update_impedance_button()
            self._update_self_test_button()
            self._update_record_controls()
            self._update_bias_controls()

        def _update_self_test_button(self) -> None:
            enabled = (
                self.connected
                and self.has_valid_eeg
                and self.peer_ip is not None
                and not self.impedance_active
                and not self.self_test_pending
                and not self.restore_failed
                and not self.bias_pending
                and not self.tx_pending
            )
            self.self_test_button.setEnabled(enabled)

        def _update_record_controls(self) -> None:
            blocked = self.self_test_active or self.self_test_pending or self.bias_pending or self.tx_pending
            self.record_button.setEnabled(self.writer is not None or not blocked)
            self.export_json_button.setEnabled(not blocked)
            self.export_csv_button.setEnabled(not blocked)
            if self.writer is not None and blocked:
                self.record_status.setText(
                    f"记录：会话保持，但内部短接批次暂停写入；已写 {self.writer.written} 批次"
                )
            self._update_http_controls()
            self._update_bias_controls()

        def _toggle_self_test(self) -> None:
            if not self.peer_ip or not self.self_test_button.isEnabled():
                return
            target = CONFIG_NORMAL if self.self_test_active else CONFIG_INTERNAL_SHORT
            self.self_test_client = SelfTestClient(
                self.peer_ip,
                DEFAULT_SELF_TEST_PORT,
                timeout_s=0.5,
                retries=2,
                events=self.events,
                exchange=self._control_exchange(),
            )
            try:
                request_id = self.self_test_client.start(target)
            except (OSError, ValueError, RuntimeError) as error:
                self._set_state("error", str(error))
                return
            self.self_test_pending = True
            self.self_test_pending_target = target
            self.http_aggregator.reset()
            action = "开启内部短接" if target == CONFIG_INTERNAL_SHORT else "恢复正常EEG"
            self.self_test_status.setText(
                f"请求 0x{request_id:08X}：正在{action}，等待控制反馈及数据 config_id确认"
            )
            self._set_state("self_test", f"正在{action}")
            self._update_impedance_button()
            self._update_bias_controls()
            self._update_self_test_button()
            self._update_record_controls()

        def _handle_self_test_event(self, event: dict) -> None:
            state = event["state"]
            if state == "started":
                return
            if state != "result":
                self.self_test_pending = False
                self.self_test_pending_target = None
                message = str(event.get("message", state))
                self.self_test_status.setText(f"模式切换失败：{message}")
                self._set_state("error", message)
            else:
                self._handle_self_test_result(event["result"])
            self._update_impedance_button()
            self._update_self_test_button()
            self._update_record_controls()

        def _handle_self_test_result(self, result: SelfTestResultData) -> None:
            flag_names = [flag.name for flag in type(result.flags) if flag and result.flags & flag]
            self.self_test_status.setText(
                f"请求 0x{result.request_id:08X}：{result.status.name}，请求config_id="
                f"{result.requested_config_id}，实际={result.actual_config_id}，"
                f"stream=0x{result.stream_id:08X}，flags={','.join(flag_names) or '0'}"
            )
            if result.status == SelfTestStatus.OK:
                self.self_test_pending_target = result.actual_config_id
                if self.model.last_config_id == result.actual_config_id:
                    self.self_test_pending = False
                    self.self_test_pending_target = None
            else:
                self.self_test_pending = False
                self.self_test_pending_target = None
                if result.status == SelfTestStatus.RESTORE_FAILED:
                    self.restore_failed = True
                self._set_state("error", f"内部短接模式切换返回 {result.status.name}")

        def _refresh_self_test_metrics(self) -> None:
            snapshot = self.self_test_analyzer.snapshot()
            if not snapshot.ready:
                if self.self_test_active:
                    self.self_test_overall.setText(
                        f"总体：等待连续1000点（{snapshot.sample_count}/1000）"
                    )
                return
            colors = {
                SelfTestGrade.PASS: "#287a4d",
                SelfTestGrade.WARN: "#9a7b16",
                SelfTestGrade.FAIL: "#b23a48",
            }
            for channel, metrics in enumerate(snapshot.channels):
                color = colors[metrics.grade]
                self._set_self_test_table_item(channel, 1, f"{metrics.rms_uv:.3f}")
                self._set_self_test_table_item(channel, 2, f"{metrics.peak_to_peak_uv:.3f}")
                self._set_self_test_table_item(channel, 3, f"{metrics.mains_50hz_uv:.3f}")
                self._set_self_test_table_item(channel, 4, metrics.grade.value, color)
            color = colors[snapshot.overall]
            self.self_test_overall.setText(f"总体：{snapshot.overall.value}")
            self.self_test_overall.setStyleSheet(
                f"color:white; background:{color}; font-weight:bold; padding:5px; border-radius:3px;"
            )

        def _start_impedance(self) -> None:
            if not self.peer_ip or not self.impedance_button.isEnabled():
                return
            self.impedance_client = ImpedanceClient(
                self.peer_ip, self.impedance_port, timeout_s=5.0, retries=1, events=self.events,
                exchange=self._control_exchange(),
            )
            try:
                request_id = self.impedance_client.start(0xFF)
            except (OSError, ValueError, RuntimeError) as error:
                self._set_state("error", str(error))
                return
            self._set_impedance_active(True)
            self.impedance_status.setText(f"请求 0x{request_id:08X}：检测中，EEG约暂停2秒，请勿触发P300刺激")
            self._set_state("impedance", "阻抗检测中，EEG暂停")

        def _handle_impedance_event(self, event: dict) -> None:
            state = event["state"]
            if state == "started":
                return
            if state == "result":
                self._handle_impedance_result(event["result"])
                return
            self._set_impedance_active(False)
            message = str(event.get("message", state))
            if state == "cancelled":
                self.impedance_status.setText(f"检测已取消：{message}")
                self._set_state("listening" if not self.connected else "connected", message)
            else:
                self.impedance_status.setText(f"检测失败：{message}")
                self._set_state("error", message)

        def _handle_impedance_result(self, result: ImpedanceResultData) -> None:
            self.last_impedance_result = result
            self._set_impedance_active(False)
            self.impedance_save_result.setEnabled(True)
            for channel in range(CHANNELS):
                nominal = result.nominal_ohm[channel]
                excitation = result.excitation_nv_peak[channel]
                calibration = self.calibration.channels[channel]
                if not result.channel_tested(channel):
                    state, display, color = "未测试", "--", "#555b66"
                elif result.channel_open(channel):
                    state, display, color = "开路", "--", "#b23a48"
                elif result.status != ImpedanceStatus.OK:
                    state, display, color = "失败", "--", "#b23a48"
                elif nominal is None:
                    state, display, color = "无效", "--", "#b23a48"
                else:
                    estimate = self.calibration.estimate(channel, nominal)
                    corrected = estimate.display_ohm
                    if estimate.model_status == ImpedanceModelStatus.OUT_OF_RANGE:
                        state, display, color = "超量程", "--", "#b23a48"
                    elif estimate.model_status == ImpedanceModelStatus.INVALID_RAW or corrected is None:
                        state, display, color = "无效", "--", "#b23a48"
                    elif corrected < 10_000.0:
                        display = f"{'' if calibration.calibrated else '≈'}{corrected / 1000.0:.1f}"
                        state, color = "良好", "#287a4d"
                    elif corrected <= 50_000.0:
                        display = f"{'' if calibration.calibrated else '≈'}{corrected / 1000.0:.1f}"
                        state, color = "可用", "#9a7b16"
                    else:
                        display = f"{'' if calibration.calibrated else '≈'}{corrected / 1000.0:.1f}"
                        state, color = "偏高", "#b5651d"
                self._set_table_item(channel, 1, state, color)
                self._set_table_item(channel, 2, display, color)
                self._set_table_item(channel, 3, "--" if nominal is None else f"{nominal / 1000.0:.1f}")
                self._set_table_item(channel, 4, "--" if excitation is None else f"{excitation / 1000.0:.1f}")
                self._set_table_item(channel, 5, "已校准" if calibration.calibrated else "估算")
            flag_names = [flag.name for flag in ImpedanceFlags if flag and result.flags & flag]
            self.impedance_status.setText(
                f"请求 0x{result.request_id:08X}：{result.status.name}，耗时 {result.duration_ms} ms，"
                f"样本 {result.sample_count}，flags={','.join(flag_names) or '0'}"
            )
            if result.status == ImpedanceStatus.RESTORE_FAILED:
                self.restore_failed = True
                self._set_state("error", "ADS1299恢复失败，请重启设备或重新连接")
            elif result.status == ImpedanceStatus.OK:
                self._set_state("eeg" if self.connected else "listening", "阻抗检测完成，等待EEG恢复")
            else:
                self._set_state("error", f"阻抗检测返回 {result.status.name}")
            self._update_impedance_button()
            self._update_bias_controls()

        def _save_impedance_result(self) -> None:
            if self.last_impedance_result is None:
                return
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "保存阻抗结果", "impedance_result.json", "JSON (*.json)"
            )
            if not path:
                return
            try:
                document = build_impedance_report(self.last_impedance_result, self.calibration)
                Path(path).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except (OSError, ValueError) as error:
                QtWidgets.QMessageBox.critical(self, "保存失败", str(error))

        def _apply_two_point_calibration(self) -> None:
            channel = self.calibration_channel.currentIndex()
            try:
                self.calibration.set_two_point(
                    channel,
                    self.calibration_low.value() * 1000.0,
                    self.calibration_high.value() * 1000.0,
                )
            except ValueError as error:
                QtWidgets.QMessageBox.critical(self, "校准失败", str(error))
                return
            self.calibration_status.setText(f"CH{channel + 1}两点校准已更新，尚未保存")
            if self.last_impedance_result is not None:
                self._handle_impedance_result(self.last_impedance_result)

        def _load_calibration_dialog(self) -> None:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "加载阻抗校准", str(self.calibration_path), "JSON (*.json)"
            )
            if not path:
                return
            calibration, warning = ImpedanceCalibration.load_or_default(path)
            self.calibration = calibration
            if warning:
                if calibration.load_warning:
                    self.calibration_path = Path(path)
                    self.calibration_status.setText(f"旧版未校准文件已升级，建议另存version 2：{warning}")
                    self.calibration_status.setStyleSheet("color:#d9a441")
                else:
                    self.calibration_status.setText(f"校准文件异常，已回退version 2默认值：{warning}")
                    self.calibration_status.setStyleSheet("color:#ff6b6b")
                if self.last_impedance_result is not None:
                    self._handle_impedance_result(self.last_impedance_result)
                return
            self.calibration_path = Path(path)
            self.calibration_status.setStyleSheet("")
            self.calibration_status.setText(str(self.calibration_path))
            if self.last_impedance_result is not None:
                self._handle_impedance_result(self.last_impedance_result)

        def _save_calibration_dialog(self) -> None:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "保存阻抗校准", str(self.calibration_path), "JSON (*.json)"
            )
            if not path:
                return
            try:
                self.calibration.save(path)
            except (OSError, ValueError) as error:
                QtWidgets.QMessageBox.critical(self, "保存失败", str(error))
                return
            self.calibration_path = Path(path)
            self.calibration_status.setStyleSheet("")
            self.calibration_status.setText(str(self.calibration_path))

        def _toggle_recording(self) -> None:
            if self.writer is not None:
                self.writer.close()
                self.record_status.setText(
                    f"记录：已停止，批次 {self.writer.written}，丢弃 {self.writer.dropped}"
                )
                self.writer = None
                self.record_button.setText("开始 JSONL 记录")
                self._update_record_controls()
                return
            if self.self_test_active or self.self_test_pending or self.bias_pending:
                return
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "保存JSONL", "capture.jsonl", "JSON Lines (*.jsonl)"
            )
            if path:
                self.current_record_path = Path(path)
                self.writer = JsonlWriter(self.current_record_path)
                self.writer.start()
                self.record_button.setText("停止记录")
                self.record_path_label.setText(f"文件：{self.current_record_path}")
                self._update_record_controls()

        def _choose_jsonl_source(self) -> str:
            if self.current_record_path is not None and self.writer is None and self.current_record_path.exists():
                return str(self.current_record_path)
            source, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择JSONL", "", "JSON Lines (*.jsonl)")
            return source

        def _export_json(self) -> None:
            if self.self_test_active or self.self_test_pending:
                return
            source = self._choose_jsonl_source()
            if not source:
                return
            target, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "导出JSON", str(Path(source).with_suffix(".json")), "JSON (*.json)"
            )
            if target:
                self._run_export(export_jsonl_to_json, source, target, "批次")

        def _export_csv(self) -> None:
            if self.self_test_active or self.self_test_pending:
                return
            source = self._choose_jsonl_source()
            if not source:
                return
            target, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "导出CSV", str(Path(source).with_suffix(".csv")), "CSV (*.csv)"
            )
            if target:
                self._run_export(export_jsonl_to_csv, source, target, "样本")

        def _run_export(self, exporter, source: str, target: str, unit: str) -> None:
            try:
                count = exporter(source, target)
                self.record_status.setText(f"已导出 {count} {unit}到 {target}")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                QtWidgets.QMessageBox.critical(self, "导出失败", str(error))

        def smoke_validate(self) -> None:
            assert len(self.plots) == CHANNELS
            assert len(set(CHANNEL_COLORS)) == CHANNELS
            assert self.tabs.count() == 6
            assert self.http_ip_edit.text() == self.http_settings.server_ip
            assert self.http_port_edit.value() == self.http_settings.port
            assert self.http_endpoint_edit.text() == self.http_settings.endpoint
            assert self.http_button.isEnabled()
            assert self.filter_combo.currentData() == FilterMode.RAW.value
            assert self.display_filter.mode == FilterMode.RAW
            assert self.reference_combo.currentData() == ReferenceMode.RAW.value
            assert self.display_reference.mode == ReferenceMode.RAW
            assert not self.force_markers_enabled
            assert not self.force_marker_button.isChecked()
            assert self.force_marker_button.text() == "Marker覆盖：关闭"
            assert not self.impedance_button.isEnabled()
            assert not self.self_test_button.isEnabled()
            assert self.impedance_table.rowCount() == CHANNELS
            assert self.self_test_table.rowCount() == CHANNELS
            assert self.quality_table.rowCount() == CHANNELS
            assert len(self.bias_checkboxes) == CHANNELS
            assert len(self.reference_checkboxes) == CHANNELS
            assert self.impedance_table.horizontalHeaderItem(2).text() == "电极 kΩ"
            assert self.impedance_table.horizontalHeaderItem(3).text() == "原始 kΩ"
            samples = np.zeros((20, CHANNELS), dtype=np.int32)
            markers = np.zeros(20, dtype=np.uint16)
            markers[0] = 1
            self.model.ingest(EegBatch(1, 1, 0, 1_783_985_595_550_000, samples, markers))
            self._refresh_plots()
            assert all(len(lines) == 1 for lines in self.marker_lines)
            self.connected = True
            self.has_valid_eeg = True
            self.peer_ip = "127.0.0.1"
            self._update_impedance_button()
            self._update_self_test_button()
            assert self.impedance_button.isEnabled()
            assert self.self_test_button.isEnabled()
            self._set_self_test_active(True)
            assert not self.self_test_banner.isHidden()
            assert not self.impedance_button.isEnabled()
            assert not self.record_button.isEnabled()
            assert not self.http_button.isEnabled()
            self._set_self_test_active(False)
            result = ImpedanceResultData(
                request_id=1,
                status=ImpedanceStatus.OK,
                tested_mask=0xFF,
                open_mask=0x04,
                flags=ImpedanceFlags.DC_VALID | ImpedanceFlags.AC_VALID,
                duration_ms=1800,
                sample_count=1024,
                nominal_ohm=(9000, 12000, None, 30000, 47000, 1_000_000, 7500, 22000),
                excitation_nv_peak=(275000, 366000, None, 917000, 1436000, 1558000, 229000, 672000),
            )
            self._handle_impedance_result(result)
            assert self.impedance_table.item(2, 1).text() == "开路"
            assert self.impedance_table.item(0, 2).text().startswith("≈")
            assert self.impedance_table.item(5, 1).text() == "超量程"

        def closeEvent(self, event) -> None:
            self.timer.stop()
            self._stop_http_push()
            self._stop_bias_client()
            if self.impedance_client is not None:
                self.impedance_client.stop()
                self.impedance_client.join(timeout=1.0)
            if self.self_test_client is not None:
                self.self_test_client.stop()
                self.self_test_client.join(timeout=1.0)
            self._stop_receiver()
            self.network_panel.close()
            if self.writer is not None:
                self.writer.close()
                self.writer = None
            event.accept()

else:
    class MonitorWindow:  # pragma: no cover - dependency error path.
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PyQt5 and pyqtgraph are required for the P300 monitor")


def run_monitor(
    host: str,
    port: int,
    smoke_test: bool = False,
    impedance_port: int = DEFAULT_IMPEDANCE_PORT,
    calibration_path: Path | str = DEFAULT_CALIBRATION_PATH,
    http_settings_path: Path | str = DEFAULT_HTTP_SETTINGS_PATH,
    discovery_port: int = 1236,
) -> int:
    if QtWidgets is None:
        raise RuntimeError("PyQt5 and pyqtgraph are required; install tools/requirements-p300-tools.txt")
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MonitorWindow(host, port, impedance_port, calibration_path, http_settings_path, discovery_port=discovery_port)
    if smoke_test:
        window.smoke_validate()
    window.show()
    if smoke_test:
        QtCore.QTimer.singleShot(200, window.close)
    return application.exec_()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--discovery-port", type=int, default=1236)
    parser.add_argument("--impedance-port", type=int, default=DEFAULT_IMPEDANCE_PORT)
    parser.add_argument("--calibration", default=str(DEFAULT_CALIBRATION_PATH))
    parser.add_argument("--http-settings", default=str(DEFAULT_HTTP_SETTINGS_PATH))
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    raise SystemExit(
        run_monitor(
            args.host,
            args.port,
            args.smoke_test,
            args.impedance_port,
            args.calibration,
            args.http_settings,
            args.discovery_port,
        )
    )


if __name__ == "__main__":
    main()
