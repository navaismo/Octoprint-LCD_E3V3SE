# Unified LCD protocol compatibility

`main_cleanup` supports OctoPrint 1.11.x and 2.0.x through the same plugin
identifier, settings namespace and public printer APIs. The v2 transport,
96x96 RGB565 processing, thumbnail recovery/model fitting, M600 handling and
purge state are retained. No runtime dependency was added. Version numbers,
release tags and firmware repositories are unchanged.

## Firmware audit

The implementation was checked against the local source of
`navaismo/Marlin_bugfix_2.1_E3V3SE` on 2026-11-18:

| Branch | Audited commit | Protocol |
|---|---|---|
| c13_octoprint | c06a4b829e67ec0e07548b29260ad490ae3f5b1a | v1 |
| c14_octoprint | bd5f9b8a5b8ac9da1aca95cd6a15b9c4bc8373ae | v1 |
| c13_pure_octoprint | fda70e7383a03c5659ee066ce78f2ca269c2004b | v2 |
| c14_pure_octoprint | e764b2d237b12a70f542fe495d17ebbb83511700 | v2 |

The paired C13/C14 OctoHost implementations match within each protocol family.
The audit reads OctoHost.cpp, M73.cpp, DWIN_RenderOctoLine, the native print
screen, LcdDraw.cpp and the firmware's host-status/protocol documentation.
Legacy sends `ACK LINE n` after processing a row. Its native coordinates are
x=12..107, y=25..120; v2 keeps its existing host frame and image at (12,26).
The firmware's MAX_CMD_SIZE is 96 and MeatPack remains on the normal connection.
The plugin's `main` branch was consulted only for START/C/END serialization and
legacy metadata semantics; development remains based on `main_cleanup`.

The standard firmware requires OCTOPRINT_PLUGIN and DWIN_RENDER_THUMBNAIL. Pure
Host additionally requires M9001_DRAW_PROTOCOL and its matching LCD resources.
These checks do not establish compatibility with arbitrary third-party forks.

## Modules and lifecycle

- `firmware_caps.py`: one connection-scoped capability result, bounded waits,
  overrides and disconnect notification.
- `drawing.py`: existing v2 DrawingClient, packets, CRC32, session IDs, sequence
  checks, 42-byte batches, retry, abort and ACK/DONE waits. Only CAPS validation
  is shared with the detector; the v2 wire format/state machine is preserved.
- `drawing_legacy.py`: separate START/C/END transport, twelve pixels per command,
  one row of outstanding data, START/row/completion acknowledgments and deadlines.
- `firmware_commands.py`: small M9000 mapping for each protocol family.
- `__init__.py`: existing single preparation worker, beforePrintStarted hook,
  counted hold owner, metadata, received hooks, native actions and cancellation.

The synchronous script hook acquires the hold before background preparation,
as in the hardware-validated implementation. This intentionally holds the file
stream while metadata and capability negotiation run; moving acquisition into
the worker would allow a direct print to race preparation. Selecting a file and
starting it directly share that owner. Repeated selection/start events reuse
the worker. A new selection cancels old clients and retains the worker's single
hold until pending work is finished.

After metadata/image preparation and protocol selection, the worker sends the
family-specific metadata and S1, transfers the image, waits for completion,
refreshes v2 native fields and releases its hold. Native pause/resume state and
M876 remain under their original owners. No private communication queue is used.

Connection generations reject old-worker drawing commands on a replacement
connection. Disconnect wakes negotiation and both transports, releases the
owned hold where the public printer API still permits it, and discards the old
ownership token. Reconnect creates fresh clients and performs one new query.
An unfinished worker may process a pending new job, acquiring the new connection's
hold first. Its old completion cannot decrement that replacement hold.

## Protocol negotiation

On each Connected event, A1 announces the host and DrawingClient sends one
`M9001 Q`. No START probe is sent on connection, even with the legacy override.

