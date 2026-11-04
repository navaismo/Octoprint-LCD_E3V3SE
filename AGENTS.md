# Repository Guidelines

## Project Structure & Module Organization

- `octoprint_LCD_E3V3SE/__init__.py` contains the plugin class, OctoPrint hooks, G-code metadata parsing, thumbnail transmission, and pause/resume coordination.
- `octoprint_LCD_E3V3SE/static/js/LCD_E3V3SE.js` implements browser popups and the settings view model.
- `octoprint_LCD_E3V3SE/templates/settings.LCD_E3V3SE.jinja2` defines the settings interface and its styles; `plugin.yaml` lists event hooks.
- `setup.py` defines package metadata and dependencies, including Pillow. `MANIFEST.in` includes packaged assets; `babel.cfg` configures translation extraction.
- `media/` holds README screenshots and animations. No test directory currently exists.

## Build, Test, and Development Commands

Use a Python 3 virtual environment with OctoPrint installed. Run commands from the repository root:

- `python -m pip install -e .` installs the plugin for development using the same environment as OctoPrint. `requirements.txt` delegates to this package.
- `octoprint serve` starts the local OctoPrint server; restart it after backend changes.
- `python setup.py sdist bdist_wheel` creates distribution archives in `dist/`; install `wheel` first if needed.
- `python -m compileall -q octoprint_LCD_E3V3SE` checks Python syntax without exercising printer behavior.

## Coding Style & Naming Conventions

Write all code comments, README content, and project documentation in English.

Follow `.editorconfig`: UTF-8, LF endings, a final newline, no trailing whitespace, and four spaces for Python and JavaScript indentation. No formatter or linter is configured.

Use `snake_case` for new Python helpers and variables; preserve existing OctoPrint hook signatures and plugin identifiers. Follow existing JavaScript `camelCase` naming. Scope CSS beneath `.e3v3selcd` and prefix new popup IDs with `e3v3selcd-` to avoid conflicts with other plugins. Keep settings keys consistent across Python defaults and Knockout bindings.

## Testing Guidelines

No automated test framework or coverage threshold is configured. For new automated tests, use `tests/test_<feature>.py` and document the runner command.

Manually verify settings persistence, G-code uploads with and without thumbnails, M73 progress, thumbnail acknowledgments, pause/resume, cancellation, and optional purge behavior as relevant. Hardware checks require compatible modified firmware and slicer M73 output. Home the axes before starting a job because thumbnail rendering invokes the configured pause script.

## Commit & Pull Request Guidelines

History contains short imperative subjects, including `fix: improve html and css naming pattern`; no strict convention is established. Write focused commits with descriptive subjects.

PRs should explain the behavior change, link relevant issues, and report validation with OctoPrint and firmware versions. Include screenshots for settings or popup changes and reproduction steps for printer protocol fixes.
