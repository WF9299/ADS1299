"""Non-blocking 200 ms HTTP JSON push support for the P300 monitor."""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tools.p300_common import CHANNELS, EegBatch
from tools.p300_self_test import CONFIG_NORMAL


HTTP_SETTINGS_VERSION = 1
HTTP_PACKET_SAMPLES = 200
HTTP_INPUT_BATCH_SAMPLES = 20
HTTP_TIMEOUT_S = 2.0
HTTP_QUEUE_PACKETS = 10
DEFAULT_HTTP_SERVER_IP = "10.201.1.182"
DEFAULT_HTTP_SERVER_PORT = 25000
DEFAULT_HTTP_ENDPOINT = "/api/bci/data"


@dataclass(frozen=True)
class HttpPushSettings:
    server_ip: str = DEFAULT_HTTP_SERVER_IP
    port: int = DEFAULT_HTTP_SERVER_PORT
    endpoint: str = DEFAULT_HTTP_ENDPOINT

    @classmethod
    def defaults(cls) -> "HttpPushSettings":
        return cls()

    def normalized(self) -> "HttpPushSettings":
        address_text = str(self.server_ip).strip()
        try:
            address = ipaddress.IPv4Address(address_text)
        except ipaddress.AddressValueError as error:
            raise ValueError(f"服务器IP无效：{address_text}") from error

        if isinstance(self.port, bool):
            raise ValueError("服务器端口无效")
        try:
            port = int(self.port)
        except (TypeError, ValueError) as error:
            raise ValueError("服务器端口无效") from error
        if not 1 <= port <= 65535:
            raise ValueError("服务器端口必须在1~65535之间")

        endpoint = str(self.endpoint).strip()
        if not endpoint or not endpoint.startswith("/"):
            raise ValueError("endpoint必须以/开头")
        if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in endpoint):
            raise ValueError("endpoint不能包含空白或控制字符")
        return HttpPushSettings(str(address), port, endpoint)

    @property
    def url(self) -> str:
        settings = self.normalized()
        return f"http://{settings.server_ip}:{settings.port}{settings.endpoint}"

    @classmethod
    def load_or_default(cls, path: Path | str) -> tuple["HttpPushSettings", str | None]:
        source = Path(path)
        if not source.exists():
            return cls.defaults(), None
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("配置根节点必须是JSON对象")
            if document.get("version") != HTTP_SETTINGS_VERSION:
                raise ValueError(f"不支持的HTTP配置版本：{document.get('version')!r}")
            settings = cls(
                document.get("server_ip", ""),
                document.get("port", 0),
                document.get("endpoint", ""),
            ).normalized()
            return settings, None
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return cls.defaults(), str(error)

    def save(self, path: Path | str) -> None:
        settings = self.normalized()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        document = {
            "version": HTTP_SETTINGS_VERSION,
            "server_ip": settings.server_ip,
            "port": settings.port,
            "endpoint": settings.endpoint,
        }
        try:
            temporary.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class HttpPacketAggregator:
    """Build non-overlapping 200-sample payloads from continuous 20-sample batches."""

    def __init__(self, target_samples: int = HTTP_PACKET_SAMPLES) -> None:
        if target_samples <= 0 or target_samples % HTTP_INPUT_BATCH_SAMPLES:
            raise ValueError("target_samples must be a positive multiple of 20")
        self.target_samples = int(target_samples)
        self._stream_id: int | None = None
        self._next_seq: int | None = None
        self._sampling_rate: int | None = None
        self._channels: int | None = None
        self._device: str | None = None
        self._timestamp: float | None = None
        self._data: list[list[float]] = []
        self._markers: list[int] = []

    @property
    def buffered_samples(self) -> int:
        return len(self._data)

    def reset(self) -> None:
        self._stream_id = None
        self._next_seq = None
        self._sampling_rate = None
        self._channels = None
        self._device = None
        self._clear_payload()

    def _clear_payload(self) -> None:
        self._timestamp = None
        self._data = []
        self._markers = []

    @staticmethod
    def _validate_record(record: dict) -> tuple[list[list[float]], list[int], int, int, str, float] | None:
        try:
            data = record["data"]
            markers = record["markers"]
            sampling_rate = int(record["sampling_rate"])
            channels = int(record["channels"])
            device = str(record["device"])
            timestamp = float(record["timestamp"])
        except (KeyError, TypeError, ValueError):
            return None
        if sampling_rate != 1000 or channels != CHANNELS:
            return None
        if not isinstance(data, list) or not isinstance(markers, list):
            return None
        if len(data) != HTTP_INPUT_BATCH_SAMPLES or len(markers) != HTTP_INPUT_BATCH_SAMPLES:
            return None
        normalized_data: list[list[float]] = []
        normalized_markers: list[int] = []
        try:
            for row in data:
                if not isinstance(row, list) or len(row) != CHANNELS:
                    return None
                normalized_data.append([float(value) for value in row])
            normalized_markers = [int(value) for value in markers]
        except (TypeError, ValueError):
            return None
        return normalized_data, normalized_markers, sampling_rate, channels, device, timestamp

    def ingest(self, batch: EegBatch, record: dict) -> dict | None:
        if batch.config_id != CONFIG_NORMAL:
            self.reset()
            return None
        validated = self._validate_record(record)
        if validated is None or int(batch.samples.shape[0]) != HTTP_INPUT_BATCH_SAMPLES:
            self.reset()
            return None
        data, markers, sampling_rate, channels, device, timestamp = validated
        stream_id = int(batch.stream_id) & 0xFFFFFFFF
        start_seq = int(batch.start_seq) & 0xFFFFFFFF
        continuous = (
            self._stream_id is None
            or (
                self._stream_id == stream_id
                and self._next_seq == start_seq
                and self._sampling_rate == sampling_rate
                and self._channels == channels
                and self._device == device
            )
        )
        if not continuous:
            self.reset()

        if self._stream_id is None:
            self._stream_id = stream_id
            self._sampling_rate = sampling_rate
            self._channels = channels
            self._device = device
        if not self._data:
            self._timestamp = timestamp
        self._data.extend(data)
        self._markers.extend(markers)
        self._next_seq = (start_seq + HTTP_INPUT_BATCH_SAMPLES) & 0xFFFFFFFF

        if len(self._data) < self.target_samples:
            return None
        if len(self._data) != self.target_samples or self._timestamp is None:
            self.reset()
            return None

        payload = {
            "data": self._data,
            "timestamp": self._timestamp,
            "sampling_rate": sampling_rate,
            "channels": channels,
            "device": device,
            "batch": True,
            "markers": self._markers,
            "eeg_type": 0,
        }
        self._clear_payload()
        return payload


