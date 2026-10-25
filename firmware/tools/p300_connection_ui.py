"""USB provisioning and Windows hotspot assistant for the existing Monitor."""
from __future__ import annotations
import queue
import ipaddress
import re
import threading
from PyQt5 import QtCore, QtWidgets
from tools.p300_serial import SerialSession, serial_ports
from tools.p300_discovery import new_pairing
from tools.p300_windows_network import (
    ProtectedSettings, current_personal_wifi, hotspot_credentials, hotspot_status,
    network_interfaces, open_hotspot_settings, set_hotspot_enabled,
)

class ConnectionPanel(QtWidgets.QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.events = owner.events
        self.session = None
        self.busy = False
        self.closed = False
        self.store = ProtectedSettings()
        try:
            self.stored = self.store.load()
        except (OSError, RuntimeError, ValueError):
            self.stored = {}
        self.pairing = self.stored.get("pairing") or new_pairing()
        self.actions = []
        layout = QtWidgets.QVBoxLayout(self)
        usb = QtWidgets.QGroupBox("隔离 USB：配网、配对和串口 EEG")
        grid = QtWidgets.QGridLayout(usb)
        self.com = QtWidgets.QComboBox()
        self.baud = QtWidgets.QComboBox()
        self.baud.addItems(["921600", "460800"])
        refresh = self.button("刷新串口", self.refresh_ports)
        self.connect_button = self.button("USB 连接并配对", self.connect_usb)
        grid.addWidget(self.com, 0, 0, 1, 2)
        grid.addWidget(self.baud, 0, 2)
        grid.addWidget(refresh, 0, 3)
        grid.addWidget(self.connect_button, 1, 0, 1, 4)
        self.usb_status = QtWidgets.QLabel("USB 未连接；请选择串口后点击连接并配对")
        self.usb_status.setWordWrap(True)
        grid.addWidget(self.usb_status, 2, 0, 1, 4)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Wi-Fi（TCP）", "wifi")
        self.mode.addItem("USB 串口（关闭 Wi-Fi）", "serial")
        grid.addWidget(self.mode, 3, 0, 1, 2)
        grid.addWidget(self.button("应用数据模式", self.apply_mode), 3, 2, 1, 2)
        layout.addWidget(usb)
        layout.addWidget(owner._build_gain_group())

        wifi = QtWidgets.QGroupBox("USB 下发 Wi-Fi：无需修改源码或烧录")
        form = QtWidgets.QGridLayout(wifi)
        self.source = QtWidgets.QComboBox()
        self.source.addItems(["电脑共享热点（校园网推荐）", "普通个人 Wi-Fi"])
        self.source.currentIndexChanged.connect(self.select_endpoint_for_source)
        self.ssid = QtWidgets.QComboBox()
        self.ssid.setEditable(True)
        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password.setPlaceholderText("热点密码，不是校园网登录密码")
        self.endpoint = QtWidgets.QComboBox()
        self.endpoint.setEditable(True)
        self.endpoint.setPlaceholderText("热点侧 PC IP；刷新后选择，可手填")
        self.remember = QtWidgets.QCheckBox("使用 Windows 用户级加密保存此次网络配置")
        network = self.stored.get("network", {})
        if isinstance(network, dict):
            self.ssid.setEditText(network.get("ssid", ""))
            self.password.setText(network.get("password", ""))
        form.addWidget(QtWidgets.QLabel("配置来源"), 0, 0)
        form.addWidget(self.source, 0, 1, 1, 3)
        form.addWidget(QtWidgets.QLabel("网络 SSID"), 1, 0)
        form.addWidget(self.ssid, 1, 1, 1, 3)
        form.addWidget(QtWidgets.QLabel("网络密码"), 2, 0)
        form.addWidget(self.password, 2, 1, 1, 3)
        form.addWidget(QtWidgets.QLabel("PC 数据地址"), 3, 0)
        form.addWidget(self.endpoint, 3, 1, 1, 3)
        form.addWidget(self.button("授权读取当前个人 Wi-Fi", self.copy_personal), 4, 0, 1, 2)
        form.addWidget(self.button("扫描设备附近 Wi-Fi", self.scan_wifi), 4, 2, 1, 2)
        form.addWidget(self.button("通过 USB 下发并验证网络", self.send_network), 5, 0, 1, 3)
        form.addWidget(self.button("清除设备网络", self.clear_network), 5, 3)
        form.addWidget(self.remember, 6, 0, 1, 4)
        hint = QtWidgets.QLabel("共享热点名称/密码须与 Windows 设置一致；校园网账号只用于电脑登录。下发网络会暂停当前输出，验证后再应用 Wi-Fi 模式。")
        hint.setWordWrap(True)
        form.addWidget(hint, 7, 0, 1, 4)
        self.wifi_status = QtWidgets.QLabel("无线：等待已配对设备发现与时钟同步")
        self.wifi_status.setWordWrap(True)
        form.addWidget(self.wifi_status, 8, 0, 1, 4)
        layout.addWidget(wifi)

        hotspot = QtWidgets.QGroupBox("共享 Wi-Fi 助手：校园 WLAN → PC 热点 → 板子")
        hotspot_grid = QtWidgets.QGridLayout(hotspot)
        hotspot_grid.addWidget(self.button("打开 Windows 热点设置", self.open_settings), 0, 0)
        hotspot_grid.addWidget(self.button("检测网络/热点能力", self.detect_hotspot), 0, 1)
        self.hotspot_start = self.button("开启热点", lambda: self.change_hotspot(True))
        self.hotspot_stop = self.button("关闭热点", lambda: self.change_hotspot(False))
        self.hotspot_read = self.button("授权读取热点配置", self.copy_hotspot)
        hotspot_grid.addWidget(self.hotspot_start, 1, 0)
        hotspot_grid.addWidget(self.hotspot_stop, 1, 1)
        hotspot_grid.addWidget(self.hotspot_read, 2, 0, 1, 2)
        self.hotspot_available = False
        self.hotspot_info = QtWidgets.QLabel("先检测能力；自动管理不可用时仍可打开系统设置并手填一次。接口存在不代表热点已开启。")
        self.hotspot_info.setWordWrap(True)
        hotspot_grid.addWidget(self.hotspot_info, 3, 0, 1, 2)
        layout.addWidget(hotspot)
        self.operation_status = QtWidgets.QLabel("首次配网/验证时请断开人体电极；不要旁路板上隔离。")
        self.operation_status.setWordWrap(True)
        layout.addWidget(self.operation_status)
        self.wifi_activate_button = self.button("启用 Wi-Fi 数据模式", self.activate_wifi_mode)
        layout.addWidget(self.wifi_activate_button)
        self.device_log = QtWidgets.QPlainTextEdit()
        self.device_log.setReadOnly(True)
        self.device_log.setMaximumBlockCount(200)
        self.device_log.setMaximumHeight(110)
        self.device_log.setPlaceholderText("设备诊断日志（不写入 EEG、记录或 HTTP）")
        layout.addWidget(self.device_log)
        layout.addStretch()
        self.refresh_ports()
        self.update_buttons()

    def button(self, text, callback):
        button = QtWidgets.QPushButton(text)
        button.clicked.connect(callback)
        self.actions.append(button)
        return button

    def refresh_ports(self):
        selected = self.com.currentData()
        self.com.clear()
        try:
            for port, description in serial_ports():
                self.com.addItem(f"{port} — {description}", port)
            index = self.com.findData(selected)
            if index < 0:
                index = next((i for i in range(self.com.count()) if "CP210" in self.com.itemText(i).upper()), -1)
            if index >= 0:
                self.com.setCurrentIndex(index)
        except ImportError:
            self.operation_status.setText("缺少 pyserial，请安装工具依赖")

    def update_buttons(self):
        for action in self.actions:
            action.setEnabled(not self.busy)
        for action in (self.hotspot_start, self.hotspot_stop, self.hotspot_read):
            action.setEnabled(not self.busy and self.hotspot_available)
        self.wifi_activate_button.setEnabled(not self.busy and self.session is not None and self.session.connected)
        self.mode.setEnabled(not self.busy)
        self.com.setEnabled(not self.busy and self.session is None)
        self.baud.setEnabled(not self.busy and self.session is None)
        self.connect_button.setText("断开 USB" if self.session else "USB 连接并配对")

    def work(self, label, function):
        if self.busy:
            return
        self.busy = True
        self.operation_status.setText(label + "…")
        self.update_buttons()
        def run():
            try:
                result = function()
                event = ("network_done", {"label": label, "result": result})
            except Exception as error:
                # Do not include request documents/passwords in exception messages.
                event = ("network_done", {"label": label, "error": str(error)})
            if not self.closed:
                self.events.put(event)
        threading.Thread(target=run, daemon=True, name="p300-network-operation").start()

    def require_session(self):
        if self.session is None or not self.session.connected:
            raise RuntimeError("请先连接 USB；设备 UART 与烧录终端不能同时占用")
        return self.session

    def connect_usb(self):
        if self.session:
            if self.owner.data_transport == "serial":
                self.owner._pause_transport_output()
            session = self.session
            self.session = None
            self.usb_status.setText("正在断开 USB…")
            self.work("断开 USB", lambda: session.close())
            return
        port = self.com.currentData()
        baud = int(self.baud.currentText())
        if not port:
            self.operation_status.setText("没有可用串口")
            self.usb_status.setText("USB 未连接：没有可用串口，请检查连接后刷新")
            return
        self.usb_status.setText(f"正在连接 {port}：握手并配对…")
        def connect():
            session = SerialSession(port, baud, self.events)
            try:
                session.open()
                session.manage("pair", **self.pairing)
                self.stored["pairing"] = self.pairing
                warning = None
                try:
                    self.store.save(self.stored)
                except (OSError, RuntimeError):
                    warning = "当前 USB 会话可用，但 PC 配对未能持久化；下次需重新 USB 配对"
                if self.closed:
                    raise RuntimeError("窗口已关闭")
                self.session = session
                return {"connected": True, "warning": warning}
            except Exception:
                session.close()
                raise
        self.work("USB 配对", connect)

    def activate_wifi_mode(self):
        if self.session is None or not self.session.connected:
            self.operation_status.setText("请先连接 USB，再启用设备 Wi-Fi 数据模式")
            return
        self.mode.setCurrentIndex(self.mode.findData("wifi"))
        self.apply_mode()

    def apply_mode(self):
        selected = self.mode.currentData()
        self.owner._pause_transport_output()
        session = self.session
        if selected == "wifi" and (session is None or not session.connected):
            self.owner._activate_transport("wifi")
            return
        def change():
            link = self.require_session()
            link.manage("mode", mode=selected, timeout=4)
            return {"activate": selected}
        self.work("切换数据模式", change)

    def copy_personal(self):
        if QtWidgets.QMessageBox.question(self, "授权读取", "允许读取电脑当前个人 Wi-Fi 的 SSID/密码用于 USB 配网？校园网企业/网页登录不能直接复制。") != QtWidgets.QMessageBox.Yes:
            return
        self.work("读取个人 Wi-Fi", lambda: {"credentials": current_personal_wifi()})

    def copy_hotspot(self):
        if QtWidgets.QMessageBox.question(self, "授权读取", "允许读取 Windows 当前热点 SSID/密码用于板子配网？") != QtWidgets.QMessageBox.Yes:
            return
        self.work("读取热点配置", lambda: {"credentials": hotspot_credentials()})

    def scan_wifi(self):
        self.work("设备扫描", lambda: self.require_session().manage("scan", timeout=15))

    def send_network(self):
        ssid, password = self.ssid.currentText().strip(), self.password.text()
        host = self.endpoint.currentText().strip()
        index = self.endpoint.currentIndex()
        if index >= 0 and self.endpoint.currentText() == self.endpoint.itemText(index):
            host = self.endpoint.currentData() or host
        if not ssid or not host:
            self.operation_status.setText("请填写 SSID、密码，并检测/选择 PC 热点侧地址")
            return
        try:
            address = ipaddress.IPv4Address(host)
            if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
                raise ValueError
        except ValueError:
            self.operation_status.setText("请输入有效的 PC IPv4 地址")
            return
        self.owner._pause_transport_output()
        remember = self.remember.isChecked()
        port = int(self.owner.port_edit.value()) or 1234
        def send():
            link = self.require_session()
            result = link.manage("set_network", ssid=ssid, password=password, host=host, port=port, timeout=25)
            try:
                if remember:
                    self.stored["network"] = {"ssid": ssid, "password": password}
                    self.store.save(self.stored)
                elif "network" in self.stored:
                    self.stored.pop("network")
                    self.store.save(self.stored)
            except (OSError, RuntimeError):
                result["warning"] = "设备配置已验证并保存，但 PC 本地受保护配置写入失败"
            return result
        self.work("下发网络（最长约 20 秒）", send)

    def clear_network(self):
        if QtWidgets.QMessageBox.question(self, "清除网络", "清除设备保存的网络并切回串口？不会擦除其他 NVS 数据。") != QtWidgets.QMessageBox.Yes:
            return
        self.owner._pause_transport_output()
        self.work("清除设备网络", lambda: self.require_session().manage("clear_network", timeout=4))

    def open_settings(self):
        try:
            open_hotspot_settings()
        except (OSError, RuntimeError) as error:
            self.operation_status.setText(str(error))

    def detect_hotspot(self):
        self.work("检测共享网络", lambda: {"interfaces": network_interfaces(), "hotspot": hotspot_status()})

    def change_hotspot(self, enabled):
        if not enabled and QtWidgets.QMessageBox.question(self, "关闭热点", "关闭系统热点将中断通过它连接的设备，是否继续？") != QtWidgets.QMessageBox.Yes:
            return
        self.work("开启热点" if enabled else "关闭热点", lambda: set_hotspot_enabled(enabled))

    def select_endpoint_for_source(self):
        if not hasattr(self, "endpoint"):
            return
        hotspot = self.source.currentIndex() == 0
        self.password.setPlaceholderText("热点密码，不是校园网登录密码" if hotspot else "个人 Wi-Fi 密码")
        for i in range(self.endpoint.count()):
            role = self.endpoint.itemData(i, QtCore.Qt.UserRole+1) or {}
            if role.get("hotspot" if hotspot else "wlan"):
                self.endpoint.setCurrentIndex(i)
                return
        self.endpoint.setCurrentIndex(-1)
        self.endpoint.setEditText("")

    def handle(self, kind, result):
        # Ignore queued events from an already replaced/closed USB session.
        if kind.startswith("usb_") and "session" in result and result["session"] is not self.session:
            return
        if kind == "usb_log":
            message = re.sub(r"\x1b\[[0-9;]*m", "", str(result.get("message", ""))).rstrip()
            if message:
                self.device_log.appendPlainText(message)
        elif kind == "discovery":
            self.wifi_status.setText(f"已发现设备 {result['host']}；校时 {'已同步' if result.get('synchronized') else '等待'}" +
                                    ("；" + result["error"] if result.get("error") else ""))
        elif kind == "usb_status":
            self.usb_status.setText(f"USB 已连接 | 设备 {result.get('device_id', '--')} | 实际 {result.get('mode', '--')} | ADS {'运行' if result.get('ads_running') else '未启动'} | Wi-Fi {result.get('ip', '--')} | 校时 {'就绪' if result.get('time_ready') else '等待'}")
        elif kind == "usb_sync" and "rtt_us" in result:
            self.usb_status.setToolTip(f"串口同步 RTT {result['rtt_us']/1000:.2f} ms；不能据此保证刺激同步精度")
        elif kind == "usb_error":
            failed_session = self.session
            self.session = None
            if self.owner.data_transport == "serial":
                self.owner._pause_transport_output()
            if failed_session is not None:
                failed_session.close()
            self.usb_status.setText("USB 已断开：" + result["error"] + "；可重新连接并配对")
            self.operation_status.setText("USB 错误：" + result["error"])
            self.update_buttons()
        elif kind == "network_done":
            self.busy = False
            if "error" in result:
                self.operation_status.setText(result["label"] + "失败：" + result["error"])
                if result["label"] == "USB 配对":
                    self.usb_status.setText("USB 连接失败：" + result["error"])
                elif (result["label"] == "切换数据模式" and result["error"] == "mode_busy_or_unavailable"
                      and self.session is not None and self.session.last_status.get("ads_running") is False):
                    self.operation_status.setText("切换数据模式失败：ADS 未启动，当前固件无法切换数据模式；请检查 ADS 初始化日志（mode_busy_or_unavailable）")
            else:
                self.operation_status.setText(result["label"] + "完成；网络连上不等于 EEG/HTTP 已通过验证")
                data = result.get("result") or {}
                if data.get("connected") and self.session is not None:
                    # Handshake status may arrive before the worker publishes its
                    # session. Replay it even if ADS startup prevents clock updates.
                    if self.session.connected:
                        self.handle("usb_status", {**self.session.last_status, "session": self.session})
                    else:
                        self.handle("usb_error", {"error": "连接在配对完成前已中断", "session": self.session})
                elif result["label"] == "断开 USB":
                    self.usb_status.setText("USB 已断开，可重新连接并配对")
                if data.get("network_verified"):
                    self.operation_status.setText("网络已验证并保存；验证结束后 Wi-Fi 已关闭。保持 USB 连接，点击下方“启用 Wi-Fi 数据模式”建立持续连接。")
                    self.wifi_status.setText("Wi-Fi：网络验证通过，尚未启用 Wi-Fi 数据模式")
                if data.get("warning"):
                    self.operation_status.setText(self.operation_status.text() + "；" + data["warning"])
                if "credentials" in data:
                    self.ssid.setEditText(data["credentials"]["ssid"])
                    self.password.setText(data["credentials"]["password"])
                if "networks" in data:
                    previous = self.ssid.currentText()
                    self.ssid.clear()
                    self.ssid.addItems([ap["ssid"] for ap in data["networks"] if ap.get("ssid")])
                    self.ssid.setEditText(previous)
                if "interfaces" in data:
                    self.endpoint.clear()
                    descriptions = []
                    for interface in sorted(data["interfaces"], key=lambda i: not i.get("hotspot_candidate")):
                        descriptions.append(f"{interface['name']}: {', '.join(interface['addresses'])}")
                        for address in interface["addresses"]:
                            role = "热点候选" if interface.get("hotspot_candidate") else "其他接口"
                            self.endpoint.addItem(f"[{role}] {interface['name']} — {address}", address)
                            wlan = any(word in interface.get("description", "").lower() for word in ("wi-fi", "wireless", "802.11", "wlan"))
                            self.endpoint.setItemData(self.endpoint.count()-1,
                                {"hotspot": interface.get("hotspot_candidate", False), "wlan": wlan and not interface.get("hotspot_candidate")}, QtCore.Qt.UserRole+1)
                    self.select_endpoint_for_source()
                    capability = data["hotspot"]
                    self.hotspot_available = capability["available"]
                    self.hotspot_info.setText("\n".join(descriptions) + "\n" +
                        ("热点 API 可用，状态：" + capability["state"] if self.hotspot_available else capability["reason"]))
                if "activate" in data:
                    self.owner._activate_transport(data["activate"])
                    if data["activate"] == "wifi":
                        self.operation_status.setText("已启用 Wi-Fi 数据模式；等待设备取得 IP、校时和 EEG 数据")
                        self.wifi_status.setText("Wi-Fi 数据模式已启用；等待设备发现与时钟同步")
            self.update_buttons()

    def close(self):
        self.closed = True
        if self.session:
            self.session.close()
            self.session = None
