# Coding=utf 8
from __future__ import absolute_import

import io
import os
import re
import json
import time
import base64
import inspect
import logging
import threading

from flask import jsonify
from octoprint.access.permissions import Permissions
from .purge import PurgeState
from .drawing import DrawingClient
from .print_card import card_operations, rgb565

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
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin
):
    def __init__(self):
        self._purge = PurgeState()
        self._draw = DrawingClient(lambda cmd: self._printer.commands(cmd, tags={"ignore_blocker"}))
        self._render_generation = 0
        self._render_attempted = False
        self._pending_render = None
        self._rendering_generation = -1
        self._hold_lock = threading.RLock()
        self._hold_owned = False

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

        # File-stream hold used only during initial card rendering.

        # --- Metadata thread guard (avoid double-start per file) ---
        self._metadata_lock = threading.Lock()
        self._metadata_running = False

        # Completion never changes OctoPrint pause or prompt state.
        self.thumb_rendered_event = threading.Event()
        self.pause_gate_active = False
        
        # Add in __init__ (new state tracking)
        self._last_state_id = "UNKNOWN"
        self._last_state_ts = time.time()

        # --- Timing ---
        self.start_time = None
        self.elapsed_time = None

        # --- Logger ---
        self._plugin_logger = logging.getLogger("octoprint.plugins.LCD_E3V3SE")

    def get_api_commands(self):
        return {"purge_filament": []}

    def on_api_command(self, command, data):
        if not Permissions.CONTROL.can():
            return jsonify(error="Printer control permission is required."), 403
        if command != "purge_filament":
            return jsonify(error="Unknown command."), 400
        if not self._settings.get_boolean(["enable_purge_filament"]):
            return jsonify(error="The purge option is disabled."), 409
        if not self._printer.is_paused() or self.pause_gate_active:
            return jsonify(error="Purge is available only during a normal paused print."), 409
        try:
            commands = self._purge.begin()
        except ValueError as error:
            return jsonify(error=str(error)), 409
        try:
            self._printer.commands(commands, tags={"plugin:LCD_E3V3SE:purge"})
        except Exception:
            self._purge.reset()
            self._plugin_logger.exception("Could not queue the purge sequence")
            return jsonify(error="Could not queue purge. Extrusion state must be read again."), 503
        return jsonify(success=True)

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
            "layer_height": self.find_layer_height_from_content(file_content),
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
        if not file_name or self._render_attempted:
            return
        with self._metadata_lock:
            if self._metadata_running and self._rendering_generation == self._render_generation:
                return
            self._pending_render = (file_name, thumb_enabled, self._render_generation)
            if self._metadata_running:
                return
            with self._hold_lock:
                if not self._printer.set_job_on_hold(True, blocking=False):
                    self._plugin_logger.warning("Could not hold the job; keeping native print controls")
                    self._render_attempted = True
                    self._pending_render = None
                    return
                self._hold_owned = True
                self.pause_gate_active = True
            self._metadata_running = True

        def worker():
            while True:
                with self._metadata_lock:
                    pending = self._pending_render
                    self._pending_render = None
                    if pending is not None:
                        self._rendering_generation = pending[2]
                if pending is not None:
                    name, enabled, generation = pending
                    try:
                        self.get_print_metadata(name, enabled, generation)
                    except Exception:
                        self._plugin_logger.exception("Print-card rendering failed; releasing the job hold")
                    finally:
                        if generation == self._render_generation:
                            self._render_attempted = True
                with self._metadata_lock:
                    if self._pending_render is not None:
                        continue
                    self._release_render_hold()
                    self._metadata_running = False
                    self._rendering_generation = -1
                    break
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
        threading.Thread(target=worker, daemon=True).start()

    def _release_render_hold(self):
        # OctoPrint counts holds. Release exactly the one acquired by this worker.
        with self._hold_lock:
            if self._hold_owned:
                try:
                    self._printer.set_job_on_hold(False)
                except RuntimeError:
                    pass  # The connection and its hold counter were discarded.
                self._hold_owned = False
            self.pause_gate_active = False

    def _select_render_file(self, name, path):
        self._stop_pause_gate()
        self.file_name, self.file_path = name, path
        self.sent_imagemap = self.is_lcd_ready = False
        self._render_attempted = False
        version = self._draw.version
        self._draw = DrawingClient(lambda cmd: self._printer.commands(cmd, tags={"ignore_blocker"}))
        self._draw.version = version

    def gcode_script_hook(self, comm, script_type, script_name, *args, **kwargs):
        # Read the actual job here: FileSelected is dispatched asynchronously.
        if script_type == "gcode" and script_name == "beforePrintStarted":
            job = self._printer.get_current_job() or {}
            file = job.get("file") or {}
            if file.get("origin") == "sdcard":
                return None
            name, path = file.get("name"), file.get("path")
            if name and (name != self.file_name or path != self.file_path or self._draw.cancelled):
                self._select_render_file(name, path)
            self._start_metadata_thread_once(self.file_name, self._settings.get(["enable_gcode_preview"]))
        return None

    # -----------------------
    # Render cancellation
    # -----------------------


    def _stop_pause_gate(self):
        with self._metadata_lock:
            self._render_generation += 1
            self._pending_render = None
        self._draw.cancel(disconnected=not self._printer.is_operational())
        self.thumb_rendered_event.set()

    # -----------------------
    # Events
    # -----------------------
    def on_event(self, event, payload):
        if event in ("Connected", "Disconnected", "Error", "PrintCancelled", "PrintDone", "PrintFailed"):
            self._purge.reset()
        if event == "Connected":
            self.send_M9000_cmd("A1")
            self._draw.connected()
        elif event in ("Disconnected", "Error", "PrintCancelled", "PrintFailed"):
            self._stop_pause_gate()
        elif event == "FileSelected":
            if payload.get("origin") == "sdcard":
                return
            name, path = payload.get("name"), payload.get("path")
            if (self._metadata_running or self._printer.is_printing()) and name == self.file_name and path == self.file_path:
                return  # The synchronous start hook already owns this selection.
            self._select_render_file(name, path)
            self._start_metadata_thread_once(self.file_name, self._settings.get(["enable_gcode_preview"]))
        elif event == "PrintStarted":
            self.start_time = time.time()
        elif event == "PrintDone":
            self._stop_pause_gate()
            self.send_M9000_cmd("F1")
            self.print_finish = True
        elif event == "PrinterStateChanged":
            self._last_state_id = payload.get("state_id", "UNKNOWN")
            if self._last_state_id == "PAUSED" and self._settings.get(["enable_purge_filament"]) and not self.pause_gate_active:
                if not self._purge.firmware_pause:
                    self._printer.commands(["M503", "M114"], tags={"plugin:LCD_E3V3SE:purge-query"})
                self._plugin_manager.send_plugin_message(self._identifier, {"type":"purge_popup", "message":"Printer is paused. Do you want to purge filament?"})

    # -----------------------
    # Metadata sender
    # -----------------------
    def get_print_metadata(self, file_name, thumb_enabled, generation=None):
        generation = self._render_generation if generation is None else generation
        draw = self._draw
        file_path = self.file_path or file_name
        md = self.load_metadata_from_json(file_name) or {}
        if generation != self._render_generation or self._purge.firmware_pause:
            return False
        total = md.get("total_layers")
        height = md.get("layer_height")
        try:
            total = max(0, min(65535, int(total or 0)))
        except (ValueError, TypeError, OverflowError):
            total = 0
        try:
            height = max(0, min(65535, round(float(height or 0) * 1000)))
        except (ValueError, TypeError, OverflowError):
            height = 0
        if not height:
            # Old metadata JSON may predate layer_height. Recover from the stored
            # G-code before rendering, without requiring another upload.
            recovered = self.find_layer_height_from_file(file_path)
            if generation != self._render_generation or draw.cancelled:
                return False
            if recovered is not None:
                height = round(recovered * 1000)
                md["layer_height"] = recovered
                self.save_metadata_to_json(file_name, md)
        pixels = None
        if thumb_enabled and md.get("thumb_data"):
            try:
                pixels = rgb565(self.decode_base64_image(md["thumb_data"]))
            except Exception:
                self._plugin_logger.warning("Invalid thumbnail; drawing the placeholder", exc_info=True)
        operations = card_operations(pixels)
        if generation != self._render_generation or draw.cancelled:
            return False
        name = "".join(c if 32 <= ord(c) < 127 and c not in ";*" else " " for c in (file_name or "Print")).replace('"', "'").replace("\n", " ").replace("\r", " ")[:49]
        self.send_M9000_cmd('N"' + name + '"')
        self.send_M9000_cmd("L{} H{}".format(total, height))
        estimated = md.get("print_time")
        try:
            estimated = max(0, min(0xFFFFFFFF, int(float(estimated or 0))))
        except (ValueError, TypeError, OverflowError):
            estimated = 0
        # Snapshot the job's initial estimate independently of live M73 R updates.
        self.send_M9000_cmd("E{}".format(estimated))
        self._printer.commands("M73 R{}".format(estimated), tags={"ignore_blocker"})
        self.send_M9000_cmd("S1")
        draw.render(operations)
        if generation != self._render_generation:
            return False
        self.send_M9000_cmd("D1")
        self.sent_imagemap = True
        self.thumb_rendered_event.set()
        return True

    # -----------------------
    # G-code hooks
    # -----------------------
    def gcode_sending_handler(self, comm_instance, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
        """
        IMPORTANT:
        We do NOT block the queue. Queue blocking can create comm deadlocks.
        The synchronous beforePrintStarted hook holds only the file stream.
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

    def gcode_sent_handler(self, comm, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
        if phase == "sent":
            self._purge.sent(cmd)

    def gcode_received_handler(self, comm, line, *args, **kwargs):
        self._purge.received(line)
        self._draw.received(line)
        if line.startswith(("//action:prompt_begin", "//action:paused")):
            self._draw.cancel()
        if line.startswith("M9000"):
            if "lcd-rendered" in line:
                self.is_lcd_ready = True
            elif "pause-job" in line and self._printer.is_printing():
                self._printer.pause_print()
            elif "resume-job" in line and self._printer.is_paused():
                if not self._purge.firmware_pause:
                    self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_purge_popup"))
                    self._printer.resume_print()
            elif "cancel-job" in line:
                # Stop also dismisses a selected-file preview before printing.
                self._stop_pause_gate()
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_purge_popup"))
                if self._printer.is_printing() or self._printer.is_paused():
                    self._printer.cancel_print()
        return line

    # -----------------------
    # Sending commands
    # -----------------------
    def send_M9000_cmd(self, value):
        """Send M9000 with internal tag so our own hook never interferes."""
        cmd = f"M9000 {value}"
        self._printer.commands(cmd, tags={"ignore_blocker"})


    # -----------------------
    # Thumbnail decoding
    # -----------------------


    # -----------------------
    # Thumbnail pipeline
    # -----------------------


    def decode_base64_image(self, b64_string):
        image_data = base64.b64decode(b64_string)
        return Image.open(io.BytesIO(image_data))


    # -----------------------
    # Parsers
    # -----------------------
    def find_layer_height_from_content(self, content):
        # Orca appends its settings after EXECUTABLE_BLOCK_END. The last valid
        # exact key wins; first_layer_height and support settings are different.
        pattern = r"^[ \t]*;[ \t]*(?:layer_height|layer[ \t]+height)[ \t]*[:=][ \t]*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)[ \t]*(?:mm[ \t]*)?\r?$"
        height = None
        for match in re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE):
            value = float(match.group(1))
            if 0 < value <= 65.535:
                height = value
        return height

    def find_layer_height_from_file(self, file_path):
        # Read a bounded footer first, then a header for slicers with no footer.
        # Use OctoPrint's storage path resolver, including subdirectories.
        try:
            path = self._file_manager.path_on_disk("local", file_path)
            with open(path, "rb") as stream:
                limit = 64 * 1024
                start = max(0, os.fstat(stream.fileno()).st_size - limit)
                stream.seek(max(0, start - 1))
                previous = stream.read(1) if start else b"\n"
                tail = stream.read(limit)
                if previous != b"\n":
                    tail = tail.partition(b"\n")[2]  # Ignore a partial first line.
                height = self.find_layer_height_from_content(tail.decode("utf-8", errors="ignore"))
                if height is None and start:
                    stream.seek(0)
                    header = stream.read(limit).rpartition(b"\n")[0]
                    height = self.find_layer_height_from_content(header.decode("utf-8", errors="ignore"))
                return height
        except (OSError, ValueError, AttributeError, TypeError):
            self._plugin_logger.debug("Could not recover layer height from %s", file_path, exc_info=True)
            return None

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
__plugin_version__ = "0.0.9"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_name__ = "LCD_E3V3SE"
    __plugin_implementation__ = LCD_E3V3SEPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.scripts": __plugin_implementation__.gcode_script_hook,
        "octoprint.filemanager.preprocessor": __plugin_implementation__.file_preprocessor,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gcode_sending_handler,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received_handler,
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.gcode_sent_handler,
    }
