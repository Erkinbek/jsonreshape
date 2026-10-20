# Contributing to jsonreshape

Thank you for your interest. Bug reports, feature requests, translations and pull requests are welcome.
By contributing you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).

## Reporting a bug

Open an [issue](https://github.com/Erkinbek/jsonreshape/issues) with the bug report template. Please include:

* the version (`jsonreshape --version`, or Help → About in the GUI) and whether you run the `.exe` or from Python;
* Windows / Python versions and the core (`rust` or `python`; shown in Help → Environment);
* the smallest file that shows the problem (or its shape: format, size, record count — never upload private data);
* the log: Tools → Logs folder (`%LOCALAPPDATA%\jsonreshape\logs\jsonreshape.log`). The log never contains file
  contents, only paths, sizes and errors.

## Development setup

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -U pip maturin
.venv\Scripts\maturin develop --release          # Rust core (needs Rust stable + MSVC Build Tools)
.venv\Scripts\python -m pip install -e ".[dev,gui]"
```

Without Rust the pure-Python core is used (`JSONRESHAPE_BACKEND=python`): everything works, slower.

## Running the tests

```powershell
.venv\Scripts\python -m pytest -q                           # everything (~45 min with the Rust core)
.venv\Scripts\python -m pytest -q -m "not gui and not bench"  # the fast part (~20 min)
.venv\Scripts\python -m pytest -q tests/test_writer.py      # one module
```

* GUI tests run offscreen (`QT_QPA_PLATFORM=offscreen` is set by the test configuration).
* `-m bench` runs the 2 GB benchmarks (hours; needs `bench/make_synthetic.py` data). `-m packaging` needs a built
  `dist\jsonreshape`.
* Every behaviour change needs a test; a bug fix needs a regression test that fails before the fix.

## Code rules (see `docs/ARCHITECTURE.md` §0)

* The original file is never modified; every output is written to a temporary file and renamed atomically.
* No file is ever loaded whole into memory: work on byte ranges of the index, stream everything else.
* Numbers are exact: JSON number text is never re-serialized through `float`.
* The program is offline: no network access, no telemetry, no update checks.
* Every user-visible text goes through `jsonreshape.i18n.t()` with both `uz` and `en` entries (a test enforces it);
  texts of a module live in that module's section of `i18n.py`.
* The Rust core and the pure-Python core must give identical results (`tests/test_core*.py` compare them).
* Code, comments and docstrings are English; module docstrings describe the module's behaviour and are kept current.

## Pull requests

* One logical change per pull request; describe what changed and why, and name the tests you added.
* Run the relevant tests and `tests/test_static.py` (static rules: no `eval`, no direct ijson use outside the
  protected modules, no hard-coded colours outside `theme.py`).
* Update `README.md` **and** `README.uz.md` when behaviour or options change (both pages share the same sections;
  `tests/test_docs.py` checks them), and `docs/ARCHITECTURE.md` when the design changes.
* Add a line to `CHANGELOG.md` under "Unreleased".

## Translations

The UI texts are in `jsonreshape/i18n.py` (`uz` primary, `en` fallback). A new language is a new key in every
entry plus its code in `SUPPORTED_LANGUAGES`; missing entries fall back to English, so a translation can land
incrementally. The README exists in English and Uzbek (`README.uz.md`).
