---
title: O'rnatish
description: jsonreshape'ni Windows, Linux yoki macOS'ga o'rnating — pip orqali PyPI'dan, tayyor Windows ijro etiluvchi fayli sifatida yoki manbadan. Python 3.11+.
---

# O'rnatish

jsonreshape **Windows, Linux va macOS**'da ishlaydi va **Python 3.11+** talab qiladi. Tayyor
Windows ijro etiluvchi fayli hamma narsani o'z ichiga oladi va hech narsa o'rnatishni talab qilmaydi.

## PyPI'dan (pip)

```bash
pip install jsonreshape             # CLI + Python library
pip install "jsonreshape[gui]"      # add the PySide6 desktop window
pip install "jsonreshape[parquet]"  # add Parquet export (pyarrow)
pip install "jsonreshape[import]"   # add YAML import (pyyaml)
```

Keyin `jsonreshape --help` (CLI) yoki `jsonreshape-gui` (desktop oyna) ni ishga tushiring.

!!! note
    Har bir versiyaning Windows o'rnatuvchisi va portativ `.zip`, Linux va macOS yuklamalari hamda
    wheel'lari [GitHub Releases] sahifasida; PyPI'da jsonreshape **v0.1.1** dan boshlab bor.
    winget paketi hozircha yo'q.

## Tayyor yuklamalar (Windows, Linux, macOS)

[GitHub Releases] sahifasidan Windows o'rnatuvchisi yoki `.zip` ni, yoki Linux / macOS arxivini
yuklab oling, uni `SHA256SUMS.txt` bilan tekshiring va `jsonreshape-gui` (oyna) yoki `jsonreshape`
(buyruq satri) ni ishga tushiring — Python kerak emas.

[GitHub Releases]: https://github.com/Erkinbek/jsonreshape/releases

## Manbadan

```bash
git clone https://github.com/Erkinbek/jsonreshape
cd jsonreshape
python -m venv .venv
.venv\Scripts\python -m pip install -U pip maturin
.venv\Scripts\python -m pip install -e ".[gui]"   # drop [gui] for CLI only
```

maturin Rust yadrosini avtomatik quradi; Rust mavjud bo'lmasa, jsonreshape sof Python nusxasiga
(fallback) o'tadi (natija bir xil, sekinroq). Mustaqil `.exe` ni quyidagicha quring:

```bash
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

## Tekshirish

```bash
jsonreshape --version
jsonreshape inspect yourfile.json
```

Keyingi: [katta JSON faylni oching](guides/open-large-json-file.md).
