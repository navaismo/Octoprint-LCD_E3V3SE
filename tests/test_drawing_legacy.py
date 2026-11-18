"""Full legacy frame, credits, acknowledgment and cancellation regressions."""
import importlib.util
from pathlib import Path
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    "lcd_legacy", Path(__file__).resolve().parents[1] / "octoprint_LCD_E3V3SE/drawing_legacy.py")
legacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(legacy)


class LegacyTests(unittest.TestCase):
    def peer(self, fail=None):
        sent, frame, started = [], [None] * 9216, []
        def send(command):
            sent.append(command)
            if command == "M9001 START":
                if fail != "start":
                    client.received(command)
            elif command.startswith("M9001 C "):
                _, _, row, offset, encoded = command.split()
                row, offset = int(row), int(offset)
                values = [int(encoded[i:i+4], 16) for i in range(0, len(encoded), 4)]
                self.assertTrue(0 <= row < 96 and 0 <= offset < 96)
                if row:
                    self.assertIsNotNone(frame[(row-1)*96+95], "Next row sent before previous row credit")
                frame[row*96+offset:row*96+offset+len(values)] = values
                if offset + len(values) == 96:
                    client.received("ok")  # Never an application acknowledgment.
                    if fail == "row":
                        client.received("M9001 ACK LINE {}".format(row+1))
                    else:
                        client.received("M9001 ACK LINE {}".format(row))
            elif command == "M9001 END":
                client.received("ok")
                if fail != "end":
                    client.received("M9001 END")
                if fail != "rendered":
                    client.received("M9001 thumbnail-rendered")
        client = legacy.LegacyDrawingClient(send, lambda: started.append(True), timeout=.005)
        return client, sent, frame, started

    def test_complete_96_frame_pixel_order_offsets_and_safe_serial_lengths(self):
        client, sent, frame, started = self.peer()
        pixels = [(i * 7919) % 65536 for i in range(9216)]
        client.render(pixels)
        self.assertEqual(frame, pixels)
        self.assertEqual(started, [True])
        self.assertEqual(sent[0], "M9001 START")
        self.assertEqual(sent[-1], "M9001 END")
        self.assertEqual(len(sent), 2 + 96 * 8)
        for command in sent:
            self.assertLessEqual(len("N2147483647 " + command + "*255"), 95)
            self.assertNotIn("G", command)  # Hex payload cannot trigger MeatPack's G heuristic.
        self.assertEqual(sent[1], "M9001 C 0 0 " + "".join("{:04X}".format(x) for x in pixels[:12]))

    def test_missing_start_never_sends_pixel_data(self):
        client, sent, _, started = self.peer("start")
        with self.assertRaises(TimeoutError):
            client.render([0] * 9216)
        self.assertEqual(sent, ["M9001 START"])
        self.assertEqual(started, [])

    def test_missing_row_ack_never_queues_a_second_row_or_end(self):
        client, sent, _, _ = self.peer("row")
        with self.assertRaises(TimeoutError):
            client.render([0] * 9216)
        self.assertEqual(len(sent), 9)
        self.assertFalse(any(command.startswith("M9001 C 1 ") for command in sent))
        self.assertNotIn("M9001 END", sent)

    def test_completion_needs_end_then_rendered_not_plain_ok(self):
        for fail in ("end", "rendered"):
            with self.subTest(fail=fail):
                client, _, _, _ = self.peer(fail)
                with self.assertRaises(TimeoutError):
                    client.render([0] * 9216)

    def test_cancellation_wakes_worker_without_false_end_or_resume(self):
        sent, errors, started = [], [], threading.Event()
        client = legacy.LegacyDrawingClient(lambda command: (sent.append(command), started.set()), timeout=1)
        def render():
            try:
                client.render([0] * 9216)
            except RuntimeError as error:
                errors.append(error)
        worker = threading.Thread(target=render)
        worker.start()
        self.assertTrue(started.wait(1))
        client.cancel(disconnected=True)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(sent, ["M9001 START"])
        self.assertEqual(len(errors), 1)
        self.assertIsNone(client.phase)

    def test_session_timeout_and_single_owner(self):
        client, sent, _, _ = self.peer()
        client.session_timeout = 0
        with self.assertRaises(RuntimeError):
            client.render([0] * 9216)
        self.assertEqual(sent, [])
        client.lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                client.render([0] * 9216)
        finally:
            client.lock.release()

    def test_invalid_dimensions_and_pixels_are_rejected(self):
        client, sent, _, _ = self.peer()
        with self.assertRaises(ValueError):
            client.render([0])
        self.assertEqual(sent, [])
        for pixels, row in (([-1] * 9216, 0), ([65536] * 9216, 0), ([0] * 9216, 96)):
            with self.assertRaises(ValueError):
                list(legacy.row_commands(pixels, row))


if __name__ == "__main__":
    unittest.main()
