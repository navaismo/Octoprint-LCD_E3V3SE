# OctoPrint-LCD_E3V3SE

Plugin to send print job details and thumbnails to the LCD of the E3V3SE (requires modified firmware).
Fully compatible with Meatpack and M600 commands

## Table of contents
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Preview](#preview)
- [Starting Job](#starting-job)
- [Disclaimer](#disclaimer)
- [License](#license)
- [Author](#author)



## Requirements.
> [!Important]
> 
> * Enable M73 in the slicer. Follow the [Wiki Article to Enable M73 Support](https://github.com/navaismo/Octoprint-LCD_E3V3SE/wiki/Enable-M73-Support).
> * Install the Custom Firmware for the Printer, [Ensure to use the Latest BELAYA Version](https://github.com/navaismo/Marlin_bugfix_2.1_E3V3SE/releases). 
> 


## Installation.

> [!TIP]
> 
> 1. Download from the Release Page the Latest Version.
> 2. Open OctoPrint and go to `Settings` -> `Plugin Manager`.
> 3. Click `Get More...`.
> 4. Select `Install from File` and add the downloaded ZIP
> 5. Restart OctoPrint.
>


## Configuration. 
1. Open OctoPrint and go to `Settings` -> `LCD E3V3SE`.
2. Configure the plugin options:
   - `Enable G-code Preview`: send and render the 96x96 thumbnail on the LCD.
   - `Progress Type`: `M73`-based progress parsing.
   - `Enable Purge Filament`(Optional): show a purge prompt when the printer is paused.
3. Click `Save` and restart OctoPrint if prompted.

![Settings](./media/Settings.png)



## Preview.

![LCD](./media/LCD.gif)


## Starting Job.


* While Rendering the LCD you will see the Pop-Up in the screen:

![PopUp](./media/PopUp.png)

> [!Important]
> 
> When starting a Job the printer will Pause and execute the `PAUSE SCRIPT` defined in the Ocotprint Script settings. 
> So the first thing after connect Octoprint is to HOME the AXIS! Otherwise the printer will fail to move to the Pause Position.

* The printer will Pause, Render and Continue the job:

![StartJob](./media/StartJob.gif)





## Disclaimer
BAU:
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

TL;DR:
If your house burns down because this app malfunctioned, you cannot sue me.

## License
This project is licensed under the AGPL license.

## Author
Created by navaismo

## Purging a paused print

The optional purge button asks the backend to extrude 15 mm at 150 mm/min.
It preserves the E coordinate, extrusion mode and feedrate and keeps the motors
enabled. Units must be confirmed as millimeters; unknown extrusion state is
rejected rather than assuming M82 or M83. The plugin observes sent G-code and
reads M503/M114 during an ordinary pause. These read-only commands do not move
or reconfigure the printer. Reconnects clear the saved state.

During a firmware filament change, the button uses M876 S0 only when Marlin
offers Purge more. Marlin then controls the purge length and preserves its own
resume position. Earlier insertion/heating prompts must be answered normally.
Purge requires printer-control permission and an enabled purge option; it is
unavailable during printing or the thumbnail pause gate. Resume still belongs
to OctoPrint and the configured pause/resume scripts.

Run the offline regression checks with `python -m unittest discover -s tests`
and `node tests/test_purge_frontend.js`. Printer validation is still needed for
absolute/relative extrusion, repeated purges and M600 prompt handling.