Auto Detect accepts `M9001 CAPS 2 42 240 320`, optionally followed by the firmware's
zero session/sequence fields (`00000000 0000`). The version, payload limit,
dimensions and optional fields must match. A delayed valid response within the
two-second negotiation window wakes the worker. A valid result is reused for
later files without another query. Malformed/unsupported CAPS abort automatic
preparation; a plain `ok` does not identify firmware.

If the window expires without CAPS, v1 is only a candidate. Pixel transmission
requires the actual START acknowledgment. A legacy override selects that
handshake immediately; it cannot override already-confirmed v2 firmware. A v2
override still requires valid CAPS and never falls back to v1. Changing the
setting applies on the next selection; reconnect resets all learned state.
Settings show the current connection status through the authenticated Simple API
and plugin messages. The log records the selected backend and handshake errors.

## Legacy transfer and limits

The worker sends `M9001 START` and waits for `M9001 START`. It then serializes all
96 rows in row-major order:

```text
M9001 C <row 0..95> <offset 0,12,..84> <12 four-digit RGB565 words>
```

Eight commands fill a row. The next row is sent only after its exact
`M9001 ACK LINE <row>` response. The longest command is 62 characters, or 78
including `N2147483647 ` and `*255`, within MAX_CMD_SIZE=96 including terminators.
Hex payloads contain no G, preserving the existing MeatPack path.

After row 95, END must produce `M9001 END` followed by
`M9001 thumbnail-rendered`. An ordinary serial `ok` never confirms completion.
The default START/row/END timeout is three seconds and the whole session is
bounded to 120 seconds. Missing acknowledgments fail rather than retransmitting
unidentified rows. Cancellation stops producing commands without sending a
false END. At most eight already-queued pixel commands remain in flight.

Legacy has no session IDs, CRC32, ABORT or physical LCD fence. Its completion is
the existing firmware's completion acknowledgment, not v2's stronger fence.
Independent clients must not inject START/C/END during a plugin transfer. After
failure, queued rows cannot be recalled through public printer APIs. A subsequent
selection's START resets the native line buffer. Physical UART/LCD timing,
electrical communication errors and unusually slow firmware still need tests.

Missing/invalid/disabled thumbnails open the legacy native screen without a
START or pixel stream. V2 retains its existing MODEL placeholder. Image sizes,
native artwork and LCD layouts are unchanged.

## M9000 mapping

| Operation | Legacy v1 | Pure Host v2 |
|---|---|---|
| Host connection | A1, once on connection | A1, once on connection |
| Filename | N with sanitized title | N with sanitized title |
| Total layers | L | L |
| Initial time | T in minutes for legacy Print Time | E fixed total minutes |
| Layer height | Not sent | H in micrometers |
| Remaining time | Native M73 R | Native M73 R |
| Progress/current layer | Native M73 P/L; initial M9000 P0 | Native M73 P/L |
| Open print screen | S1 after metadata | S1 after metadata |
| Transfer complete | END restores the native title | D1 restores native field values |
| Host paused/resumed | R1/R0, supported by the audited standard branches | R1/R0 |
| Print finished | T elapsed minutes, L total, P100, then F1 | F1; native timer and fixed E remain |

T sets `ui.print_time` via minutes-to-seconds conversion. Although its handler
comment calls it elapsed time, legacy's initial Print Time display reads that
value; the original plugin supplied the estimate there, and replaces it with
elapsed time on completion. V2's distinct fixed ETA field uses E. H/E/D1 are
never sent to v1. Both audited firmware families implement R notifications as
LCD state changes; they do not resume motion or answer a filament prompt.
Older legacy builds without R support may ignore these notifications.

M73 and M117 handling is preserved, including native L/current-layer behavior.
There is no new marker conversion or recurring M9000 progress mirror. V2's
session format, native controls and fixed estimate behavior remain unchanged.

## Metadata, settings and API compatibility

