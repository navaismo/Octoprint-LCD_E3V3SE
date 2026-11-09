# Host-only print-card rendering (M9001 v2)

Use `main_cleanup` together with Marlin `c13_octoprint_cleanup`. The native LCD
MAIN and controls remain in Marlin. This plugin does not replace MAIN, Tune,
pause/stop confirmation dialogs, temperature status or safety prompts.

## Startup and lifecycle

On connection the plugin sends M9000 A1 and negotiates M9001 Q once. There is
no recurring capability query or heartbeat. Selecting a local file starts a
single background preparation/render worker. The synchronous OctoPrint
`beforePrintStarted` script hook also starts it when selection and print start
race. The worker acquires one counted `set_job_on_hold` hold before file
streaming; plugin commands can still pass through the ordinary connection.

After M9000 supplies title, total layers, known layer height and fixed total
estimated minutes (E), S1 opens the
native print screen. The host renders only y=25..122. It sends one bounded
batch at a time, waits for its application credit, then requests an LCD fence.
Only DONE means the transfer completed. Ordinary `ok` grants no drawing credit.
M9000 D1 restores the local field values after composition. Finally the worker
releases exactly its own hold, including on errors or disconnect. It never
calls resume_print as part of rendering, answers M876 or invokes pause scripts.
A hold belonging to another plugin, a real pause or a filament prompt survives.

A transfer can be cancelled by the encoder, a native modal, another command,
a firmware prompt, a different selected file or disconnect. One batch is the
maximum outstanding work. Five seconds without progress expires the MCU
session; the complete session has a two-minute limit. Missing/invalid images
produce a small MODEL placeholder and the same usable native controls.

## Graphics and metadata

Images are resized/letterboxed to 96x96, composited onto black and converted to
RGB565 on the host. Horizontal runs merge vertically into rectangles; disjoint
rectangles are grouped by color. Black pixels are omitted after initialization.
A bounded 19-pixel STRIP fallback avoids sending a COLOR/RECT pair in host G-code
for every high-entropy pixel. It is selected only when it cannot increase the
number of LCD drawing frames and reduces operation bytes.

The host draws only the 96x96 thumbnail at (12,26) and its green frame at
x=10..109, y=25..122. All four frame edges are drawn last, below the scrolling
title (ending at y=24) and above Tune's clear area (starting at y=123). Native
progress remains on the right; host operations never clear or draw over it.
There is no metadata table or separator grid.

Marlin draws four plain rows at y=127/143/159/175, above the native buttons
starting at y=191. Labels Layers, Layer Height, ETA and Elapsed are cyan
(RGB565 0x07FF). Values are centered in x=124..233 on a dark background.
The current layer and separator are white, the fixed total layer count is yellow.
ETA is the initial total estimated duration in HH:MM, fixed for the job and yellow;
Elapsed is Marlin's advancing print timer in HH:MM and white. Layer height remains
white. The value clear starts at x=120, beyond the complete Layer Height label. A
conservative eight-pixel glyph width reserves room for `65535 / 65535` without
truncation. Full 16-pixel value cells are cleared when changed, including after
values become shorter. Marlin uses the existing M73 L current layer, M9000 L
total layers, M9000 H layer height in micrometers, M9000 E total estimated minutes,
and its print timer. HEIGHT means layer thickness, not current Z or total model
height. The plugin sends E once before S1, using the initial estimate cached from
the first M73 P0 R command. Live M73 R time-left updates do not change ETA.
Missing or invalid estimates explicitly send E0 and show --; MAIN clears the
previous job's estimate.
Missing totals/heights/ETA use `--`; layers require actual slicer M73 L output,
as in the existing firmware. Unknown current layers are not inferred from Z
moves (which may include Z hops).

