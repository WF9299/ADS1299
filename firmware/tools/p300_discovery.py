
"""Authenticated discovery of this Monitor; payload EEG remains unencrypted."""
from __future__ import annotations
import hashlib
import hmac
import json
import queue
import secrets
import socket
import threading
import time
import uuid
from tools.p300_marker_client import P300MarkerClient


def new_pairing() -> dict:
    return {"monitor_id": str(uuid.uuid4()), "pair_key": secrets.token_hex(32)}


def sign(key: str, message: str) -> str:
    raw = bytes.fromhex(key)
    if len(raw) != 32:
        raise ValueError("invalid pairing key")
    return hmac.new(raw, message.encode(), hashlib.sha256).hexdigest()


def make_discovery_reply(request: dict, pairing: dict, port: int) -> dict | None:
    if not isinstance(request, dict) or request.get("type") != "P300_DISCOVER":
        return None
    if type(request.get("version")) is not int or request["version"] != 1:
        return None
    mid, nonce = request.get("monitor_id"), request.get("nonce")
    signature = request.get("signature")
    if mid != pairing.get("monitor_id") or not isinstance(nonce, str) or len(nonce) != 32:
        return None
    if not isinstance(signature, str) or len(signature) != 64 or type(port) is not int or not 1 <= port <= 65535:
        return None
    try:
        if len(bytes.fromhex(nonce)) != 16:
            return None
        expected = sign(pairing["pair_key"], f"P300_DISCOVER|1|{mid}|{nonce}")
        if not hmac.compare_digest(signature, expected):
            return None
        signature = sign(pairing["pair_key"], f"P300_MONITOR|1|{mid}|{nonce}|{port}")
    except (ValueError, KeyError, TypeError):
        return None
    return {"type": "P300_MONITOR", "version": 1, "monitor_id": mid, "nonce": nonce,
            "port": port, "signature": signature}


class DiscoveryServer(threading.Thread):
    def __init__(self, pairing, tcp_port, events, *, port=1236):
        super().__init__(daemon=True, name="p300-discovery")
        self.pairing = pairing
        self.tcp_port = tcp_port
        self.events = events
        self.port = port
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.bound_port = None
        self.event_epoch = None
        self.sock = None
        self.clocks: dict[str, tuple[P300MarkerClient, float]] = {}

    def emit(self, event):
        if self.event_epoch is not None:
            event = ("receiver_epoch", self.event_epoch, event)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            pass

    def stop(self):
        self.stop_event.set()
        if self.sock:
            self.sock.close()

    def _expire_clocks(self):
        now = time.monotonic()
        for host, (client, seen) in list(self.clocks.items()):
            if now - seen > 30:
                client.stop()
                self.clocks.pop(host, None)

    def run(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                self.sock = sock
                sock.bind(("0.0.0.0", self.port))
                self.bound_port = int(sock.getsockname()[1])
                sock.settimeout(.5)
                self.ready.set()
                while not self.stop_event.is_set():
                    # Prune even while idle or full, so IP changes cannot exhaust slots.
                    self._expire_clocks()
                    try:
                        data, source = sock.recvfrom(1024)
                    except socket.timeout:
                        continue
                    try:
                        request = json.loads(data)
                        reply = make_discovery_reply(request, self.pairing, int(self.tcp_port()))
                    except (ValueError, TypeError):
                        continue
                    if reply is None:
                        continue
                    sock.sendto(json.dumps(reply, separators=(",", ":")).encode(), source)
                    host = source[0]
                    if host not in self.clocks:
                        if len(self.clocks) >= 8:
                            continue
                        client = P300MarkerClient(host, anchor_unix_time=True)
                        client.start()
                        self.clocks[host] = (client, time.monotonic())
                    else:
                        self.clocks[host] = (self.clocks[host][0], time.monotonic())
                    clock = self.clocks[host][0]
                    self.emit(("discovery", {"host": host, "synchronized": clock.synchronized,
                                             "error": clock.last_sync_error}))
        except OSError as error:
            if not self.stop_event.is_set():
                self.emit(("discovery_error", {"error": str(error)}))
        finally:
            self.ready.set()
            self.sock = None
            for client, _ in self.clocks.values():
                client.stop()
            self.clocks.clear()