Existing cache fields and settings keys remain valid. `lcd_protocol` is the only
new setting, defaulting to `auto`. The preprocessor accepts both versions' hook
calls with optional links/printer_profile/allow_overwrite arguments and extra
positional/keyword arguments. Existing is_api_protected and template autoescape
methods remain true; purge still verifies CONTROL permission, paused state,
feature enablement and the known extrusion state.

A missing JSON file is a cache miss rather than an error popup. When available,
the stored G-code repairs metadata using a bounded 256 KiB header, the existing
bounded layer-height reader and optional streamed thumbnail fallback. Recovered
fields are saved once after preparation while preserving other cached fields.
If no source can be read, native placeholders remain usable. Metadata logs show
only filename, thumbnail presence/encoded size, layers, height, estimate and
selected protocol; they do not include the encoded thumbnail.

## Automated validation

Run from the repository root in each real OctoPrint environment with Pillow:

```sh
python -m unittest discover -s tests
node tests/test_purge_frontend.js
node tests/test_protocol_frontend.js
python -m compileall -q octoprint_LCD_E3V3SE
git diff --check
```

The original suite remains intact. Added tests cover negotiation, exact legacy
pixels/row offsets/line lengths, missing START/row/END acknowledgments, bounded
production, cancellation, backend-specific metadata, direct print, duplicate
selection, reconnection ownership, optional preprocessor calls, protected API,
safe logging and the settings status view. CRC, session, sequence, retry, drawing
bounds, model fitting, metadata repair, pause and purge regressions still run.

The test environment uses Python 3.10 and separate temporary dependency directories
for the actual OctoPrint 1.11.8 and 2.0.0 packages, inside a disposable container.
The plugin source is mounted read-only and test runs have networking disabled.
Firmware and printer communication are simulated; no server or printer is
installed, flashed or exercised by these tests.

Final software results on 2026-11-18, with Python 3.10.18 and Pillow 12.3.0:

| Environment/check | Passed | Failed/errors | Skipped |
|---|---:|---:|---:|
| OctoPrint 1.11.8, full unittest suite | 95 | 0 | 0 |
| OctoPrint 2.0.0, full unittest suite | 95 | 0 | 0 |
| Purge frontend script | 1 script | 0 | 0 |
| Protocol frontend script | 1 script | 0 | 0 |
| Python compileall / git diff --check | Passed | 0 | 0 |

All 66 original Python cases run unchanged in each environment; 29 new cases
extend them. Error traces in negative-case test output are intentional simulated
failures, followed by assertions that the owned hold was released. Existing
test files, image processing and purge.py have no diff from the starting commit.

## Working-tree changes

Modified files:

- `octoprint_LCD_E3V3SE/__init__.py`: backend selection, shared lifecycle,
  connection/hold generation guards, recoverable cache miss, compact logs and
  authenticated status API; optional preprocessor arguments remain supported.
- `octoprint_LCD_E3V3SE/drawing.py`: share strict CAPS validation with the detector.
- `octoprint_LCD_E3V3SE/static/js/LCD_E3V3SE.js`: authenticated status refresh.
- `octoprint_LCD_E3V3SE/templates/settings.LCD_E3V3SE.jinja2`: protocol selector/status.
- `README.md`: firmware matrix, configuration, failure handling and test commands.
- `docs/host-only-rendering.md`: identify the v2 guide within the unified plugin.

Added files:

- `octoprint_LCD_E3V3SE/drawing_legacy.py`
- `octoprint_LCD_E3V3SE/firmware_caps.py`
- `octoprint_LCD_E3V3SE/firmware_commands.py`
- `tests/test_drawing_legacy.py`
- `tests/test_firmware_caps.py`
- `tests/test_unified_plugin.py`
- `tests/test_protocol_frontend.js`
- `docs/unified-protocols.md`

This is six modified files and eight new files. Standard `git diff --stat`
reports tracked modifications only; inspect the new files with
`git ls-files --others --exclude-standard` as well. No existing tests were
removed or weakened. No branch, commit, push, PR, version bump, release or
Marlin modification was made.

## Physical validation: all four combinations

