"""Swap-paced OpenGL stimulus. Timers refresh text only, never toggle the square."""
from collections import deque
from dataclasses import replace
import math
import statistics
import time
from PyQt5.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PyQt5.QtGui import QOpenGLWindow, QPainter
from .config import stimulus_white
from .display import draw_scene, frequency_buttons
from .diagnostic_view import render_panel
from .cca_chart import render_cca_panel


class FrameClock:
    def __init__(self):
        self.last = None
        self.intervals = deque(maxlen=420)
        self.period = None
        self.frames = 0
        self.missed = 0

    def swap(self, now):
        dt = None if self.last is None else now - self.last
        if dt is not None and dt > 0:
            self.intervals.append(dt)
            if self.period and dt > 1.5 * self.period:
                self.missed += max(0, round(dt / self.period) - 1)
        self.last = now
        self.frames += 1
        return dt

    def calibrate(self):
        if len(self.intervals) < 10:
            raise ValueError("换帧样本不足，无法验证显示时序")
        self.period = statistics.median(self.intervals)
        return 1 / self.period

    def fps(self):
        return len(self.intervals) / sum(self.intervals) if self.intervals else 0


class StimulusWindow(QOpenGLWindow):
    def __init__(self, config, screen, worker, log):
        super().__init__(QOpenGLWindow.NoPartialUpdate)
        self.setTitle("SSVEP 实时测试 · ESC 退出")
        self.setScreen(screen)
        self.config, self.worker, self.log = config.validate(), worker, log
        self.pending_frequency = None
        self.painted_frequency = config.frequency
        self.painted_requested_unix = None
        self.switch_requested_unix = None
        self.trial_id = 0
        self.screen_refresh = screen.refreshRate()
        self.clock = FrameClock()
        self.result = {"valid": False, "status": "测量换帧率…"}
        self.timing = "正在测量换帧率…"
        self.warmup_start = None
        self.epoch = None
        self.running = False
        self.begin_pending = False
        self.analysis_epoch = None
        self.phase = "baseline" if config.calibrate_before_run else "test"
        self.panel = None
        self.show_diagnostics = True
        self.panel_mode = "cca"
        self.diagnostic_channel = config.channels[0]
        self.failed = False
        self.painted_white = False
        self.painted_phase = 0
        self.frameSwapped.connect(self.swapped)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self.check_gl)
        self.watchdog.start(5000)

    def check_gl(self):
        if self.clock.frames < 10:
            self.failed = True
            self.result = {"valid": False, "status": "OpenGL 无法持续换帧；请检查显卡驱动"}
            self.log.put("result", self.result)
            self.close()
            QCoreApplication.quit()

    def paintGL(self):
        self.painted_phase = 0
        self.painted_white = False
        if self.running:
            # Predict the upcoming swap by one observed refresh period. Re-evaluate
            # absolute phase after any late frame; never accumulate timer drift.
            periods = max(1, math.ceil((time.perf_counter() - self.clock.last) / self.clock.period))
            next_swap = self.clock.last + periods * self.clock.period
            self.painted_phase = max(0, next_swap - self.epoch)
            self.painted_frequency = self.config.frequency if self.pending_frequency is None else self.pending_frequency
            if self.pending_frequency is not None:
                self.painted_phase = 0
                self.painted_requested_unix = self.switch_requested_unix
            self.painted_white = None if self.phase == "baseline" else stimulus_white(self.painted_frequency, self.painted_phase)
        p = QPainter(self)
        paint_config = self.config if self.pending_frequency is None else replace(self.config, frequency=self.pending_frequency)
        draw_scene(p, self.width(), self.height(), self.devicePixelRatio(), paint_config,
                   self.painted_white, self.result, self.timing, panel=self.panel if self.show_diagnostics else None)
        p.end()

    def swapped(self):
        now = time.perf_counter()
        dt = self.clock.swap(now)
        if self.warmup_start is None:
            self.warmup_start = now
        if self.running and self.painted_frequency != self.config.frequency:
            old = self.config.frequency
            self.config.frequency = self.painted_frequency
            self.config.validate()
            if self.pending_frequency == self.painted_frequency:
                self.pending_frequency = None
            self.trial_id += 1
            self.epoch = now
            self.phase = "test"
            onset = time.time()
            self.analysis_epoch = self.worker.begin(timestamp=onset, mode="test", frequency=self.config.frequency,
                trial_id=self.trial_id, transition_seconds=self.config.switch_transition_seconds)
            self.begin_pending = False
            self.log.put("event", {"event": "frequency_change", "unix": onset,
                "requested_unix": self.painted_requested_unix, "old_frequency": old,
                "target_frequency": self.config.frequency, "trial_id": self.trial_id,
                "analysis_epoch": self.analysis_epoch, "swap_monotonic": now})
        elif self.begin_pending:
            if self.config.analysis_profile == "matlab":
                self.trial_id += 1
                self.analysis_epoch = self.worker.begin(mode="test", frequency=self.config.frequency,
                    trial_id=self.trial_id, transition_seconds=self.config.switch_transition_seconds)
                self.log.put("event", {"event": "trial_start", "unix": time.time(),
                    "target_frequency": self.config.frequency, "trial_id": self.trial_id,
                    "analysis_epoch": self.analysis_epoch})
            else:
                self.analysis_epoch = self.worker.begin(mode=self.phase)
            self.begin_pending = False
        elif self.running and dt is not None and dt > (1.0 if self.phase == "baseline" else max(0.1, 5 * self.clock.period)):
            # EEG may still be arriving while the window is occluded or blocked.
            # Static gray has no phase to interrupt: a short chart/glyph upload
            # must not erase a 40 s baseline. Long static interruptions still reset.
            self.analysis_epoch = self.worker.begin(mode=self.phase)
            self.result = {"valid": False, "status": "静态显示长时中断，重新采集基线" if self.phase == "baseline" else "刺激换帧中断，重新积累 EEG"}
            self.panel = None
            self.log.put("event", {"event": "stimulus_gap", "unix": time.time(),
                                   "gap_seconds": dt, "analysis_epoch": self.analysis_epoch})
        if not self.running and not self.failed and now - self.warmup_start >= 1.0:
            try:
                measured = self.clock.calibrate()
                if not 20 <= measured <= 1000:
                    raise ValueError("换帧率异常，无法确认垂直同步")
                if self.screen_refresh > 20 and measured > self.screen_refresh * 1.3:
                    raise ValueError("换帧回调快于系统刷新率，垂直同步可能未生效")
                maximum = max(f for f in self.config.switch_frequencies if 10 <= f < 27.25) if self.config.analysis_profile == "matlab" else self.config.frequency
                if maximum >= measured / 2:
                    raise ValueError(f"最高切频点 {maximum:g} Hz 必须低于实测换帧率 {measured:.1f} Hz 的一半")
                self.epoch = now + self.clock.period
                self.running, self.begin_pending = True, True
                self.log.put("event", {"event": "stimulus_start", "unix": time.time(),
                    "measured_swap_rate": measured, "system_refresh": self.screen_refresh})
            except ValueError as exc:
                self.failed = True
                self.result = {"valid": False, "status": str(exc) + "；ESC 退出"}
                self.log.put("result", self.result)
        if self.running:
            self.log.put("frame", (self.clock.frames, now, None if dt is None else dt * 1000,
                                   -1 if self.painted_white is None else int(self.painted_white), self.painted_phase,
                                   self.clock.missed, self.painted_frequency, self.trial_id))
        self.update()

    def poll(self):
        new = self.worker.poll()
        if new is not None and self.running and not self.begin_pending and self.pending_frequency is None and new.get("analysis_epoch") == self.analysis_epoch:
            self.result = new
            self.log.put("result", new)
            self.refresh_panel()
            if new.get("action") == "start_response":
                self.begin_phase("response")
            elif new.get("action") == "start_test":
                self.begin_phase("test")
        if self.running:
            if not self.worker.process.is_alive():
                self.result = {"valid": False, "status": "分析进程已退出，请按 ESC 重启测试"}
                self.refresh_panel()
            received = self.result.get("received_monotonic")
            data_end = self.result.get("data_end")
            if self.result.get("valid") and (received is None or time.monotonic() - received > self.config.stale_seconds
                                            or data_end is None or time.time() - data_end > self.config.stale_seconds):
                self.result = {"valid": False, "status": "EEG 已断流，等待新数据"}
                self.refresh_panel()
            elif self.result.get("valid"):
                self.result["age_ms"] = max(0, time.time() - data_end) * 1000
        fps = self.clock.fps()
        mismatch = abs(fps - self.config.expected_refresh) > self.config.expected_refresh * 0.08
        self.timing = f"换帧 {fps:.1f} Hz  |  估计漏帧 {self.clock.missed}"
        if mismatch and self.running:
            self.timing += f"  ·  未达到 {self.config.expected_refresh:g} Hz"
        elif self.running and self.clock.period and self.clock.intervals and max(self.clock.intervals) > self.clock.period * 1.5:
            self.timing += "  ·  近期换帧不稳定"
        if self.log.error:
            self.timing += "  ·  日志写入失败"
        elif self.log.dropped:
            self.timing += f"  ·  日志丢弃 {self.log.dropped}"
        raw_status = self.result.get("raw_recording", {})
        if raw_status.get("error") or raw_status.get("dropped_packets"):
            self.timing += "  ·  原始记录写入异常"

    def begin_phase(self, phase):
        self.phase = phase
        self.epoch = self.clock.last + self.clock.period
        self.begin_pending = True
        self.panel = None
        labels = {"baseline": "静态灰块：保持睁眼，等待基线数据", "response": "注视闪烁块：通道标定中", "test": "标定结束，积累新的连续测试 EEG"}
        self.result = {"valid": False, "status": labels[phase]}
        self.log.put("event", {"event": "phase_change", "phase": phase, "unix": time.time()})

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            QCoreApplication.quit()
        elif event.key() in (Qt.Key_B, Qt.Key_D):
            selected = "cca" if event.key() == Qt.Key_B else "quality"
            self.show_diagnostics = not self.show_diagnostics if selected == self.panel_mode else True
            self.panel_mode = selected
            self.refresh_panel()
        elif Qt.Key_1 <= event.key() <= Qt.Key_8:
            self.diagnostic_channel = event.key() - Qt.Key_0
            self.refresh_panel()
        elif event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Home) and self.config.analysis_profile == "matlab":
            frequencies = tuple(f for f in self.config.switch_frequencies if 10 <= f < 27.25)
            current = self.config.frequency if self.pending_frequency is None else self.pending_frequency
            index = frequencies.index(current)
            if event.key() == Qt.Key_Home:
                frequency = self.config.frequency_center
            else:
                index = min(len(frequencies) - 1, max(0, index + (1 if event.key() == Qt.Key_Right else -1)))
                frequency = frequencies[index]
            self.request_frequency(frequency)
        elif event.key() == Qt.Key_R and self.config.analysis_profile != "matlab" and self.running and not self.failed:
            self.begin_phase("baseline")
        else:
            super().keyPressEvent(event)

    def request_frequency(self, frequency):
        if not self.running or self.failed or self.config.analysis_profile != "matlab":
            return
        if frequency not in self.config.switch_frequencies or not 10 <= frequency < 27.25:
            return
        current = self.config.frequency if self.pending_frequency is None else self.pending_frequency
        if frequency == current:
            return
        if frequency == self.config.frequency and self.painted_frequency == self.config.frequency:
            self.pending_frequency = None
            self.result = {"valid": False, "status": "已取消未呈现的切频，等待当前分析结果"}
            self.update()
            return
        self.pending_frequency = frequency
        self.switch_requested_unix = time.time()
        self.result = {"valid": False, "status": f"切换至 {frequency:g} Hz：过渡 {self.config.switch_transition_seconds:g} 秒，再积累 {self.config.window_seconds:g} 秒新 EEG"}
        self.refresh_panel(target=frequency)
        self.update()

    def refresh_panel(self, target=None):
        frequency = self.config.frequency if target is None else target
        if self.panel_mode == "cca":
            expected = tuple(round(frequency + .5 * k, 6) for k in range(-8, 9)) if self.config.analysis_profile == "matlab" else self.config.candidates
            self.panel = render_cca_panel(self.result, frequency, expected)
        else:
            self.panel = render_panel(self.result, self.diagnostic_channel, frequency)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            for rect, frequency in frequency_buttons(self.width(), self.height(), self.config):
                if rect.contains(event.localPos()):
                    self.request_frequency(frequency)
                    return
        super().mousePressEvent(event)

    def event(self, event):
        if event.type() == QEvent.Close:
            QTimer.singleShot(0, QCoreApplication.quit)
        return super().event(event)
