"""Offline negotiation regressions: python -m unittest discover -s tests."""
import importlib.util
from pathlib import Path
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    "lcd_caps", Path(__file__).resolve().parents[1] / "octoprint_LCD_E3V3SE/firmware_caps.py")
caps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(caps)


class CapabilitiesTests(unittest.TestCase):
    def detector(self):
        detector = caps.FirmwareCapabilities(timeout=.005)
        detector.connected()
        return detector

    def test_exact_version_payload_and_dimensions_are_required(self):
        for line in ("M9001 CAPS 2 42 240 320", "M9001 CAPS 2 42 240 320 00000000 0000"):
            detector = self.detector()
            detector.received(line)
            self.assertEqual(detector.select(), "v2")
            self.assertIn("CAPS confirmed", detector.status())
        for line in ("M9001 CAPS 3 42 240 320", "M9001 CAPS 2 41 240 320",
                     "M9001 CAPS 2 42 320 240", "M9001 CAPS 2 42 240",
                     "M9001 CAPS 2 42 240 320 garbage", "M9001 CAPS 2 42 240 320 00000001 0000"):
            with self.subTest(line=line):
                detector = self.detector()
                detector.received(line)
                with self.assertRaises(RuntimeError):
                    detector.select()

    def test_absent_caps_only_selects_a_candidate(self):
        detector = self.detector()
        detector.received("ok")
        self.assertEqual(detector.select(), "v1")
        self.assertFalse(detector.legacy_confirmed)
        detector.confirm_legacy()
        self.assertIn("START confirmed", detector.status())

    def test_manual_overrides_do_not_bypass_validation(self):
        detector = self.detector()
        self.assertEqual(detector.select("v1"), "v1")
        self.assertFalse(detector.legacy_confirmed)
        with self.assertRaises(TimeoutError):
            detector.select("v2")
        detector.received("M9001 CAPS 2 42 240 320")
        self.assertEqual(detector.select("v2"), "v2")
        with self.assertRaises(RuntimeError):
            detector.select("v1")

    def test_delayed_response_wakes_negotiation(self):
        detector = self.detector()
        detector.timeout = 1
        started, results = threading.Event(), []
        def select():
            started.set()
            results.append(detector.select())
        worker = threading.Thread(target=select)
        worker.start()
        self.assertTrue(started.wait(1))
        detector.received("M9001 CAPS 2 42 240 320")
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, ["v2"])

    def test_disconnect_wakes_waiter_and_reconnect_discards_capabilities(self):
        detector = self.detector()
        detector.timeout = 1
        started, errors = threading.Event(), []
        def select():
            started.set()
            try:
                detector.select()
            except RuntimeError as error:
                errors.append(error)
        worker = threading.Thread(target=select)
        worker.start()
        self.assertTrue(started.wait(1))
        detector.reset()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        detector.received("M9001 CAPS 2 42 240 320")
        self.assertFalse(detector.v2)
        detector.connected()
        detector.timeout = .005
        self.assertEqual(detector.select(), "v1")
        self.assertFalse(detector.legacy_confirmed)

    def test_selection_cancellation_and_invalid_then_valid_response(self):
        detector = self.detector()
        with self.assertRaises(RuntimeError):
            detector.select(cancelled=lambda: True)
        detector.received("M9001 CAPS garbage")
        detector.received("M9001 CAPS 2 42 240 320")
        self.assertEqual(detector.select(), "v2")


if __name__ == "__main__":
    unittest.main()
