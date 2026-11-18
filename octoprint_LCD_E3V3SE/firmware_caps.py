"""Connection-scoped M9001 negotiation without a destructive legacy probe."""
import re
import threading
import time


def is_v2_caps(line):
    fields = line.strip().split()
    if fields[:6] != ["M9001", "CAPS", "2", "42", "240", "320"]:
        return False
    return len(fields) == 6 or (len(fields) == 8
        and re.fullmatch(r"0{8}", fields[6]) is not None
        and re.fullmatch(r"0{4}", fields[7]) is not None)


class FirmwareCapabilities:
    def __init__(self, timeout=2.0):
        self.condition = threading.Condition()
        self.timeout = timeout
        self.started = None
        self.disconnected = False
        self.v2 = False
        self.invalid = False
        self.legacy_confirmed = False

    def connected(self):
        with self.condition:
            self.started = time.monotonic()
            self.disconnected = self.v2 = self.invalid = self.legacy_confirmed = False

    def received(self, line):
        if not line.startswith("M9001 CAPS"):
            return
        with self.condition:
            if self.disconnected:
                return
            if is_v2_caps(line):
                self.v2 = True
                self.invalid = False
            elif not self.v2:
                self.invalid = True
            self.condition.notify_all()

    def reset(self):
        with self.condition:
            self.disconnected = True
            self.started = None
            self.v2 = self.invalid = self.legacy_confirmed = False
            self.condition.notify_all()

    def wake(self):
        with self.condition:
            self.condition.notify_all()

    def select(self, preference="auto", cancelled=lambda: False):
        if preference not in ("auto", "v1", "v2"):
            preference = "auto"  # Existing settings have no protocol key.
        with self.condition:
            if preference != "v1":
                deadline = (self.started or time.monotonic()) + self.timeout
                while not self.v2 and not self.disconnected and not cancelled():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(min(remaining, .1))
            if self.disconnected or cancelled():
                raise RuntimeError("M9001 negotiation cancelled or disconnected")
            if preference == "v1":
                if self.v2:
                    raise RuntimeError("Legacy override conflicts with confirmed M9001 v2 firmware")
                return "v1"
            if self.v2:
                return "v2"
            if self.invalid:
                raise RuntimeError("Invalid or unsupported M9001 CAPS response")
            if preference == "v2":
                raise TimeoutError("M9001 v2 override requires a valid CAPS response")
            return "v1"  # Candidate only. START must be acknowledged before pixels.

    def confirm_legacy(self):
        with self.condition:
            if self.disconnected or self.v2:
                raise RuntimeError("Legacy handshake no longer matches this connection")
            self.legacy_confirmed = True

    def status(self):
        with self.condition:
            if self.disconnected or self.started is None:
                return "Disconnected"
            if self.v2:
                return "M9001 Draw v2 (CAPS confirmed)"
            if self.legacy_confirmed:
                return "Legacy M9001 v1 (START confirmed)"
            if self.invalid:
                return "Unsupported CAPS response"
            return "Awaiting CAPS / legacy START confirmation"
