"""Offline regression checks: python -m unittest discover -s tests."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lcd_purge", ROOT / "octoprint_LCD_E3V3SE/purge.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PurgeTests(unittest.TestCase):
    def state(self, relative=False):
        state = MODULE.PurgeState()
        for cmd in ("G21", "G90", "M83" if relative else "M82", "G92 E125.25", "G1 F2400"):
            state.sent(cmd)
        return state

    def test_restores_absolute_and_relative_extrusion(self):
        for relative in (False, True):
            with self.subTest(relative=relative):
                state = self.state(relative)
                commands = state.begin()
                self.assertEqual(commands[2], "G1 E15 F150")
                self.assertEqual(commands[5], "M83" if relative else "M82")
                self.assertFalse(any(c.startswith(("M84", "M18", "G90", "G91")) for c in commands))
                for command in commands:
                    state.sent(command)
                self.assertEqual(state.e, 125.25)
                self.assertEqual(state.feed, 2400)
                self.assertEqual(state.relative, relative)
                self.assertEqual(state.units, "G21")
                with self.assertRaises(ValueError):
                    state.begin()
                state.received("echo:" + commands[-1].split(" ", 1)[1])
                self.assertIsNone(state.pending)
                self.assertEqual(state.begin()[2], "G1 E15 F150")

    def test_tracks_numbered_absolute_relative_and_reset_commands(self):
        state = self.state()
        state.sent("N12 G1X10E126.5F1800*5")
        self.assertEqual(state.e, 126.5)
        state.sent("G91")
        state.sent("G1 E-2")
        self.assertEqual(state.e, 124.5)
        state.sent("G90")
        self.assertFalse(state.relative)
        state.sent("G92.9 E0")
        self.assertEqual(state.e, 0)

    def test_position_report_does_not_discard_known_precision(self):
        state = self.state()
        state.sent("G92 E125.25678")
        state.received("X:0.00 Y:0.00 Z:0.00 E:125.26 Count X:0 Y:0 Z:0")
        self.assertEqual(state.begin()[4], "G92 E125.256780000")

    def test_unknown_state_is_not_assumed(self):
        state = MODULE.PurgeState()
        with self.assertRaises(ValueError):
            state.begin()
        state.sent("M83")
        state.sent("G1 E10 F1200")
        self.assertIsNone(state.e)
        state.received("echo:  G21 ; Units in mm (mm)")
        state.received("X:10.0 Y:20.0 Z:2.0 E:13.5 Count X:0 Y:0 Z:0")
        self.assertEqual(state.begin()[4], "G92 E13.500000000")

    def test_inches_or_untracked_extrusion_are_rejected(self):
        for command in ("G20", "M701", "M702", "T1"):
            with self.subTest(command=command):
                state = self.state()
                state.sent(command)
                with self.assertRaises(ValueError):
                    state.begin()

    def test_firmware_change_uses_emergency_prompt_and_no_g1(self):
        state = MODULE.PurgeState()
        state.received("//action:paused")
        with self.assertRaises(ValueError):
            state.begin()
        for line in ("//action:prompt_begin Paused", "//action:prompt_button PurgeMore",
                     "//action:prompt_button Continue", "//action:prompt_show"):
            state.received(line)
        self.assertEqual(state.begin(), ["M876 S0"])
        with self.assertRaises(ValueError):
            state.begin()
        state.received("//action:resumed")
        self.assertFalse(state.firmware_pause)

    def test_disconnect_or_firmware_restart_clears_old_snapshot(self):
        for restart in (False, True):
            state = self.state()
            state.begin()
            state.received("start") if restart else state.reset()
            self.assertIsNone(state.pending)
            with self.assertRaises(ValueError):
                state.begin()


if __name__ == "__main__":
    unittest.main()
