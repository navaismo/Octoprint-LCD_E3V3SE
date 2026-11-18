"""Exercise both backends with real OctoPrint plugin APIs and fake firmware."""
import base64
import inspect
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import octoprint
except ImportError:
    module = None
else:
    import octoprint_LCD_E3V3SE as module
    from PIL import Image
    from flask import Flask


@unittest.skipIf(module is None, "Requires the OctoPrint Python environment")
class UnifiedPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = module.LCD_E3V3SEPlugin()
        self.settings = self.plugin.get_settings_defaults()
        self.plugin._settings = SimpleNamespace(get=lambda keys: self.settings.get(keys[0]))
        self.plugin._plugin_manager = SimpleNamespace(send_plugin_message=lambda *args: None)
        self.plugin._identifier = "LCD_E3V3SE"
        self.plugin.metadata_dir = self.temp.name
        self.commands, self.actions = [], []
        self.holds = 1  # Another plugin's hold must survive every outcome.
        self.released = threading.Event()
        self.mode = "v2"
        self.respond_start = True
        self.respond_rows = True
        self.respond_end = True
        self.started = threading.Event()
        self.end_queued = threading.Event()
        self.plugin._printer = SimpleNamespace(
            commands=self.send, set_job_on_hold=self.hold, is_operational=lambda: True,
            is_printing=lambda: True, is_paused=lambda: False,
            get_current_job=lambda: {"file": {"name": "test.gcode", "path": None, "origin": "local"}},
            pause_print=lambda: self.actions.append("pause"), resume_print=lambda: self.actions.append("resume"),
            cancel_print=lambda: self.actions.append("cancel"))
        self.plugin.file_name = "test.gcode"
        data = io.BytesIO()
        Image.new("RGB", (96, 96), "red").save(data, format="PNG")
        self.payload = base64.b64encode(data.getvalue()).decode("ascii")
        self.metadata = dict(layer_height=.2, total_layers=123, print_time=302, thumb_data=self.payload)
        self.plugin.load_metadata_from_json = lambda name: dict(self.metadata)

    def hold(self, value, blocking=True):
        self.holds += 1 if value else -1
        if not value:
            self.released.set()
        return True

    def received(self, line):
        self.plugin.gcode_received_handler(None, line)

    def send(self, command, **kwargs):
        self.commands.append(command)
        if command == "M9001 Q" and self.mode == "v2":
            self.received("M9001 CAPS 2 42 240 320 00000000 0000")
        elif command == "M9001 START":
            self.started.set()
            if self.respond_start:
                self.received("M9001 START")
        elif command.startswith("M9001 C "):
            _, _, row, offset, payload = command.split()
            if self.respond_rows and int(offset) + len(payload)//4 == 96:
                self.received("M9001 ACK LINE " + row)
        elif command == "M9001 END":
            self.end_queued.set()
            if self.respond_end:
                self.received("M9001 END")
                self.received("M9001 thumbnail-rendered")
        elif self.mode == "v2" and command.startswith(("M9001 B", "M9001 D", "M9001 E")):
            kind = command[6]
            raw = base64.b64decode(command[7:].replace("_", "G"))
            sid = int.from_bytes(raw[1:5], "big")
            seq = int.from_bytes(raw[5:7], "big") if kind in "DE" else 0
            self.received("M9001 {} {:08X} {:04X}".format(
                "DONE" if kind == "E" else "ACK", sid, seq + (kind == "D")))

    def connect(self, mode):
        self.mode = mode
        self.plugin.on_event("Connected", {})
        self.plugin._caps.timeout = .005
        self.plugin._legacy.timeout = .02

    def render_job(self):
        self.plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
        self.assertTrue(self.released.wait(2), "Render worker did not release its hold")
        self.assertEqual(self.holds, 1)
        self.assertEqual(self.actions, [])

    def test_v1_direct_print_with_safe_commands_and_native_metadata(self):
        self.connect("v1")
        self.render_job()
        self.assertTrue(self.plugin.sent_imagemap)
        self.assertTrue(self.plugin._caps.legacy_confirmed)
        self.assertEqual(self.commands.count("M9001 Q"), 1)
        self.assertEqual(self.commands.count("M9001 START"), 1)
        self.assertEqual(len([cmd for cmd in self.commands if cmd.startswith("M9001 C ")]), 768)
        self.assertIn("M9000 T302 L123 P0", self.commands)
        self.assertFalse(any(cmd.startswith(("M9000 E", "M9000 D", "M9000 L123 H", "M9001 B")) for cmd in self.commands))
        self.assertEqual(self.commands[-1], "M9001 END")
        self.plugin.on_event("PrintStarted", {})
        self.plugin.on_event("PrintDone", {})
        self.assertEqual(self.commands[-2:], ["M9000 T0 L123 P100", "M9000 F1"])

    def test_v2_direct_print_keeps_original_metadata_and_packet_path(self):
        self.connect("v2")
        self.render_job()
        self.assertTrue(self.plugin.sent_imagemap)
        self.assertIn("M9000 L123 H200", self.commands)
        self.assertIn("M9000 E302", self.commands)
        self.assertEqual(self.commands[-1], "M9000 D1")
        self.assertEqual(self.commands.count("M9001 Q"), 1)
        self.assertFalse(any(cmd.startswith(("M9001 START", "M9001 C ", "M9000 T")) for cmd in self.commands))

    def test_manual_v2_does_not_send_packets_without_caps(self):
        self.settings["lcd_protocol"] = "v2"
        self.connect("unknown")
        self.render_job()
        self.assertFalse(self.plugin.sent_imagemap)
        self.assertEqual(self.commands, ["M9000 A1", "M9001 Q"])

    def test_legacy_start_timeout_releases_only_owned_hold_without_pixels(self):
        self.connect("unknown")
        self.respond_start = False
        self.render_job()
        self.assertFalse(self.plugin.sent_imagemap)
        self.assertIn("M9001 START", self.commands)
        self.assertFalse(any(cmd.startswith("M9001 C ") for cmd in self.commands))
        self.assertNotIn("M9001 END", self.commands)

    def test_manual_v1_still_requires_start_and_conflicting_v2_is_rejected(self):
        self.settings["lcd_protocol"] = "v1"
        self.connect("v2")
        self.render_job()
        self.assertEqual(self.commands, ["M9000 A1", "M9001 Q"])

    def test_legacy_row_timeout_releases_hold_with_only_one_outstanding_row(self):
        self.connect("v1")
        self.respond_rows = False
        self.render_job()
        self.assertFalse(self.plugin.sent_imagemap)
        self.assertEqual(len([cmd for cmd in self.commands if cmd.startswith("M9001 C ")]), 8)

    def test_legacy_without_thumbnail_preserves_native_screen_and_needs_no_start(self):
        self.metadata["thumb_data"] = None
        self.connect("v1")
        self.render_job()
        self.assertIn("M9000 S1", self.commands)
        self.assertNotIn("M9001 START", self.commands)
        self.assertFalse(self.plugin._caps.legacy_confirmed)

    def test_cancellation_during_legacy_transfer_does_not_resume_or_send_end(self):
        self.connect("v1")
        self.respond_start = False
        self.plugin._legacy.timeout = 1
        self.plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
        self.assertTrue(self.started.wait(1))
        self.plugin.on_event("PrintCancelled", {})
        self.assertTrue(self.released.wait(1))
        self.assertEqual(self.holds, 1)
        self.assertEqual(self.actions, [])
        self.assertNotIn("M9001 END", self.commands)

    def test_duplicate_selection_after_completion_does_not_redraw(self):
        self.connect("v2")
        self.plugin._printer.is_printing = lambda: False
        self.render_job()
        before = list(self.commands)
        self.plugin.on_event("FileSelected", {"name": "test.gcode", "path": None, "origin": "local"})
        self.assertEqual(self.commands, before)
        self.assertEqual(self.holds, 1)

    def test_new_file_reuses_connection_caps_without_another_query(self):
        self.connect("v2")
        self.render_job()
        self.released.clear()
        self.plugin.on_event("FileSelected", {"name": "next.gcode", "path": "next.gcode", "origin": "local"})
        self.assertTrue(self.released.wait(1))
        self.assertEqual(self.commands.count("M9001 Q"), 1)
        self.assertIn('M9000 N"next.gcode"', self.commands)
        self.assertEqual(self.holds, 1)

    def test_reconnect_prevents_old_commands_and_old_hold_release(self):
        self.connect("v2")
        old_send = self.plugin._draw.send
        self.plugin._hold_owned = True
        self.plugin._hold_connection = self.plugin._connection_generation
        self.holds = 2
        self.plugin.on_event("Disconnected", {})
        self.assertEqual(self.holds, 1)
        self.connect("v1")
        with self.assertRaises(RuntimeError):
            old_send("M9001 END")
        before = list(self.commands)
        self.plugin._release_render_hold()
        self.assertEqual(self.holds, 1)
        self.assertEqual(self.commands, before)
        self.assertFalse(self.plugin._caps.v2)

    def test_reconnect_with_old_worker_pending_preserves_new_job_hold(self):
        self.connect("v2")
        original, old_send = self.plugin.get_print_metadata, self.plugin._draw.send
        entered, finish_old, checked_new = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(finish_old.set)
        calls = []
        def metadata(*args):
            calls.append(args)
            if len(calls) == 1:
                entered.set()
                finish_old.wait(2)
                with self.assertRaises(RuntimeError):
                    old_send("M9001 END")
                return False
            self.assertEqual(self.holds, 2, "Old worker released the replacement job's hold")
            checked_new.set()
            return original(*args)
        self.plugin.get_print_metadata = metadata
        self.plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
        self.assertTrue(entered.wait(1))
        self.plugin.on_event("Disconnected", {})
        self.assertEqual(self.holds, 1)
        self.connect("v2")
        self.released.clear()
        self.plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
        self.assertEqual(self.holds, 2)
        finish_old.set()
        self.assertTrue(self.released.wait(1))
        self.assertTrue(checked_new.is_set())
        self.assertEqual(self.holds, 1)
        self.assertEqual(self.actions, [])

    def test_hold_survives_until_legacy_completion_and_ignores_plain_ok(self):
        self.connect("v1")
        self.respond_end = False
        self.plugin._legacy.timeout = 1
        self.plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
        self.assertTrue(self.end_queued.wait(1))
        self.assertEqual(self.holds, 2)
        self.assertFalse(self.released.is_set())
        self.received("ok")
        self.received("M9001 END")
        self.assertFalse(self.released.is_set())
        self.received("M9001 thumbnail-rendered")
        self.assertTrue(self.released.wait(1))
        self.assertEqual(self.holds, 1)

    def test_metadata_logs_do_not_include_encoded_thumbnail(self):
        self.plugin._plugin_logger = Mock()
        content = ("; thumbnail begin 96x96 {}\n; {}\n; thumbnail end\nM73 P0 R10\n".format(
            len(self.payload), self.payload)).encode("ascii")
        upload = SimpleNamespace(filename="image.gcode", stream=lambda: io.BytesIO(content))
        with patch.object(module.octoprint.filemanager, "valid_file_type", return_value=True):
            self.plugin.file_preprocessor("image.gcode", upload)
        self.assertNotIn(self.payload, str(self.plugin._plugin_logger.mock_calls))
        saved = json.loads((Path(self.temp.name) / "image.gcode.json").read_text())
        self.assertEqual(saved["thumb_data"], self.payload)

    def test_missing_metadata_is_recovered_once_without_error_popups_or_base64_logs(self):
        gcode = Path(self.temp.name) / "test.gcode"
        gcode.write_text(";LAYER_COUNT:123\nM73 P0 R302\n; layer_height = 0.2\n")
        self.plugin._file_manager = SimpleNamespace(path_on_disk=lambda *args: str(gcode))
        self.plugin.load_metadata_from_json = module.LCD_E3V3SEPlugin.load_metadata_from_json.__get__(self.plugin)
        self.plugin._plugin_manager.send_plugin_message = Mock()
        self.connect("v2")
        self.render_job()
        saved = json.loads((Path(self.temp.name) / "test.gcode.json").read_text())
        self.assertEqual(saved["total_layers"], "123")
        self.assertEqual(saved["print_time"], 302)
        messages = [call.args[1] for call in self.plugin._plugin_manager.send_plugin_message.call_args_list]
        self.assertFalse(any(message["type"] == "error_popup" for message in messages))

    def test_optional_preprocessor_arguments_and_protected_api(self):
        signature = inspect.signature(self.plugin.file_preprocessor)
        self.assertIsNone(signature.parameters["links"].default)
        self.assertIsNone(signature.parameters["printer_profile"].default)
        self.assertFalse(signature.parameters["allow_overwrite"].default)
        self.assertTrue(self.plugin.is_api_protected())
        self.assertTrue(self.plugin.is_template_autoescaped())
        self.assertEqual(self.plugin.get_settings_defaults()["lcd_protocol"], "auto")
        upload = SimpleNamespace(filename="upload.gcode", stream=lambda: io.BytesIO(b"M73 P0 R10\n"))
        with patch.object(module.octoprint.filemanager, "valid_file_type", return_value=True):
            self.assertIs(self.plugin.file_preprocessor("upload.gcode", upload), upload)
            self.assertIs(self.plugin.file_preprocessor("upload.gcode", upload, None, None, True, None, extra=True), upload)
        app = Flask(__name__)
        with app.app_context():
            self.assertEqual(self.plugin.on_api_get(None).get_json()["preference"], "auto")
        from octoprint.printer.standard import Printer
        self.assertIn("blocking", inspect.signature(Printer.set_job_on_hold).parameters)


if __name__ == "__main__":
    unittest.main()
