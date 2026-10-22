---
title: Repair broken JSON
description: Automatically fix invalid or broken JSON — trailing commas, missing brackets, unquoted keys, single quotes, truncated files — from the command line or the desktop app.
---

# Repair broken / invalid JSON

`repair` fixes the common mistakes that make a JSON file invalid, so it becomes parseable and
openable: trailing commas, missing or extra brackets, unquoted keys, single quotes, comments,
and truncated endings.

```bash
jsonreshape repair broken.json repaired.json --report fixes.csv
```

- `repaired.json` is the fixed file; the original is left untouched.
- `--report fixes.csv` writes a list of every change that was made, so nothing is fixed
  silently.

## In the desktop app

When a file fails to parse, the error panel offers a **Repair…** button. It shows what will
be changed before it writes the fixed file.

## Tips

- Run [`inspect`](../cli.md) or [`validate`](../cli.md) afterwards to confirm the file is now
  valid.
- For files that are valid but have the wrong shape, see
  [Flatten nested JSON](flatten-nested-json.md) and
  [Convert JSON](convert-json-to-csv-excel.md).
