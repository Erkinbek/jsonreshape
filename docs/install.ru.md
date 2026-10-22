---
title: Установка
description: Установка jsonreshape в Windows, Linux или macOS — из PyPI с помощью pip, как готовый исполняемый файл для Windows или из исходного кода. Python 3.11+.
---

# Установка

jsonreshape работает в **Windows, Linux и macOS** и требует **Python 3.11+**. Готовый исполняемый
файл для Windows содержит всё необходимое, и ничего устанавливать не нужно.

## Из PyPI (pip)

```bash
pip install jsonreshape             # CLI + Python library
pip install "jsonreshape[gui]"      # add the PySide6 desktop window
pip install "jsonreshape[parquet]"  # add Parquet export (pyarrow)
pip install "jsonreshape[import]"   # add YAML import (pyyaml)
```

Затем выполните `jsonreshape --help` (CLI) или `jsonreshape-gui` (настольное окно).

!!! note
    Пакеты wheel для PyPI, готовые сборки для Windows и пакет winget выходят вместе с релизом
    **v0.1.0**. До этого устанавливайте из исходного кода (ниже).

## Готовый исполняемый файл для Windows

Скачайте `.zip` или установщик со страницы [GitHub Releases], проверьте его по
`SHA256SUMS.txt` и запустите `jsonreshape.exe` — Python не требуется.

[GitHub Releases]: https://github.com/Erkinbek/jsonreshape/releases

## Из исходного кода

```bash
git clone https://github.com/Erkinbek/jsonreshape
cd jsonreshape
python -m venv .venv
.venv\Scripts\python -m pip install -U pip maturin
.venv\Scripts\python -m pip install -e ".[gui]"   # drop [gui] for CLI only
```

maturin собирает ядро на Rust автоматически; если Rust недоступен, jsonreshape переходит к
реализации на чистом Python (те же результаты, но медленнее). Соберите автономный `.exe` командой:

```bash
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

## Проверка

```bash
jsonreshape --version
jsonreshape inspect yourfile.json
```

Далее: [открыть большой файл JSON](guides/open-large-json-file.md).