@dataclass(frozen=True)
class HttpSenderSnapshot:
    running: bool
    successful_packets: int
    failed_packets: int
    dropped_packets: int
    sent_samples: int
    queue_depth: int
    last_status_code: int | None
    last_latency_ms: float | None
    last_error: str | None
    last_success_time: float | None


HttpSendFunction = Callable[[str, bytes, float], tuple[int, str]]


def _send_http_json(url: str, body: bytes, timeout_s: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = int(response.status)
            response_text = response.read(200).decode("utf-8", errors="replace")
            return status, response_text
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(200).decode("utf-8", errors="replace")


class HttpJsonSender(threading.Thread):
    """Bounded real-time-priority HTTP sender; network I/O never runs on the GUI thread."""

    def __init__(
        self,
        settings: HttpPushSettings,
        *,
        timeout_s: float = HTTP_TIMEOUT_S,
        max_queue: int = HTTP_QUEUE_PACKETS,
        send_func: HttpSendFunction | None = None,
    ) -> None:
        super().__init__(name="p300-http-json-sender", daemon=True)
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_queue <= 0:
            raise ValueError("max_queue must be positive")
        self.settings = settings.normalized()
        self.timeout_s = float(timeout_s)
        self.queue: queue.Queue[dict | None] = queue.Queue(maxsize=int(max_queue))
        self._send_func = send_func or _send_http_json
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._successful_packets = 0
        self._failed_packets = 0
        self._dropped_packets = 0
        self._sent_samples = 0
        self._last_status_code: int | None = None
        self._last_latency_ms: float | None = None
        self._last_error: str | None = None
        self._last_success_time: float | None = None

    def submit(self, payload: dict) -> bool:
        if self._stop_event.is_set():
            return False
        try:
            self.queue.put_nowait(payload)
            return True
        except queue.Full:
            try:
                discarded = self.queue.get_nowait()
            except queue.Empty:
                discarded = None
            if discarded is not None:
                with self._lock:
                    self._dropped_packets += 1
            try:
                self.queue.put_nowait(payload)
                return True
            except queue.Full:
                with self._lock:
                    self._dropped_packets += 1
                return False

    def snapshot(self) -> HttpSenderSnapshot:
        with self._lock:
            return HttpSenderSnapshot(
                running=self._running,
                successful_packets=self._successful_packets,
                failed_packets=self._failed_packets,
                dropped_packets=self._dropped_packets,
                sent_samples=self._sent_samples,
                queue_depth=self.queue.qsize(),
                last_status_code=self._last_status_code,
                last_latency_ms=self._last_latency_ms,
                last_error=self._last_error,
                last_success_time=self._last_success_time,
            )

    def _collapse_backlog_to_latest(self) -> None:
        latest: dict | None = None
        removed = 0
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                if latest is not None:
                    removed += 1
                latest = item
        if latest is not None:
            self.queue.put_nowait(latest)
        if removed:
            with self._lock:
                self._dropped_packets += removed

    def _clear_queue(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stop_event.set()
        self._clear_queue()
        if self.ident is not None and self.is_alive():
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass
            self.join(timeout=max(0.0, float(timeout_s)))

    def run(self) -> None:
        with self._lock:
            self._running = True
        try:
            while not self._stop_event.is_set():
                try:
                    payload = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if payload is None:
                    return
                started = time.monotonic()
                status_code: int | None = None
                response_text = ""
                try:
                    body = json.dumps(
                        payload,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                    status_code, response_text = self._send_func(
                        self.settings.url,
                        body,
                        self.timeout_s,
                    )
                    if not 200 <= int(status_code) <= 299:
                        detail = response_text.strip()
                        raise RuntimeError(f"HTTP {status_code}" + (f": {detail}" if detail else ""))
                except Exception as error:  # Network and injected transport failures share the same policy.
                    latency_ms = (time.monotonic() - started) * 1000.0
                    with self._lock:
                        self._failed_packets += 1
                        self._last_status_code = status_code
                        self._last_latency_ms = latency_ms
                        self._last_error = str(error)[:200]
                    if not self._stop_event.is_set():
                        self._collapse_backlog_to_latest()
                    continue

                latency_ms = (time.monotonic() - started) * 1000.0
                sample_count = len(payload.get("data", []))
                with self._lock:
                    self._successful_packets += 1
                    self._sent_samples += sample_count
                    self._last_status_code = int(status_code)
                    self._last_latency_ms = latency_ms
                    self._last_error = None
                    self._last_success_time = time.time()
        finally:
            self._clear_queue()
            with self._lock:
                self._running = False

