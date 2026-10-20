# Third-party notices

jsonreshape is licensed under the Apache License 2.0 (see `LICENSE`). It uses the following third-party
components, each under its own license. The Windows build (`dist\jsonreshape`, built by `scripts\build_exe.ps1`)
redistributes the runtime components below unchanged, as separate files.

## Python packages (runtime)

| Component | Version used | License | Notes |
|---|---|---|---|
| [orjson](https://github.com/ijl/orjson) | 3.13 | Apache-2.0 OR MIT | JSON encoding / decoding |
| [ijson](https://github.com/ICRAR/ijson) | 3.6 | BSD-3-Clause | streaming parser for huge records (yajl2_c backend) |
| [XlsxWriter](https://github.com/jmcnamara/XlsxWriter) | 3.2 | BSD-2-Clause | XLSX export |
| [jmespath](https://github.com/jmespath/jmespath.py) | 1.1 | MIT | JMESPath filters |
| [psutil](https://github.com/giampaolo/psutil) | 7.2 | BSD-3-Clause | memory and process information |
| [PySide6 / shiboken6](https://wiki.qt.io/Qt_for_Python) (Qt 6) | 6.12 | LGPL-3.0 (Qt: LGPL-3.0 / GPL / commercial) | the graphical interface; linked dynamically, the Qt libraries stay separate files and can be replaced |

Python itself: [PSF License](https://docs.python.org/3/license.html).

## Rust crates (the fast core, `rust/`)

| Crate | License |
|---|---|
| [pyo3](https://github.com/PyO3/pyo3) | Apache-2.0 OR MIT |
| [memchr](https://github.com/BurntSushi/memchr) | Unlicense OR MIT |
| [aho-corasick](https://github.com/BurntSushi/aho-corasick) | Unlicense OR MIT |

## Build and development tools (not redistributed)

| Tool | License |
|---|---|
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0-or-later with the bootloader exception (the produced executables are not affected) |
| [maturin](https://github.com/PyO3/maturin) | Apache-2.0 OR MIT |
| pytest, pytest-qt, pytest-timeout | MIT |
| hypothesis | MPL-2.0 |
| openpyxl, jsonschema | MIT |

## Data

The JSON Schema test suite used by the tests (`tests/data/json-schema-test-suite/`, when present) is MIT-licensed
(json-schema-org/JSON-Schema-Test-Suite). The synthetic benchmark files (`bench/make_synthetic.py`) are generated
and contain no real personal data.
