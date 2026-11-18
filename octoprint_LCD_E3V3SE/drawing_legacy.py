"""Legacy START/C/END transport using one acknowledged RGB565 row at a time."""
import threading
import time


WIDTH = HEIGHT = 96
PIXELS_PER_CHUNK = 12


def row_commands(pixels, row):
    if len(pixels) != WIDTH * HEIGHT or not 0 <= row < HEIGHT:
        raise ValueError("Legacy thumbnail must contain exactly 96x96 pixels")
    for offset in range(0, WIDTH, PIXELS_PER_CHUNK):
        values = pixels[row * WIDTH + offset:row * WIDTH + offset + PIXELS_PER_CHUNK]
        if any(not isinstance(value, int) or not 0 <= value <= 0xFFFF for value in values):
            raise ValueError("Invalid RGB565 pixel")
        yield "M9001 C {} {} {}".format(row, offset, "".join("{:04X}".format(value) for value in values))


class LegacyDrawingClient:
    def __init__(self, send, on_start=lambda: None, timeout=3.0, session_timeout=120.0):
        self.send = send
        self.on_start = on_start
        self.timeout = timeout
        self.session_timeout = session_timeout
        self.condition = threading.Condition()
        self.lock = threading.Lock()
        self.cancelled = False
        self.phase = None
        self.row = None
        self.ack = False
        self.end_seen = False

    def received(self, line):
        line = line.strip()
        with self.condition:
            if self.cancelled:
                return
            if self.phase == "start" and line == "M9001 START":
                self.ack = True
            elif self.phase == "row" and line == "M9001 ACK LINE {}".format(self.row):
                self.ack = True
            elif self.phase == "end":
                if line == "M9001 END":
                    self.end_seen = True
                elif self.end_seen and line == "M9001 thumbnail-rendered":
                    self.ack = True
            self.condition.notify_all()

    def cancel(self, disconnected=False):
        # v1 has no ABORT or session ID. Stop producing commands; sending END
        # here would falsely announce completion of an incomplete thumbnail.
        with self.condition:
            self.cancelled = True
            self.phase = self.row = None
            self.condition.notify_all()

    def _exchange(self, phase, commands, deadline, row=None):
        with self.condition:
            if self.cancelled:
                raise RuntimeError("Legacy drawing cancelled")
            self.phase, self.row = phase, row
            self.ack = self.end_seen = False
            for command in commands:
                if self.cancelled or time.monotonic() >= deadline:
                    raise RuntimeError("Legacy drawing cancelled or session expired")
                self.send(command)
            until = min(deadline, time.monotonic() + self.timeout)
            while not self.ack and not self.cancelled:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Legacy M9001 did not acknowledge {}{}".format(
                        phase, " {}".format(row) if row is not None else ""))
                self.condition.wait(remaining)
            if self.cancelled:
                raise RuntimeError("Legacy drawing cancelled")

    def render(self, pixels):
        if len(pixels) != WIDTH * HEIGHT:
            raise ValueError("Legacy thumbnail must contain exactly 96x96 pixels")
        if not self.lock.acquire(False):
            raise RuntimeError("Another legacy drawing session is active")
        try:
            deadline = time.monotonic() + self.session_timeout
            self._exchange("start", ("M9001 START",), deadline)
            self.on_start()
            for row in range(HEIGHT):
                self._exchange("row", row_commands(pixels, row), deadline, row)
            self._exchange("end", ("M9001 END",), deadline)
        finally:
            with self.condition:
                self.phase = self.row = None
            self.lock.release()
