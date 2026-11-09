"""M9001 v2: bounded G-free Base64 batches over OctoPrint's ordinary command queue."""
import base64
import secrets
import struct
import threading
import time
import zlib

VERSION = 2
PAYLOAD = 42


def packet(kind, sid, body=b""):
    raw = struct.pack(">BI", VERSION, sid) + body
    raw += struct.pack(">I", zlib.crc32(kind.encode("ascii") + raw) & 0xFFFFFFFF)
    return "M9001 " + kind + base64.b64encode(raw).decode("ascii").replace("G", "_")


def color(value):
    return b"\0" + struct.pack(">H", value)


def rect(x0, y0, x1, y1):
    if not (0 <= x0 <= x1 < 240 and 25 <= y0 <= y1 <= 122):
        raise ValueError("Drawing leaves the print-card region")
    return bytes((1, x0, y0, x1, y1))


def batches(operations):
    chunk = bytearray()
    for op in operations:
        if not op or len(op) > PAYLOAD or (op[0] < 2 and len(op) not in (3, 5)) or (op[0] == 2 and (len(op) < 6 or len(op) != 4 + 2*op[3])):
            raise ValueError("Invalid drawing operation")
        if len(chunk) + len(op) > PAYLOAD:
            yield bytes(chunk)
            chunk.clear()
        chunk.extend(op)
    if chunk:
        yield bytes(chunk)


class DrawingClient:
    def __init__(self, send):
        self.send = send
        self.condition = threading.Condition()
        self.lock = threading.Lock()
        self.version = None
        self.sid = None
        self.response = None
        self.cancelled = False

    def connected(self):
        with self.condition:
            self.version = None
            self.cancelled = False
        self.send("M9001 Q")

    def received(self, line):
        fields = line.strip().split()
        if fields[:5] == ["M9001", "CAPS", "2", "42", "240"] and len(fields) >= 6 and fields[5] == "320":
            with self.condition:
                self.version = 2
                self.condition.notify_all()
            return
        if len(fields) != 4 or fields[0] != "M9001":
            return
        try:
            sid, seq = int(fields[2], 16), int(fields[3], 16)
        except ValueError:
            return
        with self.condition:
            if sid == self.sid:
                self.response = (fields[1], seq)
                self.condition.notify_all()

    def cancel(self, disconnected=False):
        with self.condition:
            self.cancelled = True
            sid = self.sid
            self.condition.notify_all()
        if sid and not disconnected:
            self.send(packet("A", sid))
        if disconnected:
            self.version = None

    def _exchange(self, command, expected, seq, timeout=1.5):
        for _ in range(3):
            with self.condition:
                if self.cancelled:
                    raise RuntimeError("Drawing cancelled")
                self.response = None
            self.send(command)
            until = time.monotonic() + timeout
            with self.condition:
                while not self.cancelled:
                    if self.response is not None:
                        status, got_seq = self.response
                        if status == expected and got_seq == seq:
                            return
                        if status == "BUSY" and command.startswith("M9001 B"):
                            raise RuntimeError("Native display is busy")
                        if status not in ("ACK", "BUSY"):
                            raise RuntimeError("M9001 " + status)
                        self.response = None
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)
                if self.cancelled:
                    raise RuntimeError("Drawing cancelled")
        raise TimeoutError("M9001 did not confirm " + expected)

    def render(self, operations):
        if not self.lock.acquire(False):
            raise RuntimeError("Another drawing session is active")
        try:
            with self.condition:
                if self.cancelled:
                    raise RuntimeError("Drawing cancelled")
                until = time.monotonic() + 2
                while self.version is None and not self.cancelled:
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)
                if self.version != VERSION or self.cancelled:
                    raise RuntimeError("M9001 v2 unavailable")
                self.sid = secrets.randbelow(0xFFFFFFFF) + 1
            self._exchange(packet("B", self.sid), "ACK", 0)
            seq, checksum = 0, 0
            for data in batches(operations):
                if seq >= 65535:
                    raise ValueError("Drawing session is too long")
                self._exchange(packet("D", self.sid, struct.pack(">H", seq) + data), "ACK", seq + 1)
                checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
                seq += 1
            self._exchange(packet("E", self.sid, struct.pack(">HI", seq, checksum)), "DONE", seq, 2.5)
        except Exception:
            if self.sid:
                self.send(packet("A", self.sid))
            raise
        finally:
            with self.condition:
                self.sid = None
            self.lock.release()
