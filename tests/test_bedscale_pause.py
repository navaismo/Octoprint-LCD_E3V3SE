"""Run the production receive and popup handlers without an OctoPrint server."""
import ast
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(os.environ.get('BEDSCALE_PLUGIN_ROOT', Path(__file__).resolve().parents[1]))


class BedScalePauseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT / 'octoprint_LCD_E3V3SE/__init__.py'
        tree = ast.parse(source.read_text())
        methods = {node.name: node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)
                   and node.name in ('gcode_received_handler', 'on_event')}
        namespace = {'time': time}
        exec(compile(ast.Module(body=list(methods.values()), type_ignores=[]),
                     str(source), 'exec'), namespace)
        cls.receive = staticmethod(namespace['gcode_received_handler'])
        cls.event = staticmethod(namespace['on_event'])

    def plugin(self, printing=True, purge=True):
        plugin = SimpleNamespace(
            _printer=Mock(), _plugin_logger=Mock(), _plugin_manager=Mock(),
            _settings=Mock(), _identifier='LCD_E3V3SE',
            bedscale_warn=False, pause_gate_active=False,
        )
        plugin._printer.is_printing.return_value = printing
        plugin._settings.get.side_effect = lambda path: {
            'enable_purge_filament': purge,
        }.get(path[0])
        return plugin

    def test_bedscale_sets_warning_before_requesting_pause(self):
        plugin = self.plugin()
        plugin._printer.pause_print.side_effect = lambda: self.assertTrue(plugin.bedscale_warn)
        line = 'M9000 pause-job-bs'
        self.assertEqual(self.receive(plugin, None, line), line)
        plugin._printer.pause_print.assert_called_once_with()
        self.assertTrue(plugin.bedscale_warn)

    def test_bedscale_displays_weight_warning_after_pause(self):
        plugin = self.plugin()
        plugin._printer.pause_print.side_effect = lambda: self.event(
            plugin, 'PrinterStateChanged', {'state_id': 'PAUSED'})
        self.receive(plugin, None, 'M9000 pause-job-bs')
        plugin._printer.pause_print.assert_called_once_with()
        plugin._plugin_manager.send_plugin_message.assert_called_once_with(
            'LCD_E3V3SE', {'type': 'purge_popup',
                          'message': 'Detected a Change of the bed weight, please check the printer'})
        self.assertFalse(plugin.bedscale_warn)

    def test_regular_pause_keeps_regular_popup(self):
        plugin = self.plugin()
        plugin._printer.pause_print.side_effect = lambda: self.event(
            plugin, 'PrinterStateChanged', {'state_id': 'PAUSED'})
        self.receive(plugin, None, 'M9000 pause-job')
        plugin._printer.pause_print.assert_called_once_with()
        plugin._plugin_manager.send_plugin_message.assert_called_once_with(
            'LCD_E3V3SE', {'type': 'purge_popup',
                          'message': 'Printer is paused. Do you want to purge filament?'})
        self.assertFalse(plugin.bedscale_warn)

    def test_idle_does_not_pause_or_set_warning(self):
        for command in ('M9000 pause-job-bs', 'M9000 pause-job'):
            with self.subTest(command=command):
                plugin = self.plugin(printing=False)
                self.assertEqual(self.receive(plugin, None, command), command)
                plugin._printer.pause_print.assert_not_called()
                plugin._plugin_manager.send_plugin_message.assert_not_called()
                self.assertFalse(plugin.bedscale_warn)

    def test_disabled_purge_popup_is_respected(self):
        plugin = self.plugin(purge=False)
        plugin._printer.pause_print.side_effect = lambda: self.event(
            plugin, 'PrinterStateChanged', {'state_id': 'PAUSED'})
        self.receive(plugin, None, 'M9000 pause-job-bs')
        plugin._printer.pause_print.assert_called_once_with()
        plugin._plugin_manager.send_plugin_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
