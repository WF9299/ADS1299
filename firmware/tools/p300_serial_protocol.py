"""Resynchronizable serial envelope; shared with serial_protocol.c."""
from __future__ import annotations
import struct
from dataclasses import dataclass
try:
    from tools.p300_common import crc32c
except ModuleNotFoundError:
    from p300_common import crc32c

HELLO, HELLO_REPLY, MANAGE, MANAGE_REPLY, CONFIG, EEG, CONTROL, CONTROL_REPLY, LOG, HEARTBEAT, HEARTBEAT_REPLY = range(1, 12)
MAX_PAYLOAD = 4096
HEADER = struct.Struct(">2sBBII")

@dataclass(frozen=True)
class SerialFrame:
    kind: int
    request_id: int
    payload: bytes

def encode_frame(kind: int, request_id: int, payload: bytes = b"") -> bytes:
    if not 1 <= kind <= 11 or len(payload) > MAX_PAYLOAD:
        raise ValueError("serial type/length outside protocol limits")
    data = HEADER.pack(b"\xa5\xc3", 1, kind, request_id & 0xffffffff, len(payload)) + payload
    return data + struct.pack(">I", crc32c(data))

class SerialParser:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[SerialFrame]:
        # Consume bounded chunks: a malicious input cannot grow the buffer without limit.
        output = []
        for offset in range(0, len(data), MAX_PAYLOAD):
            self.buffer.extend(data[offset:offset + MAX_PAYLOAD])
            while True:
                start = self.buffer.find(b"\xa5\xc3")
                if start < 0:
                    self.buffer[:] = self.buffer[-1:] if self.buffer[-1:] == b"\xa5" else b""
                    break
                if start:
                    del self.buffer[:start]
                if len(self.buffer) < HEADER.size:
                    break
                _, version, kind, request_id, size = HEADER.unpack_from(self.buffer)
                if version != 1 or not 1 <= kind <= 11 or size > MAX_PAYLOAD:
                    self.errors += 1
                    del self.buffer[0]
                    continue
                total = HEADER.size + size + 4
                if len(self.buffer) < total:
                    break
                expected = struct.unpack_from(">I", self.buffer, total - 4)[0]
                if crc32c(self.buffer[:total - 4]) != expected:
                    self.errors += 1
                    del self.buffer[0]
                    continue
                output.append(SerialFrame(kind, request_id, bytes(self.buffer[HEADER.size:total-4])))
                del self.buffer[:total]
        return output
