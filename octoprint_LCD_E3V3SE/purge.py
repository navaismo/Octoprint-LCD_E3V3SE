"""Preserve Marlin's extrusion state when purging a paused host print."""

import math
import re
import threading
import uuid


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


class PurgeState:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.units = self.relative = self.e = self.feed = None
            self.firmware_pause = self.purge_option = False
            self._prompt_purge = False
            self.pending = None

    def sent(self, command):
        with self.lock:
            command = re.sub(r"^N\d+\s*", "", command.strip()).split(";", 1)[0]
            match = re.match(r"([GMT])(\d+)(?:\.(\d+))?", command, re.I)
            if not match:
                return
            code = match[1].upper() + str(int(match[2]))
            values = {key.upper(): float(value) for key, value in
                      re.findall(r"([EF])\s*(" + NUMBER + r")", command[match.end():], re.I)}
            if code in ("G20", "G21"):
                if self.units is not None and self.units != code:
                    self.e = self.feed = None
                self.units = code
            elif code in ("G90", "G91", "M82", "M83"):
                # Marlin G90/G91 also clear the explicit E-mode override.
                self.relative = code in ("G91", "M83")
            elif code == "G92":
                if "E" in values:
                    self.e = values["E"]
            elif code in ("G0", "G1", "G2", "G3"):
                if "F" in values:
                    self.feed = values["F"]
                if "E" in values:
                    if self.relative is False:
                        self.e = values["E"]
                    elif self.relative is True and self.e is not None:
                        self.e += values["E"]
                    else:
                        self.e = None
            elif code == "M600":
                self.firmware_pause = True
                self.purge_option = False
            elif code in ("M701", "M702", "G28", "T0", "T1") or code.startswith("T"):
                self.e = None
            elif code in ("M112", "M999"):
                self.reset()

    def received(self, line):
        with self.lock:
            line = line.strip()
            if line == "start":
                self.reset()
            if line.startswith("//action:paused"):
                self.firmware_pause = True
            elif line.startswith("//action:resumed"):
                self.firmware_pause = self.purge_option = False
            elif line.startswith("//action:prompt_begin"):
                self._prompt_purge = self.purge_option = False
            elif line.startswith("//action:prompt_button") and line.split()[-1] == "PurgeMore":
                self._prompt_purge = True
            elif line.startswith("//action:prompt_show"):
                self.purge_option = self._prompt_purge
                if self.purge_option:
                    self.firmware_pause = True
            elif line.startswith("//action:prompt_end"):
                self._prompt_purge = self.purge_option = False

            units = re.match(r"^(?:echo:)?\s*(G2[01])\s*;", line)
            if units:
                self.sent(units[1])
            if re.match(r"^(?:echo:)?\s*X:", line):
                position = re.search(r"\bE:(" + NUMBER + r")", line)
                if position:
                    reported = float(position[1])
                    digits = len(position[1].split(".", 1)[1]) if "." in position[1] else 0
                    # M114 rounds E; retain a more precise tracked value if it agrees.
                    resolution = 0.5 * 10 ** -digits
                    if self.e is None or abs(self.e - reported) > resolution + 1e-9:
                        self.e = reported
            if self.pending and line in (self.pending, "echo:" + self.pending, "echo: " + self.pending):
                self.pending = None

    def begin(self):
        with self.lock:
            if self.pending:
                raise ValueError("A purge is already in progress.")
            if self.firmware_pause:
                if not self.purge_option:
                    raise ValueError("Use the filament-change prompt until Purge more is available.")
                self.purge_option = False
                return ["M876 S0"]
            if self.units != "G21":
                raise ValueError("Purge requires confirmed millimeter units. Wait for the pause reports.")
            if self.relative is None or self.e is None or self.feed is None:
                raise ValueError("Extrusion mode, E position or feedrate is unknown. Use M600 instead.")
            if not math.isfinite(self.e) or not math.isfinite(self.feed) or self.feed <= 0:
                raise ValueError("The paused extrusion state is invalid. Use M600 instead.")
            self.pending = "LCDPurge:" + uuid.uuid4().hex
            return [
                "M400", "M83", "G1 E15 F150", "M400",
                "G92 E{:.9f}".format(self.e),
                "M83" if self.relative else "M82",
                "G1 F{:.9f}".format(self.feed), "M118 " + self.pending,
            ]
