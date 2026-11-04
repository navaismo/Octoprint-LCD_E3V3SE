"""Exercise the real plugin API when OctoPrint is installed, without a device."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    import octoprint
except ModuleNotFoundError:
    module = None
else:
    import octoprint_LCD_E3V3SE as module
    from flask import Flask


@unittest.skipIf(module is None, "Run in the OctoPrint Python environment")
class PurgeApiTests(unittest.TestCase):
    def setUp(self):
        self.plugin = module.LCD_E3V3SEPlugin()
        self.plugin._settings = SimpleNamespace(get_boolean=lambda key: True)
        self.commands = []
        self.plugin._printer = SimpleNamespace(
            is_paused=lambda: True,
            commands=lambda commands, **kwargs: self.commands.append((commands, kwargs)),
        )
        self.app = Flask(__name__)
        self.context = self.app.app_context()
        self.context.push()
        self.permission = patch.object(module, "Permissions", SimpleNamespace(
            CONTROL=SimpleNamespace(can=lambda: True)))
        self.permission.start()

    def tearDown(self):
        self.permission.stop()
        self.context.pop()

    def state(self):
        for command in ("G21", "M82", "G92 E75", "G1 F1800"):
            self.plugin.gcode_sent_handler(None, "sent", command, None, None)

    def request(self):
        result = self.plugin.on_api_command("purge_filament", {})
        if isinstance(result, tuple):
            return result[0].get_json(), result[1]
        return result.get_json(), result.status_code

    def test_actual_plugin_registers_api_and_sent_hook(self):
        module.__plugin_load__()
        self.assertIn("purge_filament", module.__plugin_implementation__.get_api_commands())
        self.assertIn("octoprint.comm.protocol.gcode.sent", module.__plugin_hooks__)

    def test_known_snapshot_queues_one_restoring_sequence(self):
        self.state()
        result, status = self.request()
        self.assertEqual((result, status), ({"success": True}, 200))
        self.assertEqual(len(self.commands), 1)
        commands, options = self.commands[0]
        self.assertIn("G92 E75.000000000", commands)
        self.assertIn("G1 F1800.000000000", commands)
        self.assertEqual(options["tags"], {"plugin:LCD_E3V3SE:purge"})
        self.assertEqual(self.request()[1], 409)

    def test_permission_is_checked_before_extrusion(self):
        with patch.object(module, "Permissions", SimpleNamespace(CONTROL=SimpleNamespace(can=lambda: False))):
            self.assertEqual(self.request()[1], 403)
        self.assertEqual(self.commands, [])

    def test_disabled_running_gate_or_unknown_state_sends_nothing(self):
        for case in ("disabled", "running", "thumbnail", "unknown"):
            with self.subTest(case=case):
                self.plugin._settings.get_boolean = lambda key: case != "disabled"
                self.plugin._printer.is_paused = lambda: case != "running"
                self.plugin.pause_gate_active = case == "thumbnail"
                self.assertEqual(self.request()[1], 409)
                self.assertEqual(self.commands, [])

    def test_m600_purge_uses_prompt_without_relative_commands(self):
        for line in ("//action:paused", "//action:prompt_begin Paused", "//action:prompt_button PurgeMore", "//action:prompt_show"):
            self.plugin.gcode_received_handler(None, line)
        self.assertEqual(self.request()[1], 200)
        self.assertEqual(self.commands[0][0], ["M876 S0"])
        self.assertEqual(self.request()[1], 409)

    def test_queue_failure_invalidates_the_snapshot(self):
        self.state()
        self.plugin._printer.commands = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("transport failed"))
        self.assertEqual(self.request()[1], 503)
        self.assertIsNone(self.plugin._purge.e)
        self.assertIsNone(self.plugin._purge.pending)


if __name__ == "__main__":
    unittest.main()
