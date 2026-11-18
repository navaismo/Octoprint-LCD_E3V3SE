"""M9000 differences verified against C13/C14 legacy and Pure Host firmware."""


class FirmwareCommands:
    def __init__(self, protocol, send_m9000, send_gcode):
        self.protocol = protocol
        self.send_m9000 = send_m9000
        self.send_gcode = send_gcode

    def metadata(self, name, total, height, estimated, progress=0):
        self.send_m9000('N"' + name + '"')
        if self.protocol == "v1":
            # Legacy's Print Time field reads ui.print_time, set in minutes by
            # T. Retain main's initial estimate and replace it on completion.
            self.send_m9000("T{} L{} P{}".format(min(71582788, estimated), total, progress))
        else:
            self.send_m9000("L{} H{}".format(total, height))
            self.send_m9000("E{}".format(estimated))
        self.send_gcode("M73 R{}".format(min(71582788, estimated)))
        self.send_m9000("S1")

    def refreshed(self):
        if self.protocol == "v2":
            self.send_m9000("D1")

    def finish(self, elapsed, total):
        if self.protocol == "v1":
            self.send_m9000("T{} L{} P100".format(elapsed, total))
        self.send_m9000("F1")
