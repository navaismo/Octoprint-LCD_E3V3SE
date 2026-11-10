"""Exercise the real OctoPrint plugin class with a counted job hold."""
import threading
import io
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
try:
    import octoprint
except ImportError:
    module=None
else:
    import octoprint_LCD_E3V3SE as module


@unittest.skipIf(module is None,"Requires the OctoPrint Python environment")
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.plugin=module.LCD_E3V3SEPlugin()
        self.commands=[]
        self.done=threading.Event()
        self.draw_started=threading.Event()
        self.finish=threading.Event()
        self.hold_released=threading.Event()
        self.holds=1  # A separate plugin owns this hold; ours must not clear it.
        def hold(value,blocking=True):
            self.holds+=1 if value else -1
            if not value:
                self.hold_released.set()
            return True
        self.pause_calls=[]
        self.plugin._printer=SimpleNamespace(
            get_current_job=lambda:{"file":{"name":"test.gcode","path":None,"origin":"local"}},
            set_job_on_hold=hold,is_operational=lambda:True,is_printing=lambda:True,is_paused=lambda:True,
            commands=lambda c,**k:self.commands.append(c),
            resume_print=lambda:self.pause_calls.append("resume"),pause_print=lambda:self.pause_calls.append("pause"),
            cancel_print=lambda:self.pause_calls.append("cancel"))
        self.plugin._settings=SimpleNamespace(get=lambda k:True)
        self.plugin._plugin_manager=SimpleNamespace(send_plugin_message=lambda *a:self.done.set())
        self.plugin._identifier="LCD_E3V3SE"
        self.plugin.load_metadata_from_json=lambda name:{}
        self.plugin._draw.version=2
        self.plugin.file_name="test.gcode"

    def worker(self,fail=False):
        def metadata(*args):
            self.draw_started.set()
            self.assertEqual(self.holds,2)
            self.finish.wait(2)
            if fail:
                raise RuntimeError("Failed draw")
            self.commands.append("DONE")
        self.plugin.get_print_metadata=metadata
        self.plugin.gcode_script_hook(None,"gcode","beforePrintStarted")
        self.assertTrue(self.draw_started.wait(1))

    def test_start_hook_reads_actual_job_before_file_selected_event(self):
        self.plugin.file_name=None
        self.worker()
        self.assertEqual(self.plugin.file_name,"test.gcode")
        self.plugin.on_event("FileSelected",{"name":"test.gcode","path":None,"origin":"local"})
        self.assertEqual(self.holds,2)
        self.finish.set()
        self.assertTrue(self.done.wait(1))
        self.assertEqual(self.commands,["DONE"])

    def test_synchronous_script_gate_holds_before_first_job_command(self):
        self.worker()
        self.assertEqual(self.holds,2)
        self.assertEqual(self.pause_calls,[])
        self.finish.set()
        self.assertTrue(self.done.wait(1))
        self.assertEqual(self.holds,1)
        self.assertEqual(self.pause_calls,[])
        self.assertEqual(self.commands,["DONE"])

    def test_failure_releases_only_owned_hold_and_never_resumes(self):
        self.worker(fail=True)
        self.finish.set()
        self.assertTrue(self.done.wait(1))
        self.assertEqual(self.holds,1)
        self.assertEqual(self.pause_calls,[])

    def test_cancel_and_disconnect_leave_pause_and_prompt_state_alone(self):
        self.worker()
        self.plugin.on_event("Disconnected",{})
        self.finish.set()
        self.assertTrue(self.done.wait(1))
        self.assertEqual(self.holds,1)
        self.assertEqual(self.pause_calls,[])

    def test_duplicate_start_does_not_acquire_another_hold(self):
        self.worker()
        self.plugin.gcode_script_hook(None,"gcode","beforePrintStarted")
        self.assertEqual(self.holds,2)
        self.finish.set()
        self.assertTrue(self.done.wait(1))
        self.assertEqual(self.holds,1)

        self.assertEqual(self.commands,["DONE"])

    def test_missing_thumbnail_metadata_and_native_completion(self):
        self.plugin._draw.render=lambda ops:self.commands.append("DONE")
        self.plugin.get_print_metadata("test.gcode",True)
        self.assertEqual(self.commands[-2:],["DONE","M9000 D1"])
        self.assertTrue(self.plugin.sent_imagemap)
        self.assertIn("M9000 S1",self.commands)
        self.assertFalse(any(c.startswith(("M25","M876")) for c in self.commands))

    def test_invalid_thumbnail_keeps_placeholder_and_releases_controls(self):
        self.plugin.load_metadata_from_json=lambda name:{"thumb_data":"invalid","total_layers":"bad","layer_height":"bad"}
        self.plugin.decode_base64_image=lambda value:(_ for _ in ()).throw(ValueError("invalid image"))
        self.plugin._draw.render=lambda ops:self.commands.append("DONE")
        self.assertTrue(self.plugin.get_print_metadata("test.gcode",True))
        self.assertIn("M9000 L0 H0",self.commands)
        self.assertEqual(self.commands[-1],"M9000 D1")

    def test_rapid_selection_uses_one_worker_and_drops_stale_result(self):
        self.worker()
        self.plugin.on_event("FileSelected",{"name":"next.gcode","origin":"local"})
        self.assertEqual(self.holds,2)
        self.finish.set()
        self.assertTrue(self.done.wait(1))
        self.assertEqual(self.holds,1)
        self.assertEqual(self.pause_calls,[])
        self.assertTrue(self.plugin._render_attempted)

    def test_safety_prompt_cancels_render_and_blocks_native_resume(self):
        self.plugin.gcode_received_handler(None,"//action:prompt_begin Paused")
        self.assertTrue(self.plugin._draw.cancelled)
        self.plugin._purge.firmware_pause=True
        self.plugin.gcode_received_handler(None,"M9000 resume-job")
        self.assertEqual(self.pause_calls,[])

    def test_host_pause_resume_notifies_lcd_without_reopening_or_resuming_job(self):
        self.plugin.on_event("PrintPaused",{})
        self.plugin.on_event("PrintResumed",{})
        self.assertEqual(self.commands,["M9000 R1","M9000 R0"])
        self.assertEqual(self.pause_calls,[])
        self.assertEqual(self.holds,1)
        self.assertFalse(any(c.startswith("M9000 S") for c in self.commands))

    def test_resume_state_notification_does_not_answer_firmware_prompt(self):
        self.plugin._purge.firmware_pause=True
        self.plugin.on_event("PrintResumed",{})
        self.assertEqual(self.commands,["M9000 R0"])
        self.assertEqual(self.pause_calls,[])
        self.assertTrue(self.plugin._purge.firmware_pause)

    def test_native_pause_resume_cancel_keep_existing_host_actions(self):
        self.plugin.gcode_received_handler(None,"M9000 pause-job")
        self.plugin.gcode_received_handler(None,"M9000 resume-job")
        self.plugin.gcode_received_handler(None,"M9000 cancel-job")
        self.assertEqual(self.pause_calls,["pause","resume","cancel"])

    def test_stop_before_print_cancels_preview_without_cancelling_idle_printer(self):
        self.plugin._printer.is_printing=lambda:False
        self.plugin._printer.is_paused=lambda:False
        self.worker()
        self.plugin.gcode_received_handler(None,"M9000 cancel-job")
        self.assertTrue(self.plugin._draw.cancelled)
        self.finish.set()
        self.assertTrue(self.hold_released.wait(1))
        self.assertEqual(self.holds,1)
        self.assertEqual(self.pause_calls,[])

    def test_total_estimate_is_sent_once_before_screen_and_not_changed_by_timeleft(self):
        self.plugin.load_metadata_from_json=lambda name:{"print_time":185,"layer_height":.2}
        self.plugin._draw.render=lambda ops:None
        self.assertTrue(self.plugin.get_print_metadata("test.gcode",False))
        self.assertEqual(self.commands.count("M9000 E185"),1)
        self.assertLess(self.commands.index("M9000 E185"),self.commands.index("M9000 S1"))
        before=list(self.commands)
        for line in ("M73 P20 R120","M73 P50 R60","M73 P100 R0"):
            self.plugin.gcode_sending_handler(None,"queuing",line,None,"M73")
            self.plugin.gcode_received_handler(None,"ok")
        self.assertEqual(self.commands,before)

    def test_unknown_estimate_resets_previous_total(self):
        self.plugin._draw.render=lambda ops:None
        for estimate in (None,"invalid",float("nan"),float("inf"),-1):
            self.plugin.load_metadata_from_json=lambda name,estimate=estimate:{"print_time":estimate,"layer_height":.2}
            self.commands.clear()
            self.assertTrue(self.plugin.get_print_metadata("test.gcode",False))
            self.assertIn("M9000 E0",self.commands)
            self.assertLess(self.commands.index("M9000 E0"),self.commands.index("M9000 S1"))

    def test_layer_height_footer_wins_over_header_and_similar_keys(self):
        content = ("; Layer height: 0.24\r\nG28\r\n; EXECUTABLE_BLOCK_END\r\n"
                   "; first_layer_height = 0.28\r\n; independent_support_layer_height = 1\r\n"
                   " \t; layer_height = 0.2\r\n; max_layer_height = 0.32\r\n")
        self.assertEqual(self.plugin.find_layer_height_from_content(content), .2)
        self.assertEqual(self.plugin.find_layer_height_from_content("; LAYER_HEIGHT = .16 mm"), .16)

    def test_layer_height_rejects_invalid_settings(self):
        for value in ("0", "-0.2", "nan", "inf", "100", "0.2oops", "0.2,0.4"):
            with self.subTest(value=value):
                self.assertIsNone(self.plugin.find_layer_height_from_content("; layer_height = " + value))
        self.assertIsNone(self.plugin.find_layer_height_from_content("; first_layer_height = 0.28\n; max_layer_height = 0.32"))

    def test_cached_missing_height_recovers_footer_and_persists_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            gcode=root/"model.gcode"
            gcode.write_bytes(b"G1 X0\n" * 200000 + b"; EXECUTABLE_BLOCK_END\n; first_layer_height = 0.28\n; layer_height = 0.2\n")
            self.plugin.metadata_dir=folder
            cache=root/"model.gcode.json"
            cache.write_text(json.dumps({"total_layers":123,"thumb_data":None,"processed":True}))
            self.plugin.load_metadata_from_json=module.LCD_E3V3SEPlugin.load_metadata_from_json.__get__(self.plugin)
            paths=[]
            self.plugin._file_manager=SimpleNamespace(path_on_disk=lambda origin,path:paths.append((origin,path)) or str(gcode))
            self.plugin.file_path="nested/model.gcode"
            self.plugin._draw.render=lambda ops:self.commands.append("DONE")
            self.assertTrue(self.plugin.get_print_metadata("model.gcode",False))
            self.assertIn("M9000 L123 H200",self.commands)
            self.assertEqual(paths,[("local","nested/model.gcode")])
            saved=json.loads(cache.read_text())
            self.assertEqual(saved["layer_height"],.2)
            self.assertEqual(saved["total_layers"],123)
            self.assertTrue(saved["processed"])
            paths.clear()
            self.assertTrue(self.plugin.get_print_metadata("model.gcode",False))
            self.assertEqual(paths,[])  # Successful recovery is reused.

    def test_cached_valid_height_does_not_read_gcode(self):
        self.plugin.load_metadata_from_json=lambda name:{"layer_height":.24}
        def unexpected(*args):
            self.fail("Valid cached height must not trigger file I/O")
        self.plugin._file_manager=SimpleNamespace(path_on_disk=unexpected)
        self.plugin._draw.render=lambda ops:None
        self.assertTrue(self.plugin.get_print_metadata("test.gcode",False))
        self.assertIn("M9000 L0 H240",self.commands)

    def test_unreadable_height_uses_placeholder(self):
        self.plugin._file_manager=SimpleNamespace(path_on_disk=lambda *args:"/nonexistent/lcd-height.gcode")
        self.plugin._draw.render=lambda ops:None
        self.assertTrue(self.plugin.get_print_metadata("test.gcode",False))
        self.assertIn("M9000 L0 H0",self.commands)

    def test_height_file_without_footer_uses_header(self):
        with tempfile.TemporaryDirectory() as folder:
            gcode=Path(folder)/"header.gcode"
            gcode.write_bytes(b"; Layer height: 0.16\n" + b"G1 X0\n" * 40000)
            self.plugin._file_manager=SimpleNamespace(path_on_disk=lambda *args:str(gcode))
            self.assertEqual(self.plugin.find_layer_height_from_file("header.gcode"),.16)

    def test_cancelled_height_recovery_does_not_save_or_send(self):
        def recover(path):
            self.plugin._render_generation+=1
            return .2
        self.plugin.find_layer_height_from_file=recover
        self.plugin.save_metadata_to_json=lambda *args:self.fail("Cancelled recovery must not save")
        self.assertFalse(self.plugin.get_print_metadata("test.gcode",False))
        self.assertEqual(self.commands,[])

    def test_upload_preprocessor_captures_height_after_executable_end(self):
        with tempfile.TemporaryDirectory() as folder:
            self.plugin.metadata_dir=folder
            content=b";LAYER_COUNT:123\nG28\n; EXECUTABLE_BLOCK_END\n; first_layer_height = 0.28\n; layer_height = 0.2\n"
            upload=SimpleNamespace(filename="footer.gcode",stream=lambda:io.BytesIO(content))
            with patch.object(module.octoprint.filemanager,"valid_file_type",return_value=True):
                result=self.plugin.file_preprocessor("footer.gcode",upload,None,None,True)
            self.assertIs(result,upload)
            metadata=json.loads((Path(folder)/"footer.gcode.json").read_text())
            self.assertEqual(metadata["layer_height"],.2)
            self.assertEqual(metadata["total_layers"],"123")

    def test_height_and_layer_metadata_are_not_invented(self):
        self.assertIsNone(self.plugin.find_layer_height_from_content("G1 X0"))
        self.assertEqual(self.plugin.find_layer_height_from_content("; layer_height = 0.24"),.24)
        self.assertEqual(self.plugin.find_total_layers_from_content(";LAYER_COUNT:123"),"123")

    def test_thumbnail_fallback_repairs_cached_missing_or_invalid_preview(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (192, 96), "red").save(encoded, format="PNG")
        payload = base64.b64encode(encoded.getvalue()).decode("ascii")
        for old_preview in (None, "invalid"):
            with self.subTest(old_preview=old_preview), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                gcode = root / "model.gcode"
                gcode.write_text("; " + payload + "\nG28\n")
                self.plugin.metadata_dir = folder
                metadata = {"thumb_data": old_preview, "layer_height": .2,
                            "total_layers": 123, "print_time": 302}
                cache = root / "model.gcode.json"
                cache.write_text(json.dumps(metadata))
                self.plugin.load_metadata_from_json = module.LCD_E3V3SEPlugin.load_metadata_from_json.__get__(self.plugin)
                paths = []
                self.plugin._file_manager = SimpleNamespace(
                    path_on_disk=lambda origin, path: paths.append((origin, path)) or str(gcode))
                self.plugin.file_path = "nested/model.gcode"
                rendered = []
                self.plugin._draw.render = rendered.append
                self.assertTrue(self.plugin.get_print_metadata("model.gcode", True))
                saved = json.loads(cache.read_text())
                self.assertEqual(saved["thumb_data"], payload)
                self.assertTrue(saved["thumbnail_fallback_checked"])
                self.assertEqual(saved["thumbnail_fallback_version"], module.EXTRACTION_VERSION)
                self.assertEqual(saved["print_time"], 302)
                self.assertEqual(paths, [("local", "nested/model.gcode")])
                expected = module.card_operations(module.rgb565(self.plugin.decode_base64_image(payload)))
                self.assertEqual(rendered, [expected])
                paths.clear()
                self.assertTrue(self.plugin.get_print_metadata("model.gcode", True))
                self.assertEqual(paths, [])

    def test_thumbnail_recovery_upgrades_old_checked_cache_once(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(encoded, format="PNG")
        old = base64.b64encode(encoded.getvalue()).decode("ascii")
        encoded = io.BytesIO()
        Image.new("RGB", (300, 400), "white").save(encoded, format="PNG")
        large = base64.b64encode(encoded.getvalue()).decode("ascii")
        metadata = {"layer_height": .2, "thumb_data": old, "thumbnail_fallback_checked": True}
        self.plugin.load_metadata_from_json = lambda name: dict(metadata)
        reads, rendered = [], []
        self.plugin.find_thumbnail_from_file = lambda path: reads.append(path) or (large, True)
        self.plugin.save_metadata_to_json = lambda name, value: metadata.update(value)
        self.plugin._draw.render = rendered.append
        for _ in range(2):
            self.assertTrue(self.plugin.get_print_metadata("test.gcode", True))
        self.assertEqual(len(reads), 1)
        self.assertEqual(metadata["thumb_data"], large)
        self.assertEqual(metadata["thumbnail_fallback_version"], module.EXTRACTION_VERSION)
        expected = module.card_operations(module.rgb565(self.plugin.decode_base64_image(large), fit_model=True))
        self.assertEqual(rendered, [expected, expected])

    def test_fit_setting_controls_framing_of_existing_cached_thumbnail(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(encoded, format="PNG")
        payload = base64.b64encode(encoded.getvalue()).decode("ascii")
        self.plugin.load_metadata_from_json = lambda name: {"layer_height": .2, "thumb_data": payload}
        self.assertTrue(self.plugin.get_settings_defaults()["fit_thumbnail_to_area"])
        rendered = []
        self.plugin._draw.render = rendered.append
        for fit in (False, True):
            self.plugin._settings = SimpleNamespace(get=lambda key: fit if key == ["fit_thumbnail_to_area"] else False)
            self.assertTrue(self.plugin.get_print_metadata("test.gcode", True))
        expected = [module.card_operations(module.rgb565(self.plugin.decode_base64_image(payload), fit_model=fit))
                    for fit in (False, True)]
        self.assertEqual(rendered, expected)
        self.assertNotEqual(*rendered)

    def test_thumbnail_fallback_requires_both_settings(self):
        self.assertFalse(self.plugin.get_settings_defaults()["enable_thumbnail_fallback"])
        self.plugin.load_metadata_from_json = lambda name: {"layer_height": .2}
        self.plugin.find_thumbnail_from_file = lambda *args: self.fail("Disabled fallback must not read G-code")
        self.plugin._draw.render = lambda ops: None
        self.plugin._settings = SimpleNamespace(get=lambda key: False)
        self.assertTrue(self.plugin.get_print_metadata("test.gcode", True))
        self.plugin._settings = SimpleNamespace(get=lambda key: True)
        self.assertTrue(self.plugin.get_print_metadata("test.gcode", False))

    def test_missing_embedded_image_is_checked_once_but_read_errors_can_retry(self):
        for checked in (False, True):
            with self.subTest(checked=checked):
                metadata = {"layer_height": .2}
                self.plugin.load_metadata_from_json = lambda name: dict(metadata)
                reads, saves = [], []
                self.plugin.find_thumbnail_from_file = lambda path: reads.append(path) or (None, checked)
                self.plugin.save_metadata_to_json = lambda name, value: (saves.append(name), metadata.update(value))
                self.plugin._draw.render = lambda ops: None
                self.assertTrue(self.plugin.get_print_metadata("test.gcode", True))
                self.assertTrue(self.plugin.get_print_metadata("test.gcode", True))
                self.assertEqual(len(reads), 1 if checked else 2)
                self.assertEqual(saves, ["test.gcode"] if checked else [])

    def test_cancelled_thumbnail_recovery_does_not_save_or_draw(self):
        self.plugin.load_metadata_from_json = lambda name: {"layer_height": .2}
        def recover(path):
            self.plugin._render_generation += 1
            return None, True
        self.plugin.find_thumbnail_from_file = recover
        self.plugin.save_metadata_to_json = lambda *args: self.fail("Cancelled recovery must not save")
        self.plugin._draw.render = lambda ops: self.fail("Cancelled recovery must not draw")
        self.assertFalse(self.plugin.get_print_metadata("test.gcode", True))
        self.assertEqual(self.commands, [])

    def test_upload_thumbnail_fallback_setting_is_persisted_with_metadata(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (300, 400), "red").save(encoded, format="PNG")
        payload = base64.b64encode(encoded.getvalue()).decode("ascii")
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as folder:
                self.plugin.metadata_dir = folder
                self.plugin._settings = SimpleNamespace(get=lambda key: enabled)
                upload = SimpleNamespace(filename="preview.gcode", stream=lambda: io.BytesIO(
                    ("; " + payload + "\nG28\n").encode("ascii")))
                with patch.object(module.octoprint.filemanager, "valid_file_type", return_value=True):
                    self.assertIs(self.plugin.file_preprocessor("preview.gcode", upload, None, None, True), upload)
                saved = json.loads((Path(folder) / "preview.gcode.json").read_text())
                self.assertEqual(saved["thumb_data"], payload if enabled else None)
                self.assertEqual(saved["thumbnail_fallback_checked"], enabled)

if __name__ == "__main__":
    unittest.main()