Use an authenticated OctoPrint session and the correct modified firmware/TFT
resources for the actual printer. Home normally before selecting a job. Enable
serial logging and retain the plugin log. Use a short, known printable local
G-code with a 96x96 thumbnail, M73 P/R and native M73 L, then a second file with a
different name/image. Also prepare one file without a thumbnail. These steps
are manual deployment checks, not claims of hardware validation.

Run each row below as a separate session, using the stated OctoPrint version:

| Session | OctoPrint | Firmware | Expected Auto Detect result |
|---|---|---|---|
| 1.x/v1 | 1.11.x | c13_octoprint or c14_octoprint | No CAPS; START confirms legacy |
| 1.x/v2 | 1.11.x | c13_pure_octoprint or c14_pure_octoprint | CAPS confirms v2 |
| 2.0/v1 | 2.0.x | c13_octoprint or c14_octoprint | No CAPS; START confirms legacy |
| 2.0/v2 | 2.0.x | c13_pure_octoprint or c14_pure_octoprint | CAPS confirms v2 |

For each session:

1. Install the working-tree plugin build in that OctoPrint environment and restart
   it. Check startup, settings persistence, authenticated protocol status and
   the existing purge permission checks. Set LCD Protocol to Auto Detect.
2. Connect the printer. Confirm exactly one A1 and Q, and no START on connection.
   For v2, verify CAPS 2/42/240/320. For v1, confirm that an ignored Q does not
   prevent normal host communication. Check the connection status in settings.
3. Upload the thumbnail file. Confirm metadata JSON creation and compact logs.
   Select it. Verify that file motion waits during rendering and the native LCD
   opens with the correct title, layers, time and unchanged controls/artwork.
4. For v1, verify START acknowledgment, eight C chunks per row, sequential ACK LINE
   responses, END and thumbnail-rendered before file streaming continues. Confirm
   the complete image, its orientation, all row edges and the legacy Print Time.
   For v2, verify B/D/E, matching session/sequence ACKs, DONE and D1 before file
   streaming continues. Confirm layer height, fixed ETA and the native image frame.
5. Start a print directly from upload without a separate selection. Repeat with a
   previously selected file. Each job must render once and release one owned hold.
   Observe native M73 percentage/layers/remaining time. In v2, the fixed E/ETA
   remains unchanged by later R updates. Confirm the normal completion screen;
   v1 receives elapsed T before F1, while v2 receives F1 without legacy T.
6. Select the second file during rendering and again after completion. Check that
   the most recent file/image wins, there is one worker, and the old worker does
   not unblock a newer render. Test the thumbnail-free file and preview disabled.
   V1 should use its native screen; v2 should retain MODEL.
7. Cancel during rendering, disconnect during START/DATA, and reconnect. Check
   prompt release, no automatic resume/cancel of unrelated jobs, no END for an
   incomplete v1 image, no stale drawing commands, and one new Q on reconnect.
   Test a delayed/missing acknowledgment using a firmware simulator if available.
8. Pause on the LCD and resume in OctoPrint, then reverse the controls. Confirm
   R1/R0, icon/title state and unchanged progress. Open Tune and M600, exercise
   native prompts, and use the optional purge only at the appropriate prompt or
   known normal paused state. A render must not answer M876 or resume motion.
9. Save the matching manual override and select a new file: Legacy for v1, Draw v2
   for v2. Confirm the same required handshake. On v1, force Draw v2 and confirm
   timeout/release with no B/D/E. On v2, force Legacy and confirm rejection with
   no START/pixels. Restore Auto Detect. Test plain serial and MeatPack separately.
10. Retain OctoPrint/firmware versions, logs, screenshots and observed transfer
    duration. Repeat on the other C13/C14 hardware when available; matching source
    alone does not prove that second printer's electrical/LCD behavior.

Physical tests of these four combinations remain outstanding. Previous user
reports of working v2 on OctoPrint 2.0.0 describe the starting implementation,
not physical validation of this compatibility extension.
