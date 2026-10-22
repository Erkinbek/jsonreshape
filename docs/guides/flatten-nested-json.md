---
title: Flatten nested JSON
description: Flatten deeply nested JSON into a flat table or JSON Lines — expand nested arrays into rows and nested objects into dotted columns — at GB scale, from the CLI or the desktop structure builder.
---

# Flatten nested JSON into a table

Nested objects and arrays do not fit neatly into a CSV or a database table. `flatten` turns a
nested structure into flat records: nested objects become **dotted columns**
(`contact.address.city`) and a nested array can be expanded so that each element becomes its
own row.

```bash
jsonreshape flatten data.json flat.jsonl --children-key children
```

Then convert the flat result to any table format:

```bash
jsonreshape convert flat.jsonl flat.csv
```

## Expand a nested array into rows

When converting straight to a table, you can expand (explode) one array:

```bash
jsonreshape convert data.json table.xlsx --array-mode rows --explode orders
```

## Structure builder (desktop)

For full control, the **Structure builder** lets you drag source fields into the output shape
and see a live preview, then save the mapping and apply it to the whole file:

```bash
jsonreshape apply structure.mapping.json data.json new.json
```

![The structure builder](../img/structure-builder.png){ loading=lazy }

## Related

- [Convert JSON to CSV, Excel, SQLite, Parquet](convert-json-to-csv-excel.md)
- [Open a large JSON file](open-large-json-file.md)
