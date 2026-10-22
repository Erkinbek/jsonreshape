---
title: Compare two JSON files
description: Compare two large JSON or JSON Lines files and get a record-level diff — matched by key, not by line — from the command line or the desktop app, at GB scale.
---

# Compare two JSON files (diff)

`diff` compares two JSON or JSON Lines files **record by record**, matching records by a key
rather than by line, so records that were merely reordered are not reported as changes.

```bash
jsonreshape diff old.json new.json --key id
```

- `--key id` matches records by their `id` field (use any field, or a dotted path).
- The output lists added, removed and changed records, and for changed records, which fields
  differ.
- `--exit-code` makes the command return a non-zero status when there are differences — handy
  in CI pipelines and scripts:

```bash
jsonreshape diff old.json new.json --key id --exit-code
```

## In the desktop app

Open one file, then use **Compare…** to pick the second. The differences are shown side by
side, grouped into added / removed / changed, with the changed fields highlighted.

![The compare view](../img/compare.png){ loading=lazy }

## Related

- [Repair broken JSON](repair-broken-json.md) first, if one of the files is invalid.
- [Split and merge JSON files](split-and-merge-json.md).
