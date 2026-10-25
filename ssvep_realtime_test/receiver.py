"""Loopback-only Monitor HTTP input; all numerical work lives in a child process."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import socket
import threading
import time

HTTP_PATH = "/api/bci/data"
MAX_BYTES = 2_000_000
PHASES = {"baseline": 1, "response": 2, "test": 3}


def channel_metadata(payload):
    """Old packets implicitly contain physical CH1–CH8 in their original order."""
    mode = payload.get("channel_mode", "raw8")
    mapping = payload.get("channel_map", list(range(1, 9)))
    if not isinstance(mapping, list) or len(mapping) != 8 or any(type(ch) is not int for ch in mapping):
        raise ValueError("channel_map 必须为8项整数通道编号")
    if mode == "raw8":
        valid = mapping == list(range(1, 9))
    elif mode == "six_to_eight":
        valid = mapping[:6] == list(range(1, 7)) and all(1 <= ch <= 6 for ch in mapping[6:]) and mapping[6] != mapping[7]
    else:
        raise ValueError("未知的HTTP通道模式")
    if not valid:
        raise ValueError("channel_map 与HTTP通道模式不一致")
    return {"channel_mode": mode, "channel_map": list(mapping)}


def validate_packet(payload):
    if not isinstance(payload, dict):
        raise ValueError("需要 JSON 对象")
    data = payload.get("data")
    if not isinstance(data, list) or not 1 <= len(data) <= 4000:
        raise ValueError("data 必须包含 1–4000 个样本")
    if any(not isinstance(row, list) or len(row) != 8 or
           any(not isinstance(x, (float, int)) or isinstance(x, bool) for x in row) for row in data):
        raise ValueError("data 应为 N×8 数值数组，单位 µV")
    if any(math.isfinite(x) and abs(x) > 1e9 for row in data for x in row):
        raise ValueError("EEG 数值超出合理微伏范围")
    fs = payload.get("sampling_rate")
    ts = payload.get("timestamp")
    if fs not in (250, 500, 1000, 2000) or isinstance(fs, bool):
        raise ValueError("sampling_rate 支持 250/500/1000/2000")
    if not isinstance(ts, (float, int)) or not math.isfinite(ts) or ts <= 0:
        raise ValueError("timestamp 必须是首样本 Unix 秒时间戳")
    if payload.get("channels", 8) != 8:
        raise ValueError("channels 必须为 8")
    if "markers" in payload and (not isinstance(payload["markers"], list) or len(payload["markers"]) != len(data)):
        raise ValueError("markers 长度必须与 data 一致")
    if payload.get("units", "uV") not in ("uV", "µV", "μV"):
        raise ValueError("EEG 单位必须为微伏 uV")
    metadata = {}
    if "vref_v" in payload or "pga_gain" in payload:
        for key in ("vref_v", "pga_gain"):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError("vref_v/pga_gain 必须同时提供有效正数")
            metadata[key] = float(value)
        if not 0 < metadata["vref_v"] <= 10 or metadata["pga_gain"] not in (1, 2, 4, 6, 8, 12, 24):
            raise ValueError("ADS1299 参考电压或 PGA 增益超出支持范围")
    return {"data": data, "sampling_rate": int(fs), "timestamp": float(ts), "units": "uV",
            **({"markers": payload["markers"]} if "markers" in payload else {}),
            "device": payload.get("device", "unknown"), **metadata, **channel_metadata(payload),
            "simulation": payload.get("device") == "SSVEP_SIMULATOR"}


def create_server(port, packets):
    class ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address = False

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def reply(self, code, message):
            body = json.dumps({"ok": code == 200, "message": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if self.path != HTTP_PATH:
                self.reply(404, "路径应为 " + HTTP_PATH)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_BYTES:
                    self.reply(413, "数据长度无效或超过 2 MB")
                    return
                packet = validate_packet(json.loads(self.rfile.read(length)))
                packet["received"] = time.monotonic()
                packets.put_nowait(packet)
            except queue.Full:
                self.reply(429, "处理队列已满；请保持实时发送")
                return
            except (ValueError, TypeError, TimeoutError, OSError, OverflowError) as exc:
                self.reply(400, str(exc))
                return
            self.reply(200, "accepted")

        def log_message(self, *args):
            pass

    server = ExclusiveServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def latest_put(destination, value):
    try:
        destination.put_nowait(value)
    except queue.Full:
        try:
            destination.get_nowait()
        except queue.Empty:
            pass
        try:
            destination.put_nowait(value)
        except queue.Full:
            pass


def worker_main(settings_dict, results, ready, stop, start_epoch, phase_value, control, session_dir, calibration_path):
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    from .analysis import Analyzer
    from .config import Settings
    from .calibration import Calibration
    from .session import RawRecorder
    import numpy as np

    config = Settings(**settings_dict).validate()
    analyzer = Analyzer(config)
    calibration = Calibration(config, calibration_path)
    recorder = RawRecorder(session_dir) if session_dir and config.record_raw else None
    packets = queue.Queue(maxsize=25)
    try:
        server = create_server(config.port, packets)
    except OSError as exc:
        if recorder:
            recorder.close()
        ready.put({"ok": False, "error": f"HTTP 端口 {config.port} 无法监听：{exc}"})
        return
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    server_thread.start()
    ready.put({"ok": True, "port": server.server_port})
    expected = None
    stream_key = None
    last_received = None
    last_end = None
    changed = False
    next_update = 0
    resets = 0
    simulation = False
    status = "等待刺激开始"
    epoch_seen = 0
    try:
        while not stop.is_set():
            try:
                packet = packets.get(timeout=0.03)
            except queue.Empty:
                packet = None
            # Read the start/restart gate after the blocking receive. Otherwise
            # the very first post can be consumed under the previous epoch.
            with start_epoch.get_lock():
                epoch = start_epoch.value
                phase = next(k for k, v in PHASES.items() if v == phase_value.value)
                frequency, trial_id, stimulus_onset = control[:]
                trial_id = int(trial_id)
            if epoch != epoch_seen:
                config.frequency = frequency
                if config.analysis_profile == "matlab":
                    config.validate()
                    analyzer.references.clear()
                else:
                    config.candidates = tuple(sorted(set(config.candidates) | {frequency}))
                analyzer.reset()
                expected = None
                last_received = None
                last_end = None
                changed = False
                epoch_seen = epoch
                calibration.restart(phase)
                status = "切频过渡中，等待新频率 EEG" if epoch > time.time() else "等待 Monitor HTTP 数据"
            if packet is not None and recorder:
                # A packet may straddle a switch. Labels apply from this onset;
                # preserve all raw values and explicitly mark ambiguous packets.
                in_segment = epoch > 0 and packet["timestamp"] >= stimulus_onset
                recorder.put({**packet, "analysis_epoch": epoch, "phase": phase, "received_unix": time.time(),
                              "trial_id": trial_id if in_segment else None,
                              "target_frequency": frequency if in_segment else None,
                              "control_trial_id": trial_id, "control_frequency": frequency,
                              "stimulus_onset": stimulus_onset})
            if packet is not None and epoch > 0:
                data = np.array(packet["data"], dtype=float)
                fs, ts = packet["sampling_rate"], packet["timestamp"]
                end = ts + len(data) / fs
                # Reject backlogged/future packets. Source and receiver share the PC clock.
                if end < time.time() - config.stale_seconds or ts > time.time() + 0.5:
                    analyzer.reset()
                    calibration.confirmed_since = None
                    expected = None
                    changed = False
                    last_received = None
                    status = "EEG 时间戳过旧或超前，请检查 Monitor 时间同步"
                elif end > epoch:
                    if ts < epoch:
                        trim = min(len(data), max(0, math.ceil((epoch - ts) * fs)))
                        data, ts = data[trim:], ts + trim / fs
                    known = "vref_v" in packet
                    fullscale = packet["vref_v"] / packet["pga_gain"] * 1e6 if known else config.fullscale_uv
                    context = {"sampling_rate": fs, "simulation": packet["simulation"], "fullscale_uv": fullscale,
                               "metadata_known": known, "vref_v": packet.get("vref_v"), "pga_gain": packet.get("pga_gain"),
                               "device": packet["device"]}
                    if packet["channel_mode"] != "raw8":
                        # Keep legacy physical-channel calibration contexts
                        # compatible, while mapped inputs get a distinct scope.
                        context.update(channel_mode=packet["channel_mode"], channel_map=packet["channel_map"])
                    calibration.set_context(context)
                    analyzer.fullscale = fullscale
                    analyzer.range_source = "Monitor 实际量程" if known else "配置量程（未收到 Monitor 元信息）"
                    key = (fs, packet["simulation"], fullscale, known, packet.get("vref_v"), packet.get("pga_gain"),
                           packet["device"], tuple(packet["channel_map"]))
                    discontinuity = stream_key != key or (expected is not None and abs(ts - expected) > 2.5 / fs)
                    if discontinuity:
                        analyzer.reset()
                        calibration.confirmed_since = None
                        resets += 1
                    # Duplicates and reordered packets must not be appended twice.
                    if expected is not None and stream_key == key and ts < expected - 2.5 / fs:
                        expected = None
                        changed = False
                        last_received = None
                        status = "重复或乱序 EEG 包，重新积累"
                    elif len(data):
                        analyzer.feed(data, fs)
                        expected, stream_key = end, key
                        simulation = packet["simulation"]
                        last_received, last_end = packet["received"], end
                        changed = True
            now = time.monotonic()
            if last_received is not None and (now - last_received > config.stale_seconds or time.time() - last_end > config.stale_seconds):
                analyzer.reset()
                calibration.confirmed_since = None
                expected, last_received, changed = None, None, False
                status = "EEG 已断流，等待新数据"
            if now >= next_update:
                if changed:
                    result = analyzer.result(channels=calibration.channels)
                    changed = False
                elif last_received is None:
                    result = {"valid": False, "status": status}
                else:
                    continue
                if config.analysis_profile == "matlab":
                    match = result.get("valid", False) and result["cca_frequency"] == frequency
                    result.update(phase="test", detected=False, target_match=match,
                                  decision=("CCA估计匹配当前刺激（非可靠检出判据）" if match else
                                            "CCA估计未匹配当前刺激") if result.get("valid") else "等待新频率连续数据")
                    if result.get("target_excluded"):
                        result.update(target_match=False, decision="当前刺激16.5 Hz已屏蔽，只显示其他候选")
                else:
                    result = calibration.decorate(analyzer, result, last_end)
                if result.get("action") == "start_test" and session_dir:
                    (Path(session_dir) / "calibration.json").write_text(json.dumps(calibration.model, ensure_ascii=False, indent=2), encoding="utf-8")
                analysis_end = last_end
                if config.analysis_profile == "matlab" and analyzer.preprocessor is not None and last_end is not None:
                    analysis_end -= (analyzer.preprocessor.trailing_input_samples - analyzer.preprocessor.factor + 1) / analyzer.sampling_rate
                result.update(received_monotonic=last_received, data_end=analysis_end, source_data_end=last_end,
                              generated_monotonic=now, simulation=simulation, resets=resets,
                              analysis_epoch=epoch_seen, input_context=calibration.context,
                              target_frequency=frequency, trial_id=trial_id, stimulus_onset=stimulus_onset)
                result["excluded_frequencies"] = list(config.excluded_frequencies)
                result["candidate_center"] = frequency if config.analysis_profile == "matlab" else None
                if result.get("valid"):
                    result["analysis_data_start"] = analysis_end - config.window_seconds
                if recorder:
                    result["raw_recording"] = {"dropped_packets": recorder.dropped, "error": recorder.error}
                latest_put(results, result)
                next_update = now + config.update_seconds
    except Exception as exc:
        latest_put(results, {"valid": False, "status": f"分析进程出错：{type(exc).__name__}: {exc}"})
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)
        if recorder:
            recorder.close()
        results.cancel_join_thread()


class WorkerController:
    def __init__(self, config, session_dir=None, calibration_path=None):
        self.config = config.validate()
        ctx = mp.get_context("spawn")
        self.results, self.ready = ctx.Queue(maxsize=4), ctx.Queue(maxsize=1)
        self.stop_event = ctx.Event()
        self.start_epoch = ctx.Value("d", 0.0)
        self.phase_value = ctx.Value("i", PHASES["test"])
        self.control = ctx.Array("d", [config.frequency, 0, 0], lock=False)
        self.process = ctx.Process(target=worker_main, args=(config.to_dict(), self.results, self.ready,
            self.stop_event, self.start_epoch, self.phase_value, self.control,
            str(session_dir) if session_dir else None, str(calibration_path) if calibration_path else None), daemon=True)

    def start(self):
        self.process.start()
        try:
            status = self.ready.get(timeout=15)
        except queue.Empty:
            self.stop()
            raise RuntimeError("分析进程启动超时")
        if not status["ok"]:
            self.stop()
            raise RuntimeError(status["error"])
        return status

    def begin(self, timestamp=None, mode="test", frequency=None, trial_id=None, transition_seconds=0):
        onset = time.time() if timestamp is None else timestamp
        if frequency is not None:
            if frequency not in self.config.switch_frequencies or not 6 <= frequency <= 80:
                raise ValueError("切换频率不在固定±4 Hz频点范围")
            if self.config.analysis_profile == "focused" and frequency > 22:
                raise ValueError("此频点需改用宽带或MATLAB模式")
            if self.config.analysis_profile == "matlab" and not 10 <= frequency < 27.25:
                raise ValueError("当前刺激的CCA±4 Hz范围超出支持带宽")
        if not math.isfinite(onset) or not 0 <= transition_seconds <= 2:
            raise ValueError("无效的刺激开始/过渡时间")
        epoch = onset + transition_seconds
        with self.start_epoch.get_lock():
            if frequency is not None:
                self.control[0] = frequency
            if trial_id is not None:
                self.control[1] = trial_id
            self.control[2] = onset
            self.phase_value.value = PHASES[mode]
            self.start_epoch.value = epoch
        return epoch

    def poll(self):
        latest = None
        while True:
            try:
                latest = self.results.get_nowait()
            except queue.Empty:
                return latest

    def stop(self):
        self.stop_event.set()
        if self.process.pid is not None:
            self.process.join(timeout=3)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=2)
        for q in (self.results, self.ready):
            q.close()
