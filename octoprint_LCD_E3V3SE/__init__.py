# Coding=utf 8
from __future__ import absolute_import

import math
import io
import os
import re
import json
import time
import base64
import inspect
import logging
import threading

from PIL import Image
from octoprint.logging.handlers import CleaningTimedRotatingFileHandler

import octoprint.plugin
import octoprint.filemanager
import octoprint.filemanager.util


class LCD_E3V3SEPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.filemanager.util.LineProcessorStream,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.ProgressPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin
):
    def __init__(self):
        # --- Core state ---
        self.plugin_data_folder = None
        self.metadata_dir = None

        self.file_name = None
        self.file_path = None

        self.print_time = None
        self.progress = None
        self.myETA = None

        self.is_lcd_ready = False
        self.current_layer = 0
        self.total_layers = 0

        self.print_finish = False
        self.sent_imagemap = False

        # --- Thumbnail TX helpers (ACK/CHUNK logic from your firmware) ---
        self.printer_busy = False
        self.get_last_chunk = False
        self.chunk_index = 0
        self.txLine = None
        self.nextLineAck = False

        # --- Metadata thread guard (avoid double-start per file) ---
        self._metadata_lock = threading.Lock()
        self._metadata_running = False
        self._metadata_last_file = None

        # --- NEW: Robust pause gate (fixes PAUSING->PAUSED race) ---
        # We pause at PrintStarted if we still need to send the thumbnail,
        # and we resume only after firmware says "thumbnail-rendered".
        self.thumb_rendered_event = threading.Event()
        self.pause_gate_active = False
        self.pause_gate_thread = None
        
        # Add in __init__ (new state tracking)
        self._last_state_id = "UNKNOWN"
        self._last_state_ts = time.time()

        # --- Timing ---
        self.start_time = None
        self.elapsed_time = None

        # --- Logger ---
        self._plugin_logger = logging.getLogger("octoprint.plugins.LCD_E3V3SE")

    # Required by new OctoPrint versions
    def is_template_autoescaped(self):
        return True

    # -----------------------
    # Logging / Settings
    # -----------------------
    def configure_logger(self):
        # Get the base path for logs from the settings
        log_base_path = os.path.expanduser("~/.octoprint/logs")

        # Create the directory if it doesn't exist
        if not os.path.exists(log_base_path):
            os.makedirs(log_base_path, exist_ok=True)
            os.chmod(log_base_path, 0o775)

        log_file_path = os.path.join(log_base_path, "LCD_E3V3SE.log")
        handler = CleaningTimedRotatingFileHandler(log_file_path, when="D", backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))

        # Avoid adding handlers multiple times on reload
        if not any(isinstance(h, CleaningTimedRotatingFileHandler) for h in self._plugin_logger.handlers):
            self._plugin_logger.addHandler(handler)

        self._plugin_logger.setLevel(logging.INFO)
        self._plugin_logger.propagate = False

    def get_current_function_name(self):
        return inspect.getframeinfo(inspect.currentframe().f_back).function

    def get_settings_defaults(self):
        # NOTE: OctoPrint will store settings once user saves them.
        # If your UI does not save, you will always see old stored values.
        return dict(
            enable_gcode_preview=True,       # Send and render G-code thumbnail
            progress_type="m73_progress",    # Progress based on M73
            enable_purge_filament=False      # Show purge popup on pause
        )

    def get_template_configs(self):
        # If you use the default settingsViewModel bindings:
        # custom_bindings must be False.
        return [
            dict(
                type="settings",
                template="settings.LCD_E3V3SE.jinja2",
                name="LCD E3V3SE",
                custom_bindings=False
            )
        ]

    def get_assets(self):
        return {"js": ["js/LCD_E3V3SE.js"]}

    def on_after_startup(self):
        self.configure_logger()
        self._plugin_logger.info(">>>>>> LCD_E3V3SE Plugin Loaded <<<<<<")

        # Create metadata directory inside plugin data folder
        data_folder = self.get_plugin_data_folder()
        self.metadata_dir = os.path.join(data_folder, "metadata")
        os.makedirs(self.metadata_dir, exist_ok=True)
        os.chmod(self.metadata_dir, 0o775)

        self._plugin_logger.info(f">>>>>> LCD_E3V3SE Plugin Metadata directory initialized: {self.metadata_dir}")
        self.slicer_values()

    def slicer_values(self):
        self._plugin_logger.info(f"Plugin Version: {self._plugin_version}")
        self._plugin_logger.info("Sliders values:")
        self._plugin_logger.info(f"Progress based on: {self._settings.get(['progress_type'])}")
        self._plugin_logger.info(f"Send Gcode Preview: {self._settings.get(['enable_gcode_preview'])}")
        self._plugin_logger.info(f"Enable Purge Filament: {self._settings.get(['enable_purge_filament'])}")

    # -----------------------
    # Metadata JSON Helpers
    # -----------------------
    def save_metadata_to_json(self, filename, metadata):
        metadata_path = os.path.join(self.metadata_dir, f"{filename}.json")
        try:
            with open(metadata_path, "w") as f:
                json.dump(metadata, f)
            self._plugin_logger.info(f"Metadata saved to {metadata_path}")
        except Exception as e:
            self._plugin_logger.error(
                f"{self.get_current_function_name()}: Failed to save metadata to {metadata_path}: {e}"
            )
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            my_err = f"Error Ocurred! \n \n {e}.\n Try uploading the file again"
            self._plugin_manager.send_plugin_message(self._identifier, {"type": "error_popup", "message": my_err})
            return None

    def load_metadata_from_json(self, filename):
        metadata_path = os.path.join(self.metadata_dir, f"{filename}.json")
        try:
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
            self._plugin_logger.info(f"Metadata loaded from {metadata_path}")
            return metadata
        except Exception as e:
            self._plugin_logger.error(
                f"{self.get_current_function_name()}: Failed to load metadata from {metadata_path}: {e}"
            )
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            my_err = f"Error Ocurred! \n \n {e}.\n Try uploading the file again"
            self._plugin_manager.send_plugin_message(self._identifier, {"type": "error_popup", "message": my_err})
            return None

    # -----------------------
    # Upload preprocessor
    # -----------------------
    def file_preprocessor(self, path, file_object, links, printer_profile, allow_overwrite, *args, **kwargs):
        """Intercept file uploads and generate a metadata JSON for fast printing."""
        self._plugin_logger.info(f">>>>>> PreProcessing file: {file_object}")
        self._plugin_logger.info(f">>>>>> PreProcessing path: {path}")

        if not octoprint.filemanager.valid_file_type(path, type="gcode"):
            return file_object

        file_name = file_object.filename

        # Read the file content from the upload stream
        file_stream = file_object.stream()
        file_content = file_stream.read().decode("utf-8", errors="ignore")

        total_layers = self.find_total_layers_from_content(file_content)
        b64_thumb = self.extract_thumbnail_from_content(file_content)
        progress, self.myETA = self.find_first_m73_from_content(file_content)
        print_time = self.myETA

        metadata = {
            "file_name": file_name,
            "file_path": path,
            "total_layers": total_layers,
            "print_time": print_time,
            "current_layer": 0,
            "progress": progress,
            "thumb_data": b64_thumb,
            "processed": True
        }

        self._plugin_logger.info(f">>>>>> PreProcessing metadata: {metadata}")

        try:
            self.save_metadata_to_json(file_name, metadata)
            self._plugin_logger.info(f"Metadata written for {path}")
        except Exception as e:
            self._plugin_logger.error(f"{self.get_current_function_name()}: Error writing metadata for {path}: {e}")
            my_err = f"Error Ocurred! \n \n {e}.\n Try uploading the file again"
            self._plugin_manager.send_plugin_message(self._identifier, {"type": "error_popup", "message": my_err})

        self._plugin_logger.info(f">>>>>> PreProcessing parsing complete for {file_name}")
        return file_object

    # -----------------------
    # Thread guards
    # -----------------------
    def _start_metadata_thread_once(self, file_name, thumb_enabled):
        """Start metadata processing in a background thread (idempotent per file)."""
        if not file_name:
            return

        with self._metadata_lock:
            if self._metadata_running and self._metadata_last_file == file_name:
                return
            self._metadata_running = True
            self._metadata_last_file = file_name

        def _runner():
            try:
                self.get_print_metadata(file_name, thumb_enabled)
            finally:
                with self._metadata_lock:
                    self._metadata_running = False

        threading.Thread(target=_runner, daemon=True).start()

    # -----------------------
    # Robust pause gate (fix)
    # -----------------------
    def _start_pause_gate(self, timeout_s=180):
        """
        Pause the print and keep retrying resume until thumbnail-rendered arrives.
        FIX: Do not trust is_printing() during transitions. Wait for stable PRINTING state.
        """
        if self.pause_gate_thread and self.pause_gate_thread.is_alive():
            return

        self.thumb_rendered_event.clear()
        self.pause_gate_active = True

        def _state():
            # Prefer event-derived state (more accurate than is_printing() during races)
            return getattr(self, "_last_state_id", "UNKNOWN")

        def _wait_stable_printing(stable_s=2.0, window_s=10.0):
            """
            Return True if we observe PRINTING continuously for stable_s seconds.
            """
            end_window = time.time() + window_s
            stable_start = None

            while time.time() < end_window and self.pause_gate_active:
                s = _state()

                if s == "PRINTING":
                    if stable_start is None:
                        stable_start = time.time()
                    elif (time.time() - stable_start) >= stable_s:
                        return True
                else:
                    stable_start = None

                time.sleep(0.2)

            return False

        def worker():
            try:
                self._plugin_logger.info("GATE: Pausing print until firmware sends 'thumbnail-rendered'...")

                # Request pause (Printing -> Pausing -> Paused)
                if self._printer.is_printing():
                    self._printer.pause_print()

                ok = self.thumb_rendered_event.wait(timeout=timeout_s)
                if not ok:
                    self._plugin_logger.warning("GATE: Timeout waiting 'thumbnail-rendered'. Trying to resume anyway.")
                else:
                    self._plugin_logger.info("GATE: 'thumbnail-rendered' received. Resuming print...")

                # Give OctoPrint a tiny moment to finish the PAUSE state transition
                time.sleep(0.3)

                # Robust resume loop
                end_time = time.time() + 30.0  # bigger window for slow state transitions
                resume_attempts = 0

                while time.time() < end_time and self.pause_gate_active:
                    s = _state()

                    # If fully paused, resume
                    if s == "PAUSED":
                        try:
                            self._printer.resume_print()
                            resume_attempts += 1
                            self._plugin_logger.info(f"GATE: resume_print() attempt #{resume_attempts}")
                        except Exception as e:
                            self._plugin_logger.error(f"GATE: resume_print failed: {e}")

                        # After resume request, wait for stable PRINTING
                        if _wait_stable_printing(stable_s=2.0, window_s=8.0):
                            self._plugin_logger.info("GATE: Printer is PRINTING and stable. Gate complete.")
                            return

                        # If not stable yet, keep looping (OctoPrint might bounce)
                        time.sleep(0.5)
                        continue

                    # If pausing, just wait (do not declare success)
                    if s == "PAUSING":
                        time.sleep(0.25)
                        continue

                    # If printing, require stability before declaring success
                    if s == "PRINTING":
                        if _wait_stable_printing(stable_s=2.0, window_s=6.0):
                            self._plugin_logger.info("GATE: Printer is PRINTING and stable. Gate complete.")
                            return
                        time.sleep(0.25)
                        continue

                    # For other states (STARTING/OPERATIONAL/etc), just wait and retry
                    time.sleep(0.25)

                self._plugin_logger.warning("GATE: Resume loop ended. Printer may still be paused.")

            except Exception as e:
                self._plugin_logger.error(f"GATE: Exception in pause gate worker: {e}")
            finally:
                self.pause_gate_active = False

        self.pause_gate_thread = threading.Thread(target=worker, daemon=True)
        self.pause_gate_thread.start()


    def _stop_pause_gate(self):
        """Disable gate loops and unblock waiters (used on cancel/done)."""
        self.pause_gate_active = False
        self.thumb_rendered_event.set()

    # -----------------------
    # Events
    # -----------------------
    def on_event(self, event, payload):
        # NOTE: Logging full payload is useful for debug but can be noisy.
        if event != "ZChange":
            self._plugin_logger.info(f">>>>>> Event received: {event}")

        if event == "PrinterStateChanged":
            state = payload.get("state_id", "UNKNOWN")
            self._last_state_id = state
            self._last_state_ts = time.time()
            
            self._plugin_logger.info(f">>>>>> ++++ Intercepted state: {state}")

            if state == "STARTING":
                self._plugin_logger.info(">>> Print Job is starting.")

            if state == "PAUSED":
                if ( self._settings.get(["enable_purge_filament"]) and not self.pause_gate_active):
                    self._plugin_logger.info(">>> Printer is paused. Opening Purge popup.")
                    self._plugin_manager.send_plugin_message(
                        self._identifier,
                        {"type": "purge_popup", "message": "Printer is paused. Do you want to purge filament?"}
                    )

        if event == "Connected":
            self.send_M9000_cmd("A1")

        if event == "FileSelected":
            self.file_name = payload.get("name")
            self.file_path = payload.get("path")

            # If the file comes from SD card, we don't handle metadata (as you wanted)
            if payload.get("origin") == "sdcard":
                self._plugin_logger.info("File selected from SD Card, not processing metadata.")
                return

            # Reset flags for a fresh render
            self.sent_imagemap = False
            self.is_lcd_ready = False

            try:
                if self._settings.get(["enable_gcode_preview"]):
                    self._plugin_logger.info("FileSelected: Will render G-code thumbnail.")
                    self._plugin_manager.send_plugin_message(
                        self._identifier,
                        {"type": "popup", "message": "Rendering Data in the LCD. Please Wait..."}
                    )
                    self._start_metadata_thread_once(self.file_name, True)
                else:
                    self._plugin_logger.info("FileSelected: Thumbnail disabled, using default thumbnail.")
                    self.send_M9000_cmd("S0")
                    self._start_metadata_thread_once(self.file_name, False)

            except Exception as e:
                self._plugin_logger.error(f"{self.get_current_function_name()}: {e}")
                my_err = f"Error Ocurred! \n \n {e}.\n Try uploading the file again"
                self._plugin_manager.send_plugin_message(self._identifier, {"type": "error_popup", "message": my_err})

        if event == "PrintStarted":
            self.slicer_values()
            self._plugin_logger.info(">>>+++ PrintStarted <<<")
            self.start_time = time.time()

            # If direct print races FileSelected, ensure metadata thread is started
            # and gate the print until we confirm thumbnail rendered.
            if self._settings.get(["enable_gcode_preview"]) and self.file_name and not self.sent_imagemap:
                self._start_pause_gate(timeout_s=180)
                self._start_metadata_thread_once(self.file_name, True)

        if event == "PrintCancelled":
            # Stop gates first to avoid leaving printer paused
            self._stop_pause_gate()
            self.cleanup()

        if event == "PrintDone":
            # Stop gates first
            self._stop_pause_gate()

            e_time = self.get_elapsed_time()
            self.send_M9000_cmd(f"T{e_time} L{self.total_layers} P100")
            self.send_M9000_cmd("F1")

            self._plugin_logger.info(">>>+++ PrintDone <<<")
            self.cleanup()
            self.print_finish = True

    # -----------------------
    # Metadata sender
    # -----------------------
    def get_print_metadata(self, file_name, thumb_enabled):
        """Load metadata JSON and send print info + thumbnail to the firmware."""
        try:
            self._plugin_logger.info(f">>>>>> Called get_print_metadata for {file_name}")

            # Always reset this so we don't skip waiting due to stale state
            self.is_lcd_ready = False

            md = self.load_metadata_from_json(file_name)
            if md is None:
                self._plugin_logger.error("get_print_metadata: metadata JSON not found/invalid.")
                return None

            self.file_name = md.get("file_name")
            self.file_path = md.get("file_path")
            self.total_layers = md.get("total_layers")
            self.print_time = md.get("print_time")
            self.current_layer = md.get("current_layer", 0)
            self.progress = md.get("progress", 0)
            self.b64_thumb = md.get("thumb_data")

            self._plugin_logger.info("Sending Print Info (M9000)")
            self._plugin_logger.info(f"File Name: {self.file_name}")
            self._plugin_logger.info(f"Total Layers: {self.total_layers}")
            self._plugin_logger.info(f"Print Time (min): {self.print_time}")
            self._plugin_logger.info(f"Progress (%): {self.progress}")

            # Send the print info using custom command M9000    
            self.send_M9000_cmd(f'N"{self.file_name}"')
            time.sleep(0.3)
            self.send_M9000_cmd(f"T{self.print_time} L{self.total_layers} P{self.progress}")
            time.sleep(0.3)
            self._printer.commands(f"M73 R{self.print_time}")  
            time.sleep(0.3)
            self.send_M9000_cmd("S1")  # start rendering screen

            # Wait for LCD-ready signal from firmware
            t0 = time.time()
            while not self.is_lcd_ready:
                # Avoid infinite loop if printer disconnects
                if not self._printer.is_operational():
                    self._plugin_logger.warning("LCD wait aborted: printer not operational.")
                    return None
                if time.time() - t0 > 30.0:
                    self._plugin_logger.warning("LCD wait timeout (30s). Continuing anyway.")
                    break
                time.sleep(0.2)

            self._plugin_logger.info("LCD print info rendered. Sending thumbnail...")
            time.sleep(0.15)

            if self._settings.get(["enable_gcode_preview"]) and thumb_enabled:
                if not self.sent_imagemap:
                    self.send_thumb_imagemap(self.b64_thumb, "M9001")
            else:
                self._plugin_logger.info("Thumbnail disabled or skipped.")
                self.send_M9000_cmd("D1")

            return True

        except Exception as e:
            self._plugin_logger.error(f"{self.get_current_function_name()}: {e}")
            my_err = f"Error Ocurred! \n \n {e}.\n Try uploading the file again"
            self._plugin_manager.send_plugin_message(self._identifier, {"type": "error_popup", "message": my_err})
            return False

    # -----------------------
    # G-code hooks
    # -----------------------
    def gcode_sending_handler(self, comm_instance, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
        """
        IMPORTANT:
        We do NOT block the queue. Queue blocking can create comm deadlocks.
        Direct-print correctness is handled by pausing the print while we transmit the thumbnail.
        """
        tags = tags or kwargs.get("tags") or set()

        if phase != "queuing":
            return None

        # Always allow our internal commands to pass unchanged
        if "ignore_blocker" in tags:
            return None

        # Always allow temperature polling
        if gcode == "M105":
            return None

        # Optional: log M73 commands
        if cmd.startswith("M73") and self._settings.get(["progress_type"]) == "m73_progress":
            self._plugin_logger.info(f"=================>> GOT M73 command: {cmd}")

        return None

    def gcode_received_handler(self, comm, line, *args, **kwargs):
        # Firmware tells us LCD is ready
        if line.startswith("M9000"):
            if "lcd-rendered" in line:
                self.is_lcd_ready = True
                return line

            if "pause-job" in line:
                if self._printer.is_printing():
                    self._printer.pause_print()
                return line

            if "resume-job" in line:
                if self._printer.is_paused():
                    self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_purge_popup"))
                    self._printer.resume_print()
                return line
            
            if "cancel-job" in line:
                if self._printer.is_printing() or self._printer.is_paused():
                    self._plugin_manager.send_plugin_message(self._identifier, {"type": "error_popup", "message": "Print cancelled from LCD"})
                    self._printer.cancel_print()
                return line

        # Firmware tells us thumbnail finished
        if line.startswith("M9001"):
            if "thumbnail-rendered" in line:
                self._plugin_logger.info("M9001 thumbnail-rendered received from firmware.")
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))

                self.sent_imagemap = True

                # Signal the pause gate wroker (do NOT resume here to avoid PAUSING->PAUSED race)
                self.thumb_rendered_event.set()
                return line

            if "CHUNK" in line:
                if self.get_last_chunk:
                    try:
                        self.chunk_index = int(line.split("|")[0].split(" ")[2])
                    except Exception:
                        pass
                return line

            if self.txLine is not None and f"ACK LINE {self.txLine}" in line:
                self.nextLineAck = True
                return line

        # Busy flags (optional)
        if "busy: processing" in line:
            self.printer_busy = True
            if self.get_last_chunk:
                self.get_last_chunk = False
            return line

        if "ok" in line:
            self.printer_busy = False
            return line

        return line

    # -----------------------
    # Sending commands
    # -----------------------
    def send_M9000_cmd(self, value):
        """Send M9000 with internal tag so our own hook never interferes."""
        cmd = f"M9000 {value}"
        self._printer.commands(cmd, tags={"ignore_blocker"})

    def send_M9001_cmd(self, value):
        """Send raw M9001 command string with internal tag."""
        self._printer.commands(value, tags={"ignore_blocker"})

    # -----------------------
    # Timing helpers
    # -----------------------
    def seconds_to_hms(self, seconds_float):
        if not isinstance(seconds_float, (int, float)):
            seconds_float = 0
        seconds = int(round(seconds_float))
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    def get_elapsed_time(self):
        if self.start_time is None:
            return "0"
        self.elapsed_time = time.time() - self.start_time
        self.start_time = None
        seconds = int(math.ceil(self.elapsed_time))
        minutes = seconds // 60
        return minutes

    # -----------------------
    # Thumbnail pipeline
    # -----------------------
    def send_thumb_imagemap(self, b64, o_cmd):
        self._plugin_logger.info(">>>>>> Sending Thumbnail Image Map")

        if not b64:
            self._plugin_logger.warning("Thumbnail data not found.")
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            self.sent_imagemap = True
            self.thumb_rendered_event.set()  # avoid infinite pause if no thumbnail
            return

        img = self.decode_base64_image(b64)
        pixel_data = self.get_pixel_data(img)

        expected_size = 96 * 96 if o_cmd == "M9001" else 26 * 240
        if len(pixel_data) != expected_size:
            raise ValueError(f"Expected pixel data size {expected_size}, but got {len(pixel_data)}")

        self.send_image_to_marlin(pixel_data, o_cmd)

    def send_image_to_marlin(self, pixel_data, o_cmd):
        """
        CRITICAL:
        When printing (or paused during print), OctoPrint uses numbered lines: 'N.. <cmd> *checksum'
        That increases line length. If the command becomes too long, Marlin may complain:
        'Error:No Checksum with line number' and request Resend forever.

        Fix: Reduce pixels_per_chunk in numbered mode.
        """
        numbered_mode = self._printer.is_printing() or self._printer.is_paused()

        # Safer default in numbered mode (keep lines short)
        pixels_per_chunk = 12 if numbered_mode else 20

        self._plugin_logger.info(
            f"Starting HEX thumbnail transmission (pixels_per_chunk={pixels_per_chunk}, numbered_mode={numbered_mode})"
        )

        try:
            self._printer.commands(f"{o_cmd} START", tags={"ignore_blocker"})

            for y in range(96):
                start_idx = y * 96
                line_data = pixel_data[start_idx:start_idx + 96]
                hex_string = "".join(f"{val:04X}" for val in line_data)

                step = pixels_per_chunk * 4  # 4 hex chars per pixel

                for i in range(0, len(hex_string), step):
                    chunk_hex = hex_string[i:i + step]
                    x_offset = i // 4
                    command = f"{o_cmd} C {y} {x_offset} {chunk_hex}"
                    self._printer.commands(command, tags={"ignore_blocker"})

            self._printer.commands(f"{o_cmd} END", tags={"ignore_blocker"})
            self.sent_imagemap = True

        except Exception as e:
            self._plugin_logger.error(f"send_image_to_marlin error: {e}")
            # If we fail, unblock the pause gate to avoid leaving the printer paused forever
            self.thumb_rendered_event.set()

    def decode_base64_image(self, b64_string):
        image_data = base64.b64decode(b64_string)
        return Image.open(io.BytesIO(image_data))

    def get_pixel_data(self, image):
        img = image.convert("RGB")
        width, height = img.size
        imgbytes = img.tobytes()

        pixel_map = []
        for y in range(height):
            for x in range(width):
                idx = (y * width + x) * 3
                r_scaled = (imgbytes[idx] * 31) // 255
                g_scaled = (imgbytes[idx + 1] * 63) // 255
                b_scaled = (imgbytes[idx + 2] * 31) // 255
                color16bit = (r_scaled << 11) | (g_scaled << 5) | b_scaled
                pixel_map.append(color16bit)
        return pixel_map

    # -----------------------
    # Parsers
    # -----------------------
    def find_total_layers_from_content(self, file_content):
        for line in file_content.splitlines():
            if "; total layer number:" in line:
                return line.strip().split(":")[-1].strip()
            if ";LAYER_COUNT:" in line:
                return line.strip().split(":")[-1].strip()
        return None

    def find_first_m73_from_content(self, file_content):
        for line in file_content.splitlines():
            m73_match = re.match(r"M73 P(\d+)(?: R(\d+))?", line)
            if m73_match:
                progress = int(m73_match.group(1))
                remaining_minutes = int(m73_match.group(2)) if m73_match.group(2) else 0
                if progress == 0:
                    return progress, remaining_minutes
        return 0, 0

    def extract_thumbnail_from_content(self, file_content):
        thumbnail = None
        collecting = False
        current_thumbnail = []
        slicer_type = None

        for line in file_content.splitlines():
            line = line.strip()

            if "; generated by OrcaSlicer" in line:
                slicer_type = "OrcaSlicer"
            elif ";Generated with Cura" in line:
                slicer_type = "Cura"

            if slicer_type == "OrcaSlicer":
                if line == "; THUMBNAIL_BLOCK_START":
                    collecting = False
                    current_thumbnail = []

                if line.startswith("; thumbnail begin 96x96") or line.startswith("; thumbnail_PNG begin 96x96"):
                    collecting = True
                    continue

                if collecting:
                    if line.startswith("; thumbnail end") or line.startswith("; thumbnail_PNG end"):
                        collecting = False
                        thumbnail = "".join(current_thumbnail)
                        break
                    current_thumbnail.append(line.lstrip("; ").rstrip())

            elif slicer_type == "Cura":
                if line.startswith("; thumbnail begin 96x96"):
                    collecting = True
                    current_thumbnail = []
                    continue

                if collecting:
                    if line.startswith("; thumbnail end"):
                        collecting = False
                        thumbnail = "".join(current_thumbnail)
                        break
                    current_thumbnail.append(line.lstrip("; ").rstrip())

        if thumbnail:
            self._plugin_logger.info(f"Extracted thumbnail, size: {len(thumbnail)} characters")
        else:
            self._plugin_logger.warning("No valid thumbnail found in GCODE")

        return thumbnail

    # -----------------------
    # Cleanup
    # -----------------------
    def cleanup(self):
        # Reset job timing
        self.start_time = None
        self.elapsed_time = None

        # Reset metadata
        self.print_time = None
        self.progress = None
        self.myETA = None

        # Reset LCD / print state
        self.is_lcd_ready = False
        self.current_layer = 0
        self.total_layers = 0
        self.print_finish = False
        self.sent_imagemap = False

        # Reset comm helpers
        self.printer_busy = False
        self.get_last_chunk = False
        self.chunk_index = 0
        self.txLine = None
        self.nextLineAck = False

        # Stop gates (never keep the printer paused because of us)
        self._stop_pause_gate()
        self.thumb_rendered_event.clear()
        self.pause_gate_thread = None

        # Reset metadata thread guard
        with self._metadata_lock:
            self._metadata_running = False
            self._metadata_last_file = None

   
    def get_update_information(self):
        return {
            "LCD_E3V3SE": {
                "displayName": "LCD_E3V3SE Plugin",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "navaismo",
                "repo": "OctoPrint-LCD_E3V3SE",
                "current": self._plugin_version,
                "pip": "https://github.com/navaismo/OctoPrint-LCD_E3V3SE/archive/{target_version}.zip",
            }
        }


__plugin_pythoncompat__ = ">=3,<4"
__plugin_version__ = "0.0.7"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_name__ = "LCD_E3V3SE"
    __plugin_implementation__ = LCD_E3V3SEPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.filemanager.preprocessor": __plugin_implementation__.file_preprocessor,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gcode_sending_handler,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received_handler,
    }