The layer-height parser reads the exact `layer_height` key, including Orca's
settings after `; EXECUTABLE_BLOCK_END`. The last valid value wins, ignoring
`first_layer_height`, support heights and minimum/maximum settings. Existing
JSON metadata without a usable height is repaired from the stored local G-code
before rendering, using OctoPrint's path resolver (including subdirectories).
The reader checks at most 64 KiB of footer and, if needed, 64 KiB of header;
it never loads a large G-code into memory during print preparation. A recovered
height is persisted while retaining other cached metadata, so the next print
uses the cache. Valid cached heights skip file I/O. Missing/unreadable files or
unknown heights retain the placeholder; no re-upload is required for recovery.
Cancelled preparation cannot publish a stale recovered height.

The optional cyan Progress caption at (139,107) is currently disabled: its
Marlin draw call is commented out after hardware tests reported timeouts and
checksum resends. The cube percentage remains active. Removing this call is an
isolation step, not a confirmed fix; normal G-code checksums are separate from
M9001 batch CRC. The last measured build/test results predate this change.

During printing the host does not resend the thumbnail, layout, elapsed time
or temperatures. Marlin checks one field every 1.25 seconds (each field roughly
five seconds), draws only changed values and preserves the native thermal bar.
Tune draws below y=123 and leaves the upper card intact. Returning redraws only
native controls, all four labels and current values; the thumbnail and its
frame remain intact. Native pause/resume/cancel actions still use OctoPrint.
Confirmed Stop also clears the card and returns to MAIN before printing has
started. The plugin cancels any preview worker and releases its own job hold,
without asking OctoPrint to cancel an idle printer.
If the connection is physically gone, resuming a serial job requires reconnecting
the host; display ownership and encoder handling do not wait for that connection.

## Wire format

`M9001 Q` returns `M9001 CAPS 2 42 240 320 00000000 0000`.
Other commands have a type byte B/D/E/A followed immediately by armored Base64.
This is standard Base64 **with every uppercase G replaced by underscore**.
Unmodified standard Base64 is rejected. The substitution avoids the existing
MeatPack encoder's search for `G` followed by a digit anywhere in a line.
There is no new serial connection, raw-byte mode, baud-rate change or parser
modification. The largest command is 79 characters; including N2147483647 and
*255 it occupies 95 characters, within MAX_CMD_SIZE=96 including NUL.

All integers are big endian. CRC is the standard zlib CRC32 of the ASCII type
byte followed by the decoded bytes preceding the CRC:

| Type | Decoded record before CRC32 |
|---|---|
| B | version:u8=2, session:u32 (nonzero) |
| D | version:u8=2, session:u32, sequence:u16, payload:1..42 bytes |
| E | version:u8=2, session:u32, next_sequence:u16, stream_crc32:u32 |
| A | version:u8=2, session:u32 |

Stream CRC covers concatenated DATA payloads. Records never split an operation.
COLOR is `[0, color:u16]`; RECT is `[1,x0,y0,x1,y1]`; STRIP is
`[2,x,y,count,RGB565...]` with 1..19 pixels. Coordinates are u8; rectangles and
strips must remain within x=0..239, y=25..122. COLOR starts the first batch.
STRIP leaves the last pixel's color selected, just like explicit COLOR/RECT.
Black is opaque when explicitly drawn.

Responses are `M9001 ACK <session8hex> <next_sequence4hex>` and matching DONE.
Errors replace ACK with FORMAT, CRC, BOUNDS, ORDER, SESSION or BUSY. ABORTED
means ownership ended. The immediately preceding DATA can be retried with the
same checksum without redrawing. Stale sessions cannot affect the active one.
The final reply requires a fresh AA 00 OK ... CC33C33C LCD response and a 50 ms
settle interval; old buffered replies are drained incrementally first.

## Validation

Run `python -m unittest discover -s tests` and
`node tests/test_purge_frontend.js`. Raster tests require the already-declared
Pillow dependency. Lifecycle/API tests require OctoPrint. The paired firmware
repository supplies ASan/UBSan, exact framebuffer, MeatPack and byte-count tests.
Physical LCD timing, serial print quality and printer lifecycle checks remain
necessary before deployment. No plugin installation or firmware flashing is
performed by these tests.
