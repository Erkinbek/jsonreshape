"""Command-line interface (AGENT_PROMPT §13, §11, §1.4; docs/ARCHITECTURE.md §10).

::

    jsonreshape [--lang uz|en] [--backend auto|rust|python] [-q] [-v|-vv] [--version] COMMAND ...

    inspect  INPUT [--sample N] [--json]
    search   INPUT TEXT [--keys|--values] [--regex|--number] [--case-sensitive] [--whole-word]
             [--translit] [--path P] [--limit N] [--count] [--export FILE.csv|FILE.json] [--max-hits N]
    filter   INPUT OUTPUT (--where EXPR | --jmespath EXPR | --preset NAME) [--save-preset NAME]
             [--select SPEC] [output options]
    sort     INPUT OUTPUT --by FIELD [--by -FIELD ...] [--nulls first|last]
             [--collation uz|simple|unicode] [--no-natural] [--case-sensitive] [--memory-limit SIZE]
             [--select SPEC] [output options]
    convert  INPUT OUTPUT [--where EXPR | --jmespath EXPR | --preset NAME] [--save-preset NAME]
             [--select SPEC] [--sort-by FIELD ...] [sort options] [output options]
    convert  INPUT OUTPUT --node LOCATION [--jsonl] [--compact | --indent 2|4|tab]
    patch    INPUT PATCH (OUTPUT | --in-place [--no-backup])
    apply    MAPPING INPUT OUTPUT [--jsonl] [--compact | --indent 2|4|tab] [--skip-huge] [--workers N]
    flatten  INPUT OUTPUT --children-key KEY [--depth-field NAME] [--mode nested|records]
             [--sample N | --full-scan] [--jsonl] [--compact | --indent 2|4|tab] [--skip-huge] [--workers N]
    stats    INPUT [--field F ...] [--top N] [--exact] [--sample N] [--memory-limit SIZE] [--json]
             [record scope]
    dupes    INPUT --key KEY [--keep first|last|none] [--top N] [--examples N] [--memory-limit SIZE]
             [-o FILE [output options]] [--json] [record scope]
    schema   INPUT [OUTPUT | -o OUTPUT] [--sample N | --full] [--strict] [--record | --document]
             [--title TEXT] [--compact | --indent 2|4|tab]
    validate INPUT --schema SCHEMA [--scope auto|record|document] [--formats] [--max-issues N]
             [--max-per-record N] [--export FILE.jsonl|FILE.json|FILE.csv] [--json] [record scope]
    split    INPUT (--parts N | --records N | --size SIZE) [--balance bytes|records] [-o DIR]
             [--name PATTERN] [--force] [--remove-stale] [--drop-broken] [--no-wrapper] [output options]
             [record scope]
    merge    INPUT... -o OUTPUT [--dedupe-by KEY [--keep first|last]] [--memory-limit SIZE]
             [--to json|jsonl | --jsonl] [--compact | --indent 2|4|tab] [--sort-keys [alpha|codepoint]]
             [--no-wrapper]
    extract  INPUT [OUTPUT | -o OUTPUT] [--path RECORD_PATH | --node LOCATION | --gather PATTERN]
             [output options] [record scope]
    repair   INPUT [OUTPUT | -o OUTPUT] [--check] [--nan null|string] [--jsonl] [--allow-partial]
             [--report FILE.csv|FILE.json|FILE.jsonl] [--json] [--format F] [--record-path P]
    diff     A B [--key KEY | --by-position | --align] [--array-key [PATH=]FIELD ...]
             [--numbers value|exact|text] [--ignore-order] [--max-changes N] [--limit N] [--summary]
             [--json] [-o FILE.json|FILE.jsonl|FILE.csv] [--exit-code] [--memory-limit SIZE]
    presets  [--delete NAME] [--json]
    import   INPUT [OUTPUT | -o OUTPUT] [--from csv|yaml|xml] [--to json|jsonl | --jsonl] [--compact | --indent N]
             [--types auto|string] [--empty null|string] [--delimiter D] [--no-header] [--no-nest]
             [--encoding ENC] [--skip-bad] [--record-tag TAG | --record-path a/b/c] [--json]

    input options (every command but presets, repair and import): --record-path P,
        --format auto|array|jsonl|brace_list|wrapped|keyed|single|stream, --allow-partial (not split),
        --no-cache
    standard input: INPUT may be "-" (one of the INPUTs of merge / diff too): it is spooled to a
        temporary file first (see "Standard input" under "Decisions"); outputs are always files
    output options: --to json|jsonl|csv|xlsx|sqlite|parquet (default: from the OUTPUT extension, see
        writer.format_for_path), --jsonl (= --to jsonl), --compact, --indent 2|4|tab,
        --sort-keys [alpha|codepoint], --no-wrapper, --as-keyed, --columns a,b.c,
        --array-mode join|rows, --explode PATH, --sep ";", --delimiter "," (also "tab"), --no-bom,
        --xlsx-split sheets|files, --parquet-compression zstd|snappy|gzip|none
    record scope: [--where EXPR | --jmespath EXPR | --preset NAME] [--save-preset NAME] [--select SPEC]

The global options may also be given after the command (``jsonreshape inspect a.json --lang en``).
:func:`main` is the entry point of ``python -m jsonreshape`` and of the ``jsonreshape`` console
script; it returns the exit code:

* 0 -- success;
* 1 -- user error: bad arguments (with the usage line), an invalid filter/search expression,
  sort field or path (printed with a caret line under the error position; a JMESPath
  syntax error gets a JMESPath example as its hint; a record path with a syntax error gets
  record-path examples), a missing input file, a record path that does not exist or does
  not lead to an array/object (with the record arrays found at the start of the file --
  ``--allow-partial`` never turns it into an empty success), a record path on an input
  detected as JSON Lines or as several JSON values (see "Decisions"), OUTPUT equal to
  INPUT, a read-only output file, an unwritable output folder, an unknown filter preset, a
  filter that could not be evaluated on any record (the filter runner's
  :class:`~jsonreshape.errors.UserInputError`, with its message and hint), file-system
  errors (see "OS errors" below), ...;
* 2 -- JSON error in the input: message, the text around the error with a caret, and a hint:
  ``--allow-partial`` (work with the records before the error), or ``--format jsonl`` /
  ``--format stream`` when the error is a second JSON value after a complete first one (or
  the reader's ``format_hint`` says so); also an input that is not usable at all (empty
  file, damaged gzip archive, a forced format that does not match) and an input that
  another program changed while the command read it
  (:class:`~jsonreshape.errors.FileChangedError`: nothing is saved, run it again);
* 3 -- cancelled (Ctrl+C or Ctrl+Break, or an injected :class:`~jsonreshape.tasks.CancelToken`);
* 4 -- internal error: a short localized message and the log file path; the traceback is
  written to the log (and to stderr with ``-vv``);
* 5 -- the data does not match (docs/ARCHITECTURE.md §14.0): ``validate`` found issues or could
  not validate every record (not shown to match), ``diff --exit-code`` found differences.
  ``repair --check`` uses 2 for "the file has problems" (its JSON-error meaning), ``repair`` 2 for
  an unrepairable file.

Start-up order
==============
1. stdout/stderr are reconfigured to UTF-8 when they are not a console (redirected output
   keeps Uzbek/Cyrillic text; lone surrogates become ``\\udXXX`` text instead of crashing);
   file logging starts (:func:`jsonreshape.log.setup_logging`).
2. The language is chosen: ``--lang`` > ``JSONRESHAPE_LANG`` > ``settings.language`` >
   ``uz`` (``--lang`` is looked up in the raw arguments first, because the parser itself
   is localized).
3. The arguments are parsed. Every argparse text is localized: our help texts come from
   :func:`jsonreshape.i18n.t`, and argparse's own phrases ("usage:", "error:", "options", "the
   following arguments are required", "invalid choice", ...) are translated by routing
   argparse's gettext hooks (``argparse._`` / ``argparse.ngettext``) through
   :data:`ARGPARSE_MESSAGES` while the parser is built and used (:func:`localized_argparse`).
   Usage errors exit with 1 (argparse would use 2, which is our "JSON error" code).
4. ``JSONRESHAPE_BACKEND`` is set from ``--backend``; only then are the modules that load
   :mod:`jsonreshape.core` imported (every such import is inside a function), so
   ``--backend`` really selects the core. When the core was already loaded by the calling
   process with another backend (in-process use, tests), a warning says so: the backend of
   a running process cannot change.
5. ``-v`` / ``-vv`` add console logging (INFO / DEBUG) on stderr; the uncaught-exception
   hooks are installed; :func:`jsonreshape.env.check_environment` runs once (its warnings,
   e.g. a missing ``yajl2_c`` ijson backend, go to the log and, with ``-v``, to stderr).
6. SIGINT (Ctrl+C) and, on Windows, SIGBREAK (Ctrl+Break) set the CancelToken shared by every
   operation: the running operation stops at its next check (``errors.Cancelled``), nothing
   partial is left behind (indexes, record sets and exports are written atomically) and
   the exit code is 3. A second Ctrl+C raises ``KeyboardInterrupt`` at once (still exit 3).

Output
======
* Results go to stdout, status lines (summaries, warnings, errors) to stderr. ``-q``
  suppresses the progress line and the summaries; warnings and errors are always shown.
* Progress: only when stderr is a terminal and ``-q`` is not given -- one line refreshed in
  place with the stage, percent, done / total bytes, rate and remaining time, cleared when
  the stage ends. The stage label is the operation's own ``Progress.info().message`` when it
  sets one (the progress of a stage starts with the CLI's label -- preparing, indexing,
  filtering, sorting, writing, searching, schema -- as its message, so an operation that
  announces a phase replaces it: "merging the sorted parts" of a sort, "finding the
  columns" / "exporting" / "finishing" of the writer, waiting for another process's index);
  a trailing ``…`` or ``:`` of that message is dropped, and a label too long for the
  terminal is shortened so that the numbers stay visible.
* Numbers in summaries are grouped with spaces in Uzbek (``12 345``) and commas in English.
  English texts with a count use their singular form for 1 (``1 hit in 1 record``,
  ``1 / 1 record -> ...``, ``(1 byte)``): the key ``<key>.one`` is used when it exists
  (:func:`_tn`; argparse's own plural phrases likewise); Uzbek keeps the noun after a
  number in the singular anyway (``3 ta yozuv``).
* Document warnings (``doc.info.warnings``): those known when the work starts are printed
  then (``inspect``: in the report); those the document gains while the command runs (e.g.
  a cache-folder fallback during indexing) and the :data:`END_WARNINGS` -- data appended to
  the input while it was read, of which only the snapshot taken at open is used: never
  shown earlier, as its size keeps changing -- are printed once after the command's own
  output (also after an error), with their latest values. A warning is identified by its
  key and ``path`` (added again, e.g. with a newer byte count, it appears once) and is
  never repeated; one the document had at open is not printed at the end when the command
  failed before showing it. Then the memory check (see "Decisions").
* ``search`` prints hits while they are found (``on_hits``), at most ``--limit``:
  ``#record  path  [kalit|qiymat]  snippet`` with the matched parts in bold yellow when
  stdout is a terminal and ``NO_COLOR`` is not set, otherwise between ``«`` and ``»``;
  control characters in keys and texts are shown escaped (``\\n``, ``\\u0007``), so one hit
  is always one line.
* Broken JSON Lines lines are described in line-oriented words: the keys
  ``json.line_error.<code>`` / ``json.line_expected.<code>`` replace ``json.error.<code>`` /
  ``json.expected.<code>`` where those speak of the end of the *file* (``unexpected_eof``:
  "the line ended unexpectedly"; ``end``: "end of the line was expected").

Decisions (documented deviations / interpretations)
====================================================
* ``--max-hits N`` caps the hits the search collects for listing (default 100000) and
  ``--limit N`` the hits printed (default 50; 0 = none): at most ``min(limit, max_hits)``
  hits are printed. Counting always covers every hit. ``--export`` writes every hit
  (``search.iter_hits`` + ``search.export_hits``, bounded memory) in the same single pass
  that prints the first ``--limit`` hits; its "mode" in the summary is derived the same way
  as :func:`jsonreshape.search.search` decides it (byte prefilter possible for the query and
  the file's string flags). The "only the first N hits are shown" note advises ``--limit``
  (or ``--max-hits`` when that was the bound) and ``--export`` only when no export was given.
* ``search`` without ``--export``/``--count`` does not build the index first (AGENT_PROMPT
  §6.2/§12 "results appear at once"): :func:`jsonreshape.search.search` starts the index in
  the background and follows it, so the first hits are printed while a fresh multi-GB file
  is still being indexed. Warnings about the index (broken lines, a JSON error with
  ``--allow-partial``) and a JSON error without ``--allow-partial`` (exit 2) are reported
  when it is complete, after the hits of the records before it. With ``--export`` or
  ``--count`` (and when a complete cached index exists) the index comes first, as for every
  other command.
* ``--count`` prints one line with the counts to stdout (plain digits, for scripts).
* ``inspect`` of a file with a JSON error prints the whole report (including the error with
  context) and exits with 2 unless ``--allow-partial`` is given; the schema table needs
  ``--allow-partial`` then (it describes the records before the error). For a wrapped
  document the report shows the outer wrapper (AGENT_PROMPT §3.4 "Tashqi o'rama alohida
  ko'rsatiladi"): its members with short compact-JSON values, the record container as a
  placeholder; ``--json`` adds a ``wrapper`` object (``bytes_before``, ``bytes_after``,
  ``members``: ``key``, ``records``, ``json``, ``truncated``; ``members`` is ``null`` when a
  part is larger than :data:`WRAPPER_READ_MAX` or cannot be parsed).
* A record path that matches nothing (``target_not_found``) or a scalar
  (``target_not_container``) is not a JSON error of the file: exit 1 with the record arrays
  found in the head (``doc.info.candidates``; the closest one as the example), also with
  ``--allow-partial``.
* A record path only works inside a single JSON document. With ``--format auto`` and
  ``--record-path`` the input is first detected without the record path
  (:func:`jsonreshape.reader.detect_format`: the head only; a gzip/UTF-16 input is prepared
  once, Document.open reuses that copy); JSON Lines or several JSON values one after
  another (``jsonl`` / ``stream``) are refused before any work -- exit 1 with
  ``cli.error.record_path_values`` and its hint (remove ``--record-path``, or force
  ``--format wrapped`` for a single document with a misleading name), as a forced
  ``--format jsonl --record-path`` is. Several values found only while reading (a first
  value larger than the detection head) remain a JSON error (exit 2) with the hint to
  remove ``--record-path``.
* A syntax error in ``--record-path`` gets record-path examples as its hint
  (``cli.hint.record_path_syntax``: ``data``, ``result.items``, ``/data/0``, ``data[0]``)
  instead of the general path examples, whose ``#120`` is a record number (a Ctrl+G
  location), not a path; any other :class:`~jsonreshape.errors.PathSyntaxError` that reaches
  the command line (``--path``, ``--by`` / ``--sort-by``) gets the general examples without
  it (``cli.hint.path_syntax``).
* Memory (AGENT_PROMPT §11 "xotira chegarasi"): at the end of every command the peak
  memory of the process (Windows: the peak working set, via psutil; elsewhere the maximum
  resident set size) is compared with ``Settings.memory_limit_bytes``. Above it, one
  warning gives both numbers and what to lower (``--memory-limit`` for a sort, the "Large
  node threshold" setting) or to raise; the command is not aborted. Only a peak reached
  during the command counts: the process's peak must have grown since :func:`main` started
  (an in-process caller's earlier peak is not this command's).
* File changes (AGENT_PROMPT §1.7): ``doc.ensure_unchanged()`` runs after indexing, before
  the inspect report, after a search (inside the hit stream of ``--export``, so the export
  is never committed) and before an export starts (the writer checks again right before its
  atomic rename). Any other error raised while the input is open becomes the
  :class:`~jsonreshape.errors.FileChangedError` when the file changed meanwhile (the real
  cause, not the garbled data it produced). Exit code 2 with ``error.file_changed`` and its
  hint.
* ``--memory-limit SIZE`` (sort keys in memory): a number without a unit means megabytes
  (``512`` = 512 MB; ``parse_size`` units are binary); below :data:`MIN_MEMORY_LIMIT` it is
  raised to that minimum with a warning (a tiny limit wrote one temporary run file per few
  keys). The sort memory never exceeds half of ``Settings.memory_limit_bytes`` (the overall
  memory budget, "Xotira chegarasi"): a larger ``--memory-limit`` is lowered with a warning;
  without the option ``min(Settings.sort_memory_bytes, memory_limit_bytes // 2)`` is passed.
* ``--sort-keys [alpha|codepoint]``: ``alpha`` (the default when the flag is given) sorts the
  keys alphabetically (Uzbek collation, natural, case-insensitive), ``codepoint`` by code
  point; ``--sort-keys`` followed by any other argument that is not an option (e.g. INPUT)
  means ``alpha`` and does not swallow that argument.
* Filter presets (AGENT_PROMPT §6.3 "Filtrlar nom bilan saqlanadi"): ``--save-preset NAME``
  stores the filter of ``filter``/``convert`` (after it compiled, before the work starts),
  ``--preset NAME`` uses one, ``presets`` lists them and ``presets --delete NAME`` deletes
  one (:class:`jsonreshape.query.FilterPresets`).
* OS errors: common ``winerror``/``errno`` values (2, 3: not found; 5: access denied, or the
  read-only attribute; 32, 33: in use by another program; 39, 112: disk full; 80, 183:
  already exists) get a localized message and hint -- the ``fileio.error.*`` texts of
  :func:`jsonreshape.fileio.os_error` for "in use", "read-only" and "disk full", ``cli.error.os.*``
  for the others; the raw OS text (often in the Windows display language) is only a
  detail line. An existing read-only OUTPUT is refused before any work
  (``fileio.error.read_only``).
* ``--by -id``: argparse would read ``-id`` as an option, so a value starting with a single
  ``-`` right after ``--by`` / ``--sort-by`` (and ``--children-key`` / ``--depth-field``, whose
  value is a key) is joined to it (``--by=-id``) before parsing.
* ``convert`` also accepts the sort options of ``sort`` for its ``--sort-by`` keys.
* ``patch`` (docs/ARCHITECTURE.md §12.8): the JSON Patch is applied through an edit session whose layer lives
  in a temporary folder of the work folder (deleted at the end); the result is written with
  :func:`jsonreshape.saver.save` in INPUT's kind and encoding (OUTPUT: Save As, compressed iff it ends in
  ``.gz``; ``--in-place``: atomic replacement with ``INPUT.bak`` unless ``--no-backup``; a patch that changes
  nothing leaves an in-place INPUT untouched). Exit 1 for every patch problem (unreadable patch, invalid
  operation, a failed ``test``, a path that does not exist ...), a file with unsaved changes of the program
  (``edits.error.pending``) or one being edited elsewhere (``edits.error.locked``); 2 for a JSON error in
  INPUT. ``--allow-partial`` also allows dropping the broken lines of a JSON Lines INPUT when it is saved.
  Exactly one of OUTPUT / ``--in-place`` must be given (exit 1); ``--no-backup`` needs ``--in-place``.
* ``apply`` (docs/ARCHITECTURE.md §13.4): the mapping file is loaded and validated before INPUT is opened
  (:func:`jsonreshape.mapping.load_mapping`; every ``mapping.error.*`` names the JSON pointer of the bad node;
  exit 1). OUTPUT may be neither INPUT nor the mapping file. The result is JSON or JSON Lines only: ``--jsonl``
  or an OUTPUT named ``.jsonl`` / ``.ndjson`` writes JSON Lines; an OUTPUT named like another export format
  (``.csv``, ``.xlsx``, ``.sqlite``) or ``.gz`` is refused (exit 1: the engine writes plain JSON; convert the
  result afterwards); the indent defaults to the "export indent" setting; ``--compact`` / ``--indent`` with
  JSON Lines are ignored with a warning. A mapping made for an array inside an object opens INPUT with the
  record path stored in it (``input.record_path``) unless ``--record-path`` is given, ``--format`` names
  another kind, or INPUT is detected as JSON Lines / several values (a record path is refused there); a stored
  record path INPUT does not have is exit 1 (``cli.error.mapping_record_path``, with the arrays found). A
  mapping made for another kind of document is exit 1 (``engine.error.kind_mismatch``; when the mapping was
  made from a keyed, single-value or wrapped file the hint names the ``--format`` / ``--record-path`` that
  reads INPUT the same way). Printed: the engine's warnings (except its partial / broken-lines ones, given
  after indexing already) and a summary "records -> items -> OUTPUT (size, time, input MB/s; passes,
  processes)". ``--skip-huge`` and ``--workers N`` are :class:`jsonreshape.engine.ApplyOptions` fields.
* ``flatten`` (docs/ARCHITECTURE.md §13.4): the default tree is inferred from ``--sample N`` records
  (:func:`jsonreshape.schema.sample_positions`; default ``Settings.sample_size``) or from every record
  (``--full-scan``), read from the pinned document one block at a time and read again for the flatten's
  re-inference (never kept in memory; huge records are left out of the inference -- the apply then stops at
  them or skips them with ``--skip-huge``). Then one recursive flatten (``Designer.plan_flatten`` +
  ``flatten(plan=)``) and :func:`jsonreshape.engine.apply`. ``--mode nested`` (default) flattens the array field
  KEY of the records: the record element of an array / JSON Lines / stream / brace-list document, of the
  record array of a wrapped one, the member value of a keyed one, the value (or, for an array, its element) of
  a single one. ``--mode records`` flattens the record array itself; a keyed document or a single value that
  is not an array has none (exit 1). KEY must be a record field (else exit 1 naming the record's array fields)
  that is an array of objects (records) whose items hold a nested KEY array of objects (nested); otherwise
  exit 1 "KEY is not a recursive array of objects here" with the reason, and for a sample the advice to try
  ``--full-scan``; every flattened element must use KEY (``FlattenPlan.entries[i][1] == KEY``). Deviation
  (``--mode records``): the reference ``findRec`` picks the FIRST recursive array of the record (or one named
  like the record array), so the record's other recursive arrays are removed from the tree before planning;
  the flatten re-infers every field of the record from all levels, so the result only differs in the key
  that is flattened. ``--depth-field NAME`` renames the computed level node (default ``_depth``): a name the
  data already uses gets ``NAME_2`` (``_3`` ..., as every rename) with a warning, the default included. An
  input without records is written unchanged with a warning. ``--skip-huge`` / ``--workers`` as for ``apply``.
* Output options that do not apply to the chosen format (``--columns`` for JSON, ``--indent``
  for CSV, ...) are ignored with a warning -- except ``--as-keyed`` (rebuild one keyed object
  from ``{"key": value}`` records, ``writer.ExportOptions.as_keyed``): it changes what the
  result is, so with a non-JSON output it is a user error (exit 1, ``cli.error.as_keyed_format``).
  A record that is not a one-member object is a user error too (exit 1,
  ``writer.error.not_single_member``; nothing is saved).
* Broken pipes on stdout (``jsonreshape search ... | head``) end the command quietly (exit 0).
* Standard input (``curl ... | jsonreshape convert - out.jsonl``; :mod:`jsonreshape.stdin_input`):
  an INPUT given as ``-`` -- and only ``-`` -- is standard input, for every command with one INPUT
  (``inspect`` ... ``import``, ``patch`` with an OUTPUT) and for at most one of the inputs of
  ``merge`` / ``diff``. Every reader needs a seekable file (the index stores byte ranges), so before
  the command starts :func:`_stdin_inputs` copies the binary stream (``sys.stdin.buffer``: no
  newline translation on Windows) in 1 MiB pieces to a spool file ``stdin-<pid>-<random>.json``
  in the work folder (``<JSONRESHAPE_HOME>/tmp``, or the ``tmp`` folder of a configured cache
  folder like the other work files; mode 0600 where the OS has modes), with a progress line of
  the bytes read so far (the total is unknown), cancellation (Ctrl+C removes the file) and a
  free-space check (every 256 MiB the folder must have the bytes read so far + 64 MiB free, else
  ``fileio.error.disk_full``); then the ``-`` in the arguments is replaced by that path, so every
  later step works on a named file: ``_check_input``, the same-file check of ``_check_output``
  (an output may not be the spool file), ``Document.open`` with ``--format`` / ``--record-path``
  (gzip on the pipe is recognized by its magic bytes and converted by the normal path), the
  messages that name the input (they show the spool path). One info line says how many bytes
  were read and where (``cli.stdin.summary``); the spool file is removed when the command ends,
  also after an error. Refused before anything is read (exit 1): ``-`` when standard input is a
  terminal or missing (``cli.error.stdin_tty``), two ``-`` (``cli.error.stdin_twice``), ``-`` as
  any output (``cli.error.stdout_output``: outputs stay files), ``-`` for a file that is not an
  INPUT -- PATCH, MAPPING, ``--schema`` (``cli.error.stdin_unsupported``), ``patch --in-place -``
  (``cli.error.stdin_in_place``), ``split -`` without ``-o DIR`` (the parts would land in the
  work folder: ``cli.error.stdin_split_dir``) and ``import -`` without ``--from`` (no file name
  tells the type: ``cli.error.stdin_import_format``). The index of a spooled input is written to
  the cache like any other and left to the LRU eviction (the spool path is new every time, so it
  is never reused).
* The examples of ``--help`` work unchanged in cmd.exe, Windows PowerShell 5.1, PowerShell 7
  and POSIX shells: expressions in double quotes, texts inside them in single quotes, no
  backtick inside double quotes (PowerShell's escape character), no double quote inside a
  single-quoted argument (Windows PowerShell 5.1 drops it).

Stage 5 (docs/ARCHITECTURE.md §14.8; the tools' own module docstrings are authoritative)
========================================================================================
* Record scope (``--where`` / ``--jmespath`` / ``--preset``, ``--select SPEC``): the selection
  :func:`jsonreshape.filetools.parse_select` reads -- 0-based ``N``, ``A:B`` (``A`` .. ``B-1``), ``A:``,
  ``:B``, comma lists, kept in the given order -- is taken first, then the filter runs over it (source
  order), then ``sort`` / ``convert --sort-by`` sort the result. Its syntax is checked before INPUT is
  read (positions beyond the end only once it is indexed). ``stats``, ``dupes``, ``validate``,
  ``split`` and ``extract`` take the same scope (the task's ``--filter EXPR`` of ``stats`` / ``split``
  is ``--where EXPR``: one spelling for every command).
* ``--no-wrapper`` applies to every output format (the writer honours ``keep_wrapper`` for JSON
  Lines, CSV, XLSX and SQLite too; it also silences ``writer.note.wrapper_not_written``), so it is
  never "ignored" with a warning any more.
* ``convert --node LOCATION`` (``#5.orders``, ``/data/5/orders``, ``data[5].orders``) writes one node
  (:func:`jsonreshape.filetools.extract`, JSON or JSON Lines); it cannot be combined with a filter,
  ``--select`` or ``--sort-by`` (exit 1). Output options a node ignores (``--sort-keys``, the CSV
  options ...) are named in a warning.
* Commands writing ONE named file take it as the positional OUTPUT (``filter`` ... ``flatten``, and
  ``schema`` / ``extract`` / ``repair``, where ``-o OUTPUT`` is accepted as well -- giving both is exit
  1) or as ``-o FILE`` (``merge``, ``dupes``, ``diff``; ``split -o DIR`` is the folder of the parts).
  Every output (also ``--report``, ``--export``, ``-o``) is checked before any work: never an input
  (any spelling: case, relative path, a hard link), never the schema file, never a folder or a
  read-only file, in a folder that exists, and never a file with unsaved edits in this program
  (fix round L1: a tab or another instance edits it, or its edits are kept for recovery --
  ``filetools.error.part_open_edited``, exit 1; the writer refuses it again right before its commit).
* ``stats``: without ``--field`` the fields are :func:`jsonreshape.schema.default_columns` of a sample
  schema of the scope (``Settings.sample_size`` records). Text: :func:`jsonreshape.stats.format_stats`;
  ``--json``: ``{"file", "records", "scope", "elapsed", "fields": [FieldStats.to_dict()]}`` (every JSON
  output of the stage-5 commands is written with :func:`jsonreshape.codec.dumps`: exact numbers).
* Commands with several inputs (``merge``, ``diff``) open each with the input options and name it
  before its warnings (``FILE: ...``) and before its JSON error (``Fayl: FILE`` / ``File: FILE``); the
  tools' own copies of the CLI's partial / broken-line notes (``diff.warn.*``, ``jschema.warn.*``)
  and validate's "only the first N issues were kept" are not repeated.
* ``--memory-limit SIZE`` of ``stats`` / ``dupes`` / ``merge`` / ``diff`` follows the sort rule
  (a plain number means MB; at least :data:`MIN_MEMORY_LIMIT`, at most half of
  ``Settings.memory_limit_bytes``, both with a warning); without it the tool's own default is used.
* ``dupes``: counts, the first ``--examples N`` groups (0-based record numbers) and, with ``--top N``,
  the largest groups; ``-o FILE`` exports the records marked as duplicates (``--keep first``: every
  record of a group but the first, ``last``: but the last, ``none``: every record of a group) with the
  writer and the output options. "Distinct values" = records with the key - group members + groups.
* ``schema``: a full scan by default (the GUI's default, and the only way ``required`` is exact: a
  schema from a sample may refuse records of its own file); ``--sample N`` is faster and warns;
  ``--full`` is accepted (the default). Without OUTPUT the schema goes to stdout. A ``.gz`` OUTPUT is
  compressed.
* ``validate``: the schema file is read and compiled before INPUT is opened (schema errors: exit 1).
  Issues are printed as ``#record [key] path: message (keyword)`` (``(hujjat)`` / ``(document)`` for
  issues of the document itself), at most ``--max-issues N`` (default 100; 0 = the summary only);
  ``--max-per-record 0`` = no limit. ``--export`` writes EVERY collected issue (JSON Lines, a JSON
  array or CSV with a BOM per the CSV settings) in the same pass (``validate(on_issues=)``, one
  atomic write; the task's ``iter_issues`` would read the file twice). Exit 5 when an issue was found.
  Records the validator could not check (``ValidationReport.skipped``: larger than its parse limit,
  unreadable by the decoder, nested too deeply -- each kind named in a warning with its first record)
  are "not shown to match": with no issue found the summary says so ("not confirmed: N checked without
  issues, but M could not be checked") instead of "valid", and the exit code is 5 as well (fix round
  L1: a script must not take an unchecked file for a valid one); with issues an extra line counts
  them. Exit 0 only when every record in scope was validated and no issue was found; ``--json``
  ``ok`` is that verdict (``skipped`` holds the count).
* ``split``: parts that already exist are refused unless ``--force`` (exit 1 naming them; the GUI asks
  the same question) -- the part names are generated, so an existing file of that name may be the
  user's own. The names are :func:`jsonreshape.filetools.part_names` (exactly the files ``split``
  writes, for every scope). A part with unsaved edits in this program (a tab or another instance edits
  it, or its edits are kept for recovery) is never replaced, not even with ``--force``: exit 1 with
  ``filetools.error.part_open_edited`` before anything is written. ``--remove-stale``
  (``SplitOptions.remove_stale``) deletes, in the same all-or-nothing commit, the files of the name
  pattern that are the rest of an earlier split (filetools decision 11: never the input, a read-only
  file or one with unsaved edits); each removed file is named on stderr (also with ``-q``: files were
  deleted). Without it, stale files are only named in a warning, with the hint to use
  ``--remove-stale``. ``--size`` is at least 1 MiB; ``--balance`` only applies to ``--parts`` (else a
  warning). Partial documents cannot be split: a JSON error is exit 2 with the advice to repair the
  file (``split`` has no ``--allow-partial``); JSON Lines broken lines need ``--drop-broken``. The
  written parts are listed on stdout, one path per line.
* ``merge``: ``merge INPUT... -o OUTPUT`` (AGENT_PROMPT §13; the task's ``merge OUT INPUT...`` would
  differ from the requirement). An INPUT with ``*``, ``?`` or ``[`` that is not an existing file is a
  pattern expanded here (cmd.exe and PowerShell pass it unexpanded), matches sorted naturally
  (``qism_2`` before ``qism_10``); a pattern without matches is exit 1; OUTPUT itself is never taken
  from a pattern (a warning says it was skipped), but an INPUT naming OUTPUT is exit 1. The output
  format is ``--to`` / ``--jsonl`` / the OUTPUT extension (JSON or JSON Lines only).
* ``extract``: ``--path RECORD_PATH`` writes the records of the container at that path without the
  wrapper (any export format; the same as ``--record-path``), ``--node LOCATION`` one node and
  ``--gather PATTERN`` the values of a record-relative pattern from every record (JSON / JSON Lines);
  without any of them the records of the document (its detected record array) are written.
* ``repair`` takes no index options and never opens INPUT as a document (it is broken). Exit codes
  (s5/map_repair_diff.md §3.7 + §14.0): 0 written (also "nothing to repair": an unchanged copy);
  ``--check``: 0 when nothing would change, else 2; an unrepairable file: 2 (nothing written; the
  report, the place with its context and the ``--allow-partial`` advice are shown); with
  ``--allow-partial`` the records before the problem are written and the exit code is 0 (the problem
  and truncation lines are shown; "no problems found" is never printed then); a failed output
  check (``repair.error.invalid_output``): 4. ``--report FILE`` writes every fix (CSV / JSON / JSON
  Lines by the extension), also for ``--check`` and for an unrepairable file.
* ``diff``: ``--key`` -> key mode, ``--by-position`` / ``--align`` the other modes, else auto.
  ``--ignore-order``: arrays compare as multisets and, without ``--key`` / ``--by-position`` /
  ``--align``, records are matched by their whole value (key ``$``: member order and number spelling do
  not matter, but array order does for that match -- the canonical form of :mod:`jsonreshape.dupes`; a
  changed record is one removed and one added record; give ``--key`` to pair records whose arrays only
  differ in order). Text output: ``~`` changed, ``+`` added, ``-`` removed, ``>`` moved, values as
  compact JSON cut to :data:`DIFF_VALUE_CHARS` characters, colours on a terminal only; at most
  ``--limit N`` records (default 100, 0 = all; the others are counted); ``--summary`` prints the counts
  only; ``--json`` prints JSON Lines (``record_dict`` per record, then ``{"summary": ...}``); ``-o FILE``
  writes every difference (``write_diff``, by the extension) in the same pass; the compared files and
  their data copies (gzip / UTF-16 inputs) are passed as ``sources=``, so such a target is refused
  before the diff runs (fix round L1). Exit 0, or with ``--exit-code`` 5 when the files differ.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import errno
import glob
import inspect
import json
import logging
import os
import platform
import re
import shutil
import signal
import stat
import struct
import sys
import threading
import time
import traceback
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import __version__
from .errors import (
    Cancelled,
    FileChangedError,
    FileFormatError,
    JsonReshapeError,
    JsonSyntaxError,
    PathSyntaxError,
    QuerySyntaxError,
    TooLargeError,
    UserInputError,
)
from .i18n import SUPPORTED_LANGUAGES, has, set_language, space_grouped, t
from .tasks import CancelToken, Progress, ProgressInfo, format_bytes, format_duration, parse_size

log = logging.getLogger(__name__)

__all__ = [
    "ARGPARSE_MESSAGES",
    "EXIT_CANCELLED",
    "EXIT_DIFFERENT",
    "EXIT_INTERNAL",
    "EXIT_JSON",
    "EXIT_OK",
    "EXIT_USER",
    "build_parser",
    "localized_argparse",
    "main",
]

EXIT_OK = 0
EXIT_USER = 1
EXIT_JSON = 2
EXIT_CANCELLED = 3
EXIT_INTERNAL = 4
EXIT_DIFFERENT = 5        # the data does not match: validate issues, diff --exit-code differences

PROG = "jsonreshape"
BACKENDS = ("auto", "rust", "python")
INPUT_FORMATS = ("auto", "array", "jsonl", "brace_list", "wrapped", "keyed", "single", "stream")  # reader.KINDS
OUTPUT_FORMATS = ("json", "jsonl", "csv", "xlsx", "sqlite", "parquet")                            # writer.FORMATS
PARQUET_COMPRESSIONS = ("zstd", "snappy", "gzip", "none")                              # writer.PARQUET_COMPRESSIONS
HIT_EXPORT_FORMATS = ("csv", "json")                                                           # search.EXPORT_FORMATS
SORT_KEY_ORDERS = ("alpha", "codepoint")                                       # writer.ExportOptions.sort_keys
DEFAULT_LIMIT = 50
DEFAULT_MAX_HITS = 100000
BROKEN_SHOWN = 5          # broken JSON Lines lines listed in warnings and in the inspect report
BROKEN_JSON_MAX = 100     # broken lines listed by inspect --json
CANDIDATES_SHOWN = 5      # record-path candidates listed in the inspect text report (--json: all)
DASH_VALUE_OPTIONS = ("--by", "--sort-by", "--children-key", "--depth-field")
DEFAULT_DEPTH_FIELD = "_depth"   # flatten: the computed level node (mapping.Designer.flatten)
#: engine report warnings the CLI gave already after indexing (_check_index)
ENGINE_SHOWN_WARNINGS = ("engine.warn.partial", "engine.warn.broken_lines")
#: mapping input kinds whose file can be read "the same way" with --format (cli.hint.mapping_kind)
MAPPING_FORMAT_KINDS = ("keyed", "single", "wrapped")
MiB = 1 << 20
MIN_MEMORY_LIMIT = 16 * MiB   # smallest sort memory (--memory-limit below it is raised to it)
WRAPPER_READ_MAX = MiB        # each part of a wrapper (before/after the records) read by inspect
WRAPPER_TEXT_CHARS = 100      # characters of a wrapper value in the inspect text report
WRAPPER_JSON_CHARS = 2000     # characters of a wrapper value in inspect --json
WRAPPER_MEMBERS_SHOWN = 10    # wrapper members listed in the inspect text report (--json: all)
MIN_PROGRESS_LABEL = 12       # a progress label is shortened to fit the terminal, not below this
#: JSON error codes of a record path that does not lead to an array/object (core constants)
RECORD_PATH_CODES = ("target_not_found", "target_not_container")
#: attribute of a JsonReshapeError carrying a hint the CLI chose instead of the key's own
CLI_HINT_ATTR = "cli_hint"
#: document warnings shown only at the end of a command, with their latest values: notes
#: about this run that keep changing while it runs (reader.APPENDED_WARNING: the input grew
#: after it was opened -- only the snapshot taken at open is used)
END_WARNINGS = ("reader.warn.appended",)
# -- stage 5 --------------------------------------------------------------------------------
GLOB_CHARS = "*?["                 # merge: an INPUT with these (and no such file) is a pattern
KEEP_CHOICES = ("first", "last", "none")    # dupes.KEEP_CHOICES
MERGE_KEEPS = ("first", "last")             # filetools.KEEPS
SPLIT_BALANCES = ("bytes", "records")       # filetools.BALANCES
SCHEMA_SCOPES = ("auto", "record", "document")
REPAIR_NANS = ("null", "string")
DIFF_NUMBERS = ("value", "exact", "text")
ISSUE_EXPORT_FORMATS = ("jsonl", "json", "csv")
DIFF_OUT_FORMATS = ("json", "jsonl", "csv")
REPAIR_REPORT_FORMATS = ("csv", "json", "jsonl")
MIN_SPLIT_SIZE = MiB              # split --size: at least 1 MiB (a CLI rule)
DEFAULT_ISSUES = 100              # validate --max-issues
DEFAULT_PER_RECORD = 10           # validate --max-per-record (jschema's default)
DEFAULT_EXAMPLES = 20             # dupes --examples (dupes.EXAMPLE_GROUPS)
DEFAULT_DIFF_LIMIT = 100          # diff --limit (records printed)
DIFF_VALUE_CHARS = 100            # diff text output: values cut to this many characters
POSITIONS_SHOWN = 20              # dupes: record numbers listed per group (dupes.EXAMPLE_POSITIONS)
NAMES_SHOWN = 5                   # file names listed in one message
LINE_KINDS = ("jsonl", "stream")  # kinds whose records are written as JSON Lines by default
#: warnings of the tools' reports the CLI has given already in its own words (after indexing: the JSON
#: error with --allow-partial and the broken lines; the "only the first N issues" note of validate)
VALIDATE_SHOWN_WARNINGS = ("jschema.warn.partial", "jschema.warn.broken_lines", "jschema.warn.issues_truncated")
DIFF_SHOWN_WARNINGS = ("diff.warn.partial", "diff.warn.broken_lines")
DIFF_MARKS = {"changed": "~", "added": "+", "removed": "-", "moved": ">"}
DIFF_COLORS = {"changed": "\x1b[33m", "added": "\x1b[32m", "removed": "\x1b[31m", "moved": "\x1b[36m"}
ARROW = "\N{RIGHTWARDS ARROW}"

HIGHLIGHT_ON = "\x1b[1;33m"     # bold yellow
HIGHLIGHT_OFF = "\x1b[0m"
MARK_OPEN = chr(0xAB)           # «
MARK_CLOSE = chr(0xBB)          # »
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"

# Characters shown escaped in one-line output (controls, line/paragraph separators, bidi
# controls, BOM, lone surrogates).
_CONTROL_RANGES = ((0x00, 0x1F), (0x7F, 0x9F), (0x200E, 0x200F), (0x2028, 0x202E), (0x2066, 0x2069),
                   (0xFEFF, 0xFEFF), (0xD800, 0xDFFF))
_CONTROL_RE = re.compile("[" + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "")
                                       for a, b in _CONTROL_RANGES) + "]")
_ESCAPE_NAMES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
_PLAIN_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


# ==================================================================================
# argparse localization
# ==================================================================================
#: argparse's own message ids (gettext msgids) -> i18n keys of the "cli" section.
ARGPARSE_MESSAGES: dict[str, str] = {
    "usage: ": "cli.argparse.usage",
    "%(heading)s:": "cli.argparse.heading",
    " (default: %(default)s)": "cli.argparse.default",
    "argument %(argument_name)s: %(message)s": "cli.argparse.argument",
    "show program's version number and exit": "cli.argparse.version",
    "unknown parser %(parser_name)r (choices: %(choices)s)": "cli.argparse.unknown_parser",
    "command '%(parser_name)s' is deprecated": "cli.argparse.deprecated_command",
    "can't open '%(filename)s': %(error)s": "cli.argparse.cant_open",
    "positional arguments": "cli.argparse.positionals",
    "options": "cli.argparse.options",
    "optional arguments": "cli.argparse.options",
    "show this help message and exit": "cli.argparse.help",
    "subcommands": "cli.argparse.subcommands",
    "unrecognized arguments: %s": "cli.argparse.unrecognized",
    "not allowed with argument %s": "cli.argparse.not_allowed",
    "ambiguous option: %(option)s could match %(matches)s": "cli.argparse.ambiguous",
    "ignored explicit argument %r": "cli.argparse.ignored_explicit",
    "option '%(option)s' is deprecated": "cli.argparse.deprecated_option",
    "argument '%(argument_name)s' is deprecated": "cli.argparse.deprecated_argument",
    "the following arguments are required: %s": "cli.argparse.required",
    "one of the arguments %s is required": "cli.argparse.one_required",
    "expected one argument": "cli.argparse.expected_one",
    "expected at most one argument": "cli.argparse.expected_at_most_one",
    "expected at least one argument": "cli.argparse.expected_at_least_one",
    "expected %s argument": "cli.argparse.expected_n",
    "expected %s arguments": "cli.argparse.expected_n",
    "unexpected option string: %s": "cli.argparse.unexpected_option",
    "invalid %(type)s value: %(value)r": "cli.argparse.invalid_value",
    "invalid choice: %(value)r (choose from %(choices)s)": "cli.argparse.invalid_choice",
    "invalid choice: %(value)r, maybe you meant %(closest)r? (choose from %(choices)s)":
        "cli.argparse.invalid_choice_suggest",
    "%(prog)s: error: %(message)s\n": "cli.argparse.error",
    "%(prog)s: warning: %(message)s\n": "cli.argparse.warning",
}


def _argparse_gettext(message: str) -> str:
    key = ARGPARSE_MESSAGES.get(message)
    return t(key) if key is not None else message


def _argparse_ngettext(singular: str, plural: str, n: int) -> str:
    key = ARGPARSE_MESSAGES.get(singular)
    if key is not None:
        return _tn(key, n)
    return singular if n == 1 else plural


@contextlib.contextmanager
def localized_argparse() -> Iterator[None]:
    """Route argparse's gettext hooks through :func:`t` while the block runs (building the
    parser, parsing, formatting help/usage), so its own phrases appear in the current
    language. The previous hooks are restored afterwards."""
    saved = {name: getattr(argparse, name) for name in ("_", "ngettext") if hasattr(argparse, name)}
    if "_" in saved:
        argparse._ = _argparse_gettext  # type: ignore[attr-defined]
    if "ngettext" in saved:
        argparse.ngettext = _argparse_ngettext  # type: ignore[attr-defined]
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(argparse, name, value)


class _UsageError(Exception):
    """A usage error found by argparse (reported with the usage line, exit code 1)."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _ParserExit(Exception):
    """argparse wanted to exit (``--help``): the status to return."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that never calls ``sys.exit`` (``main()`` returns exit codes).
    ``commands`` maps command names to their parsers (main parser only)."""

    commands: dict[str, argparse.ArgumentParser]

    def error(self, message: str):  # type: ignore[override]
        raise _UsageError(self, message)

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        if message:
            self._print_message(message, sys.stderr)
        raise _ParserExit(status)


def _parser_extra() -> dict[str, Any]:
    """Keyword arguments supported by this Python's argparse (3.14: plain help without
    colours -- our own output decides about colours -- and "maybe you meant" hints)."""
    extra: dict[str, Any] = {}
    try:
        params = inspect.signature(argparse.ArgumentParser.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return extra
    if "color" in params:
        extra["color"] = False
    if "suggest_on_error" in params:
        extra["suggest_on_error"] = True
    return extra


_PARSER_EXTRA = _parser_extra()


def _formatter(prog: str) -> argparse.HelpFormatter:
    try:
        columns = shutil.get_terminal_size((100, 24)).columns
    except (OSError, ValueError):  # pragma: no cover
        columns = 100
    width = max(60, min(120, columns) - 2)
    return argparse.RawDescriptionHelpFormatter(prog, max_help_position=30, width=width)


# -- argument value types -------------------------------------------------------------
def _int_value(text: str) -> int | None:
    s = text.strip()
    if s.isascii() and (s.isdigit() or (s[:1] in "+-" and s[1:].isdigit())):
        return int(s)
    return None


def _positive_int(text: str) -> int:
    n = _int_value(text)
    if n is None or n < 1:
        raise argparse.ArgumentTypeError(t("cli.argerr.positive_int", value=text))
    return n


def _non_negative_int(text: str) -> int:
    n = _int_value(text)
    if n is None or n < 0:
        raise argparse.ArgumentTypeError(t("cli.argerr.non_negative_int", value=text))
    return n


def _indent_value(text: str) -> int | str:
    s = text.strip().lower()
    if text == "\t" or s in ("tab", "\\t"):
        return "\t"
    n = _int_value(s)
    if n is None or not 0 <= n <= 16:
        raise argparse.ArgumentTypeError(t("cli.argerr.indent", value=text))
    return n


def _memory_size_value(text: str) -> int:
    """``--memory-limit``: a size in bytes. ``256MB``, ``1GB``, ``65536B`` as
    :func:`~jsonreshape.tasks.parse_size` reads them (binary units); a number without a
    unit means megabytes (``512`` = 512 MB) -- nobody means 512 bytes of sort memory."""
    s = text.strip()
    try:
        if _PLAIN_NUMBER_RE.fullmatch(s):
            n = int(float(s) * MiB)
        else:
            n = parse_size(s)
    except (ValueError, OverflowError):
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(t("cli.argerr.size", value=text))
    return n


def _split_size_value(text: str) -> int:
    """``split --size``: a size read like ``--memory-limit`` (binary units; a plain number means
    megabytes), at least :data:`MIN_SPLIT_SIZE`."""
    n = _memory_size_value(text)
    if n < MIN_SPLIT_SIZE:
        raise argparse.ArgumentTypeError(t("cli.argerr.split_size", value=text, minimum=_mem_text(MIN_SPLIT_SIZE)))
    return n


def _delimiter_value(text: str) -> str:
    if text.strip().lower() in ("tab", "\\t"):
        return "\t"
    return text


# -- parser -------------------------------------------------------------------------------
def _add_global_options(parser: argparse.ArgumentParser, *, repeated: bool) -> None:
    """``--lang --backend -q -v``: on the main parser with real defaults; repeated on every
    command with ``SUPPRESS`` defaults (a value given after the command overrides the one
    given before it, an absent one leaves it alone)."""
    if repeated:
        target: Any = parser.add_argument_group(t("cli.group.global"))
        none, false, zero = argparse.SUPPRESS, argparse.SUPPRESS, argparse.SUPPRESS
    else:
        target = parser
        none, false, zero = None, False, 0
    target.add_argument("--lang", choices=SUPPORTED_LANGUAGES, default=none, help=t("cli.help.lang"))
    target.add_argument("--backend", choices=BACKENDS, default=none, help=t("cli.help.backend"))
    target.add_argument("-q", "--quiet", action="store_true", default=false, help=t("cli.help.quiet"))
    target.add_argument("-v", "--verbose", action="count", default=zero, help=t("cli.help.verbose"))


def _add_input_options(parser: argparse.ArgumentParser, *, partial: bool = True) -> None:
    """``--record-path --format --allow-partial --no-cache`` (``partial=False``: a command that cannot
    work with the records before a JSON error -- ``split`` -- has no ``--allow-partial``)."""
    g = parser.add_argument_group(t("cli.group.input"))
    g.add_argument("--record-path", metavar="P", default=None, help=t("cli.help.record_path"))
    g.add_argument("--format", choices=INPUT_FORMATS, default="auto", help=t("cli.help.format"))
    if partial:
        g.add_argument("--allow-partial", action="store_true", help=t("cli.help.allow_partial"))
    g.add_argument("--no-cache", action="store_true", help=t("cli.help.no_cache"))


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group(t("cli.group.output"))
    g.add_argument("--to", choices=OUTPUT_FORMATS, default=None, help=t("cli.help.to"))
    g.add_argument("--jsonl", action="store_true", help=t("cli.help.jsonl"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--compact", action="store_true", help=t("cli.help.compact"))
    mx.add_argument("--indent", metavar="N", type=_indent_value, default=None, help=t("cli.help.indent"))
    g.add_argument("--sort-keys", nargs="?", const="alpha", choices=SORT_KEY_ORDERS, default=None,
                   metavar="alpha|codepoint", help=t("cli.help.sort_keys"))
    g.add_argument("--no-wrapper", action="store_true", help=t("cli.help.no_wrapper"))
    g.add_argument("--as-keyed", action="store_true", help=t("cli.help.as_keyed"))
    g.add_argument("--columns", metavar="LIST", default=None, help=t("cli.help.columns"))
    g.add_argument("--array-mode", choices=("join", "rows"), default=None, help=t("cli.help.array_mode"))
    g.add_argument("--explode", metavar="PATH", default=None, help=t("cli.help.explode"))
    g.add_argument("--sep", metavar="TEXT", default=None, help=t("cli.help.sep"))
    g.add_argument("--delimiter", metavar="CHAR", type=_delimiter_value, default=None, help=t("cli.help.delimiter"))
    g.add_argument("--no-bom", action="store_true", help=t("cli.help.no_bom"))
    g.add_argument("--xlsx-split", choices=("sheets", "files"), default=None, help=t("cli.help.xlsx_split"))
    g.add_argument("--parquet-compression", choices=PARQUET_COMPRESSIONS, default=None,
                   help=t("cli.help.parquet_compression"))


def _add_filter_options(parser: argparse.ArgumentParser, *, required: bool, select: bool = False) -> None:
    """``--where | --jmespath | --preset``, ``--save-preset`` (and ``--select SPEC`` with ``select``:
    the record scope of docs/ARCHITECTURE.md §14.4)."""
    g = parser.add_argument_group(t("cli.group.filter"))
    mx = g.add_mutually_exclusive_group(required=required)
    mx.add_argument("--where", metavar="EXPR", default=None, help=t("cli.help.where"))
    mx.add_argument("--jmespath", metavar="EXPR", default=None, help=t("cli.help.jmespath"))
    mx.add_argument("--preset", metavar="NAME", default=None, help=t("cli.help.preset"))
    g.add_argument("--save-preset", metavar="NAME", default=None, help=t("cli.help.save_preset"))
    if select:
        _add_select_option(g)


def _add_select_option(group: Any) -> None:
    group.add_argument("--select", metavar="SPEC", default=None, help=t("cli.help.select"))


def _add_memory_option(group: Any, help_key: str) -> None:
    group.add_argument("--memory-limit", metavar="SIZE", type=_memory_size_value, default=None, help=t(help_key))


def _add_json_layout_options(group: Any) -> None:
    """``--compact | --indent N`` (JSON layout of commands without the full output options)."""
    mx = group.add_mutually_exclusive_group()
    mx.add_argument("--compact", action="store_true", help=t("cli.help.compact"))
    mx.add_argument("--indent", metavar="N", type=_indent_value, default=None, help=t("cli.help.indent"))


def _add_sort_options(parser: argparse.ArgumentParser, flag: str, *, required: bool) -> None:
    g = parser.add_argument_group(t("cli.group.sort"))
    g.add_argument(flag, dest="sort_fields", action="append", metavar="FIELD", required=required, default=None,
                   help=t("cli.help.by") if flag == "--by" else t("cli.help.sort_by"))
    g.add_argument("--nulls", choices=("first", "last"), default="last", help=t("cli.help.nulls"))
    g.add_argument("--collation", choices=("uz", "simple", "unicode"), default="uz", help=t("cli.help.collation"))
    g.add_argument("--no-natural", action="store_true", help=t("cli.help.no_natural"))
    g.add_argument("--case-sensitive", action="store_true", help=t("cli.help.sort_case_sensitive"))
    g.add_argument("--memory-limit", metavar="SIZE", type=_memory_size_value, default=None,
                   help=t("cli.help.memory_limit"))


def _add_apply_options(parser: argparse.ArgumentParser) -> None:
    """Output and engine options of ``apply`` / ``flatten`` (:class:`jsonreshape.engine.ApplyOptions`)."""
    g = parser.add_argument_group(t("cli.group.output"))
    g.add_argument("--jsonl", action="store_true", help=t("cli.help.mapping_jsonl"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--compact", action="store_true", help=t("cli.help.compact"))
    mx.add_argument("--indent", metavar="N", type=_indent_value, default=None, help=t("cli.help.indent"))
    g = parser.add_argument_group(t("cli.group.apply"))
    g.add_argument("--skip-huge", action="store_true", help=t("cli.help.skip_huge"))
    g.add_argument("--workers", metavar="N", type=_positive_int, default=None, help=t("cli.help.workers"))


def _add_command(sub: Any, name: str, short_help: str, description: str, epilog: str) -> argparse.ArgumentParser:
    return sub.add_parser(name, help=short_help, description=description, epilog=epilog, formatter_class=_formatter,
                          allow_abbrev=False, **_PARSER_EXTRA)


def _tool_command(sub: Any, name: str) -> argparse.ArgumentParser:
    return _add_command(sub, name, t(f"cli.cmd.{name}.help"), t(f"cli.cmd.{name}.description"),
                        t(f"cli.cmd.{name}.epilog"))


def _add_output_file_option(group: Any, dest: str, help_key: str) -> None:
    """``-o FILE`` (also ``--output`` / ``--out``) of the stage-5 commands."""
    group.add_argument("-o", "--output", "--out", dest=dest, metavar="FILE", default=None, help=t(help_key))


def _add_merge_output_options(parser: argparse.ArgumentParser) -> None:
    """The output options that apply to a merge (JSON / JSON Lines; another ``--to`` is refused by
    :class:`jsonreshape.filetools.MergeOptions` with its own message)."""
    g = parser.add_argument_group(t("cli.group.output"))
    g.add_argument("--to", choices=OUTPUT_FORMATS, default=None, help=t("cli.help.merge_to"))
    g.add_argument("--jsonl", action="store_true", help=t("cli.help.jsonl"))
    _add_json_layout_options(g)
    g.add_argument("--sort-keys", nargs="?", const="alpha", choices=SORT_KEY_ORDERS, default=None,
                   metavar="alpha|codepoint", help=t("cli.help.sort_keys"))
    g.add_argument("--no-wrapper", action="store_true", help=t("cli.help.merge_no_wrapper"))


def _add_tool_commands(sub: Any) -> None:
    """The stage-5 commands (docs/ARCHITECTURE.md §14.8): stats, dupes, schema, validate, split, merge,
    extract, repair, diff."""
    p = _tool_command(sub, "stats")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    g = p.add_argument_group(t("cli.group.stats"))
    g.add_argument("--field", dest="fields", action="append", metavar="PATH", default=None, help=t("cli.help.field"))
    g.add_argument("--top", metavar="N", type=_non_negative_int, default=20, help=t("cli.help.stats_top"))
    g.add_argument("--exact", action="store_true", help=t("cli.help.exact"))
    g.add_argument("--sample", metavar="N", type=_positive_int, default=None, help=t("cli.help.stats_sample"))
    _add_memory_option(g, "cli.help.stats_memory")
    g.add_argument("--json", action="store_true", help=t("cli.help.stats_json"))
    _add_filter_options(p, required=False, select=True)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "dupes")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    g = p.add_argument_group(t("cli.group.dupes"))
    g.add_argument("--key", metavar="KEY", required=True, help=t("cli.help.dupes_key"))
    g.add_argument("--keep", choices=KEEP_CHOICES, default="first", help=t("cli.help.dupes_keep"))
    g.add_argument("--examples", metavar="N", type=_non_negative_int, default=DEFAULT_EXAMPLES,
                   help=t("cli.help.dupes_examples"))
    g.add_argument("--top", metavar="N", type=_non_negative_int, default=0, help=t("cli.help.dupes_top"))
    _add_output_file_option(g, "out", "cli.help.dupes_out")
    _add_memory_option(g, "cli.help.dupes_memory")
    g.add_argument("--json", action="store_true", help=t("cli.help.dupes_json"))
    _add_filter_options(p, required=False, select=True)
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "schema")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", nargs="?", default=None, help=t("cli.help.schema_output"))
    g = p.add_argument_group(t("cli.group.schema"))
    _add_output_file_option(g, "output_opt", "cli.help.output_opt")
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--sample", metavar="N", type=_positive_int, default=None, help=t("cli.help.schema_sample"))
    mx.add_argument("--full", action="store_true", help=t("cli.help.schema_full"))
    g.add_argument("--strict", action="store_true", help=t("cli.help.schema_strict"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--document", action="store_true", help=t("cli.help.schema_document"))
    mx.add_argument("--record", action="store_true", help=t("cli.help.schema_record"))
    g.add_argument("--title", metavar="TEXT", default=None, help=t("cli.help.schema_title"))
    _add_json_layout_options(g)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "validate")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    g = p.add_argument_group(t("cli.group.validate"))
    g.add_argument("--schema", metavar="SCHEMA", required=True, help=t("cli.help.validate_schema"))
    g.add_argument("--scope", choices=SCHEMA_SCOPES, default="auto", help=t("cli.help.validate_scope"))
    g.add_argument("--formats", action="store_true", help=t("cli.help.validate_formats"))
    g.add_argument("--max-issues", metavar="N", type=_non_negative_int, default=DEFAULT_ISSUES,
                   help=t("cli.help.max_issues"))
    g.add_argument("--max-per-record", metavar="N", type=_non_negative_int, default=DEFAULT_PER_RECORD,
                   help=t("cli.help.max_per_record"))
    g.add_argument("--export", metavar="FILE", default=None, help=t("cli.help.validate_export"))
    g.add_argument("--json", action="store_true", help=t("cli.help.validate_json"))
    _add_filter_options(p, required=False, select=True)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "split")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    g = p.add_argument_group(t("cli.group.split"))
    mx = g.add_mutually_exclusive_group(required=True)
    mx.add_argument("--parts", metavar="N", type=_positive_int, default=None, help=t("cli.help.parts"))
    mx.add_argument("--records", metavar="N", type=_positive_int, default=None, help=t("cli.help.records_per_part"))
    mx.add_argument("--size", metavar="SIZE", type=_split_size_value, default=None, help=t("cli.help.part_size"))
    g.add_argument("--balance", choices=SPLIT_BALANCES, default=None, help=t("cli.help.balance"))
    g.add_argument("-o", "--out-dir", dest="out_dir", metavar="DIR", default=None, help=t("cli.help.out_dir"))
    g.add_argument("--name", metavar="PATTERN", default=None, help=t("cli.help.part_name"))
    g.add_argument("--force", action="store_true", help=t("cli.help.force"))
    g.add_argument("--remove-stale", action="store_true", help=t("cli.help.remove_stale"))
    g.add_argument("--drop-broken", action="store_true", help=t("cli.help.drop_broken"))
    _add_filter_options(p, required=False, select=True)
    _add_output_options(p)
    _add_input_options(p, partial=False)
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "merge")
    p.add_argument("inputs", metavar="INPUT", nargs="+", help=t("cli.help.merge_inputs"))
    g = p.add_argument_group(t("cli.group.merge"))
    g.add_argument("-o", "--output", "--out", dest="output", metavar="OUTPUT", required=True,
                   help=t("cli.help.merge_output"))
    g.add_argument("--dedupe-by", metavar="KEY", default=None, help=t("cli.help.dedupe_by"))
    g.add_argument("--keep", choices=MERGE_KEEPS, default=None, help=t("cli.help.merge_keep"))
    _add_memory_option(g, "cli.help.merge_memory")
    _add_merge_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "extract")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", nargs="?", default=None, help=t("cli.help.extract_output"))
    g = p.add_argument_group(t("cli.group.extract"))
    _add_output_file_option(g, "output_opt", "cli.help.output_opt")
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--path", metavar="RECORD_PATH", default=None, help=t("cli.help.extract_path"))
    mx.add_argument("--node", metavar="LOCATION", default=None, help=t("cli.help.node"))
    mx.add_argument("--gather", metavar="PATTERN", default=None, help=t("cli.help.gather"))
    _add_filter_options(p, required=False, select=True)
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "repair")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.repair_input"))
    p.add_argument("output", metavar="OUTPUT", nargs="?", default=None, help=t("cli.help.repair_output"))
    g = p.add_argument_group(t("cli.group.repair"))
    _add_output_file_option(g, "output_opt", "cli.help.output_opt")
    g.add_argument("--check", action="store_true", help=t("cli.help.repair_check"))
    g.add_argument("--nan", choices=REPAIR_NANS, default="null", help=t("cli.help.repair_nan"))
    g.add_argument("--jsonl", action="store_true", help=t("cli.help.repair_jsonl"))
    g.add_argument("--allow-partial", action="store_true", help=t("cli.help.repair_partial"))
    g.add_argument("--report", metavar="FILE", default=None, help=t("cli.help.repair_report"))
    g.add_argument("--json", action="store_true", help=t("cli.help.repair_json"))
    g = p.add_argument_group(t("cli.group.input"))
    g.add_argument("--format", choices=INPUT_FORMATS, default="auto", help=t("cli.help.repair_format"))
    g.add_argument("--record-path", metavar="P", default=None, help=t("cli.help.record_path"))
    _add_global_options(p, repeated=True)

    p = _tool_command(sub, "diff")
    p.add_argument("a", metavar="A", help=t("cli.help.diff_a"))
    p.add_argument("b", metavar="B", help=t("cli.help.diff_b"))
    g = p.add_argument_group(t("cli.group.diff"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--key", metavar="KEY", default=None, help=t("cli.help.diff_key"))
    mx.add_argument("--by-position", action="store_true", help=t("cli.help.by_position"))
    mx.add_argument("--align", action="store_true", help=t("cli.help.align"))
    g.add_argument("--array-key", dest="array_keys", action="append", metavar="FIELD", default=None,
                   help=t("cli.help.array_key"))
    g.add_argument("--numbers", choices=DIFF_NUMBERS, default="value", help=t("cli.help.numbers"))
    g.add_argument("--ignore-order", action="store_true", help=t("cli.help.ignore_order"))
    g.add_argument("--max-changes", metavar="N", type=_non_negative_int, default=None, help=t("cli.help.max_changes"))
    g.add_argument("--limit", metavar="N", type=_non_negative_int, default=DEFAULT_DIFF_LIMIT,
                   help=t("cli.help.diff_limit"))
    g.add_argument("--summary", action="store_true", help=t("cli.help.diff_summary"))
    g.add_argument("--json", action="store_true", help=t("cli.help.diff_json"))
    _add_output_file_option(g, "out", "cli.help.diff_out")
    g.add_argument("--exit-code", action="store_true", help=t("cli.help.exit_code"))
    _add_memory_option(g, "cli.help.diff_memory")
    _add_input_options(p)
    _add_global_options(p, repeated=True)


def build_parser() -> _ArgumentParser:
    """The localized argument parser (texts in the current language). Build and use it
    inside :func:`localized_argparse` so argparse's own phrases are translated as well."""
    parser = _ArgumentParser(prog=PROG, description=t("cli.help.description"), epilog=t("cli.help.epilog"),
                             formatter_class=_formatter, allow_abbrev=False, **_PARSER_EXTRA)
    parser.add_argument("--version", action="store_true", help=t("cli.help.version"))
    _add_global_options(parser, repeated=False)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", title=t("cli.help.commands"))

    p = _add_command(sub, "inspect", t("cli.cmd.inspect.help"), t("cli.cmd.inspect.description"),
                     t("cli.cmd.inspect.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    g = p.add_argument_group(t("cli.group.inspect"))
    g.add_argument("--sample", metavar="N", type=_positive_int, default=None, help=t("cli.help.sample"))
    g.add_argument("--json", action="store_true", help=t("cli.help.json"))
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "search", t("cli.cmd.search.help"), t("cli.cmd.search.description"),
                     t("cli.cmd.search.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("text", metavar="TEXT", help=t("cli.help.text"))
    g = p.add_argument_group(t("cli.group.search"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--keys", action="store_true", help=t("cli.help.keys"))
    mx.add_argument("--values", action="store_true", help=t("cli.help.values"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--regex", action="store_true", help=t("cli.help.regex"))
    mx.add_argument("--number", action="store_true", help=t("cli.help.number"))
    g.add_argument("--case-sensitive", action="store_true", help=t("cli.help.case_sensitive"))
    g.add_argument("--whole-word", action="store_true", help=t("cli.help.whole_word"))
    g.add_argument("--translit", action="store_true", help=t("cli.help.translit"))
    g.add_argument("--path", metavar="P", default=None, help=t("cli.help.path"))
    g.add_argument("--limit", metavar="N", type=_non_negative_int, default=DEFAULT_LIMIT, help=t("cli.help.limit"))
    g.add_argument("--count", action="store_true", help=t("cli.help.count"))
    g.add_argument("--export", metavar="FILE", default=None, help=t("cli.help.export"))
    g.add_argument("--max-hits", metavar="N", type=_non_negative_int, default=DEFAULT_MAX_HITS,
                   help=t("cli.help.max_hits"))
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "filter", t("cli.cmd.filter.help"), t("cli.cmd.filter.description"),
                     t("cli.cmd.filter.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.output"))
    _add_filter_options(p, required=True, select=True)
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "sort", t("cli.cmd.sort.help"), t("cli.cmd.sort.description"), t("cli.cmd.sort.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.output"))
    _add_sort_options(p, "--by", required=True)
    _add_select_option(p.add_argument_group(t("cli.group.select")))
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "convert", t("cli.cmd.convert.help"), t("cli.cmd.convert.description"),
                     t("cli.cmd.convert.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.output"))
    _add_filter_options(p, required=False, select=True)
    p.add_argument_group(t("cli.group.node")).add_argument("--node", metavar="LOCATION", default=None,
                                                           help=t("cli.help.node"))
    _add_sort_options(p, "--sort-by", required=False)
    _add_output_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "patch", t("cli.cmd.patch.help"), t("cli.cmd.patch.description"),
                     t("cli.cmd.patch.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("patch", metavar="PATCH", help=t("cli.help.patch"))
    p.add_argument("output", metavar="OUTPUT", nargs="?", default=None, help=t("cli.help.patch_output"))
    g = p.add_argument_group(t("cli.group.patch"))
    g.add_argument("--in-place", action="store_true", help=t("cli.help.in_place"))
    g.add_argument("--no-backup", action="store_true", help=t("cli.help.no_backup"))
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "apply", t("cli.cmd.apply.help"), t("cli.cmd.apply.description"),
                     t("cli.cmd.apply.epilog"))
    p.add_argument("mapping", metavar="MAPPING", help=t("cli.help.mapping"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.mapping_output"))
    _add_apply_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    p = _add_command(sub, "flatten", t("cli.cmd.flatten.help"), t("cli.cmd.flatten.description"),
                     t("cli.cmd.flatten.epilog"))
    p.add_argument("input", metavar="INPUT", help=t("cli.help.input"))
    p.add_argument("output", metavar="OUTPUT", help=t("cli.help.mapping_output"))
    g = p.add_argument_group(t("cli.group.flatten"))
    g.add_argument("--children-key", metavar="KEY", required=True, help=t("cli.help.children_key"))
    g.add_argument("--depth-field", metavar="NAME", default=None, help=t("cli.help.depth_field"))
    g.add_argument("--mode", choices=("nested", "records"), default="nested", help=t("cli.help.flatten_mode"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--sample", metavar="N", type=_positive_int, default=None, help=t("cli.help.mapping_sample"))
    mx.add_argument("--full-scan", action="store_true", help=t("cli.help.full_scan"))
    _add_apply_options(p)
    _add_input_options(p)
    _add_global_options(p, repeated=True)

    _add_tool_commands(sub)
    _add_import_command(sub)

    p = _add_command(sub, "presets", t("cli.cmd.presets.help"), t("cli.cmd.presets.description"),
                     t("cli.cmd.presets.epilog"))
    g = p.add_argument_group(t("cli.group.presets"))
    g.add_argument("--delete", metavar="NAME", default=None, help=t("cli.help.delete_preset"))
    g.add_argument("--json", action="store_true", help=t("cli.help.presets_json"))
    _add_global_options(p, repeated=True)
    parser.commands = dict(sub.choices)
    return parser


def _parse(parser: _ArgumentParser, argv: Sequence[str]) -> argparse.Namespace:
    """Parse; arguments nobody recognised are reported by the command's own parser (its
    usage line is the useful one)."""
    args, extras = parser.parse_known_args(argv)
    if extras:
        target = parser.commands.get(args.command or "", parser)
        target.error(t("cli.argparse.unrecognized") % " ".join(extras))
    if args.command is None and not args.version:
        parser.error(t("cli.argerr.no_command"))
    return args


def _scan_option(argv: Sequence[str], name: str) -> str | None:
    """Value of ``name`` (``--lang x`` / ``--lang=x``) in raw arguments (last one wins)."""
    value = None
    for i, tok in enumerate(argv):
        if tok == "--":
            break
        if tok == name and i + 1 < len(argv):
            value = argv[i + 1]
        elif tok.startswith(name + "="):
            value = tok[len(name) + 1:]
    return value


def _join_dash_values(argv: Sequence[str]) -> list[str]:
    """``--by -id`` -> ``--by=-id`` (argparse reads a lone ``-id`` as an unknown option; the same
    for every option of :data:`DASH_VALUE_OPTIONS`);
    ``--sort-keys`` followed by an argument that is neither ``alpha``/``codepoint`` nor an
    option -> ``--sort-keys=alpha`` (its optional value must never swallow a positional
    argument: ``--sort-keys in.json out.json``)."""
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            out.extend(argv[i:])
            break
        if tok in DASH_VALUE_OPTIONS and i + 1 < n:
            nxt = argv[i + 1]
            if nxt.startswith("-") and len(nxt) > 1 and not nxt.startswith("--"):
                out.append(f"{tok}={nxt}")
                i += 2
                continue
        if tok == "--sort-keys" and i + 1 < n and argv[i + 1] not in SORT_KEY_ORDERS \
                and not argv[i + 1].startswith("-"):
            out.append("--sort-keys=alpha")
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _choose_language(cli_value: str | None) -> str:
    """``--lang`` > ``JSONRESHAPE_LANG`` > ``settings.language`` > ``uz``."""
    for candidate in (cli_value, os.environ.get("JSONRESHAPE_LANG")):
        if candidate and candidate.strip().lower() in SUPPORTED_LANGUAGES:
            return candidate.strip().lower()
    try:
        from .settings import get_settings

        language = get_settings().language
        if language in SUPPORTED_LANGUAGES:
            return language
    except Exception:  # pragma: no cover - settings never raise, but the language must not fail
        log.warning("could not read the language setting", exc_info=True)
    return "uz"


# ==================================================================================
# localized texts with counts
# ==================================================================================
def _tn(key: str, count: Any, /, **params: Any) -> str:
    """:func:`t` with the singular variant ``<key>.one`` when ``count`` is 1 and that key
    exists (English "1 record" / "2 records"; the Uzbek texts are the same in both)."""
    try:
        one = int(count) == 1
    except (TypeError, ValueError):
        one = False
    if one and has(key + ".one"):
        return t(key + ".one", **params)
    return t(key, **params)


def _hits_text(n: int, shown: str) -> str:
    return _tn("cli.count.hits", n, count=shown)


def _records_text(n: int, shown: str) -> str:
    return _tn("cli.count.records", n, count=shown)


# ==================================================================================
# console output
# ==================================================================================
class _OutputClosed(Exception):
    """stdout was closed by the reader (broken pipe)."""


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _setup_streams() -> None:
    """UTF-8 for redirected stdout/stderr; never crash on unencodable characters."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if _isatty(stream):
                reconfigure(errors="backslashreplace")
            else:
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError, LookupError):  # pragma: no cover - exotic streams
            pass


def _enable_ansi() -> bool:
    """Make sure the console of stdout understands ANSI colours (Windows: VT mode)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # private instance: own argtypes
        kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel32.GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | vt))
    except Exception:  # pragma: no cover - no console API
        return False


def _closed_pipe(exc: OSError) -> bool:
    # Windows reports a pipe closed by the reader as EINVAL as often as EPIPE
    return isinstance(exc, BrokenPipeError) or exc.errno in (errno.EPIPE, errno.EINVAL)


def _write(stream: Any, text: str, *, flush: bool = False) -> None:
    """Write (and flush) ``text``; a reader that went away raises :class:`_OutputClosed`."""
    if stream is None:
        return
    try:
        stream.write(text)
        if flush:
            stream.flush()
    except OSError as exc:
        if _closed_pipe(exc):
            raise _OutputClosed() from None
        raise
    except ValueError:  # the stream was closed
        raise _OutputClosed() from None


def _flush(stream: Any) -> None:
    """Best-effort flush (status output, end of run)."""
    try:
        if stream is not None:
            stream.flush()
    except (OSError, ValueError):
        pass


def _terminal_columns() -> int:
    for stream in (sys.stderr, sys.stdout):
        try:
            return max(20, os.get_terminal_size(stream.fileno()).columns)
        except (AttributeError, ValueError, OSError):
            continue
    return 80


def _num(n: int) -> str:
    """Grouped digits: ``12 345`` (Uzbek, Russian), ``12,345`` (English)."""
    text = f"{int(n):,}"
    return text.replace(",", " ") if space_grouped() else text


def _mem_text(n: int) -> str:
    """A memory size in the binary units ``--memory-limit`` is read in (``16 MB`` = 16 MiB)."""
    for unit, factor in (("GB", 1 << 30), ("MB", MiB), ("KB", 1 << 10)):
        if n >= factor:
            value = n / factor
            return f"{value:.0f} {unit}" if value == int(value) or value >= 100 else f"{value:.1f} {unit}"
    return f"{int(n)} B"


def _display_text(s: str) -> str:
    """One-line display form: control/format characters escaped (``\\n``, ``\\u0007``)."""
    if s.isascii() and s.isprintable():
        return s
    return _CONTROL_RE.sub(_escape_match, s)


def _escape_match(m: re.Match[str]) -> str:
    ch = m.group()
    return _ESCAPE_NAMES.get(ch) or "\\" + "u" + f"{ord(ch):04x}"


def _same_width_text(s: str) -> str:
    """Control characters replaced one-for-one (keeps a caret line aligned)."""
    return _CONTROL_RE.sub(lambda m: " " if m.group() in "\t\n\r" else "?", s)


def _caret_lines(text: str, pos: int) -> list[str]:
    shown = _same_width_text(text)
    pos = max(0, min(pos, len(shown)))
    return [shown, " " * pos + "^"]


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + ELLIPSIS


def _stage_label(message: str) -> str:
    """A progress message as the label of the progress line: one line, without a trailing
    ellipsis or colon (the line adds ``": "``)."""
    text = _display_text(message or "").strip()
    while text:
        stripped = text.rstrip(":" + ELLIPSIS).rstrip()
        if stripped.endswith("..."):
            stripped = stripped[:-3].rstrip()
        if stripped == text:
            break
        text = stripped
    return text


def _display_path(path: Any) -> str:
    """A path for messages: ``str`` without the ``\\\\?\\`` long-path prefix."""
    s = os.fsdecode(path) if isinstance(path, bytes) else str(path)
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


class _ProgressLine:
    """One progress line on stderr, refreshed in place (``\\r``), cleared at the end."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = threading.RLock()
        self._label = ""
        self._shown = 0

    def stage(self, label: str) -> Progress | None:
        """A :class:`~jsonreshape.tasks.Progress` drawing this line (``None`` when progress
        is disabled). ``label`` is its initial message: operations keep a message they
        find, and replace it to announce a phase of their own (sort: merging), which then
        labels the line."""
        if not self.enabled:
            return None
        self.clear()
        with self._lock:
            self._label = label
        return Progress(callback=self._render, min_interval=0.1, message=label)

    def format(self, info: ProgressInfo, width: int | None = None) -> str:
        """The line for ``info``; longer than ``width``: the label is shortened first, so
        the numbers stay visible."""
        label = _stage_label(info.message) or self._label
        text = self._line(info, label)
        if width is not None and len(text) > width:
            room = width - (len(text) - len(label))
            if room >= MIN_PROGRESS_LABEL:
                text = self._line(info, _shorten(label, room))
        return text

    @staticmethod
    def _line(info: ProgressInfo, label: str) -> str:
        if info.total and info.fraction is not None:
            return t("cli.progress.line", stage=label, percent=f"{info.fraction * 100:.1f}%",
                     done=format_bytes(info.done), total=format_bytes(info.total), rate=format_bytes(info.rate),
                     eta=format_duration(info.eta))
        return t("cli.progress.line_unknown", stage=label, done=format_bytes(info.done), rate=format_bytes(info.rate))

    def _render(self, info: ProgressInfo) -> None:
        if info.finished:
            self.clear()
            return
        limit = _terminal_columns() - 1
        text = self.format(info, limit)
        if len(text) > limit:
            text = text[:max(0, limit - 1)] + ELLIPSIS
        with self._lock:
            pad = max(0, self._shown - len(text))
            try:
                _write(sys.stderr, "\r" + text + " " * pad)
                _flush(sys.stderr)
            except _OutputClosed:
                self.enabled = False
                return
            self._shown = len(text)

    def clear(self) -> None:
        with self._lock:
            if self._shown:
                try:
                    _write(sys.stderr, "\r" + " " * self._shown + "\r")
                    _flush(sys.stderr)
                except _OutputClosed:
                    self.enabled = False
                self._shown = 0


class _ClearProgressFilter(logging.Filter):
    """Clears the progress line before a console log record is written."""

    def __init__(self, line: _ProgressLine) -> None:
        super().__init__()
        self.line = line

    def filter(self, record: logging.LogRecord) -> bool:
        self.line.clear()
        return True


class _Console:
    """stdout/stderr writer of one command run (looked up at call time, so captured
    streams of tests work)."""

    def __init__(self, *, quiet: bool, verbose: int) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.progress = _ProgressLine(enabled=not quiet and _isatty(sys.stderr))
        self.color = _isatty(sys.stdout) and "NO_COLOR" not in os.environ and _enable_ansi()
        self._filtered: list[logging.Handler] = []
        self._filter = _ClearProgressFilter(self.progress)

    # -- plain output -----------------------------------------------------------------
    def out(self, text: str = "") -> None:
        self.progress.clear()
        _write(sys.stdout, text + "\n")

    def out_lines(self, lines: Iterable[str]) -> None:
        self.progress.clear()
        _write(sys.stdout, "".join(line + "\n" for line in lines), flush=True)

    def err(self, text: str) -> None:
        self.progress.clear()
        try:
            _write(sys.stderr, text + "\n")
            _flush(sys.stderr)
        except _OutputClosed:
            pass

    def info(self, text: str) -> None:
        if not self.quiet:
            self.err(text)

    def warn(self, text: str) -> None:
        self.err(t("cli.warning", message=text))

    def hint(self, text: str) -> None:
        self.err(t("cli.error.hint", hint=text))

    def error(self, exc: JsonReshapeError, *, hint: str | None = None) -> None:
        lines = [t("cli.error.prefix", message=exc.message)]
        lines.extend("  " + line for line in _detail_lines(exc))
        shown_hint = hint if hint is not None else _hint_of(exc)
        if shown_hint:
            lines.append(t("cli.error.hint", hint=shown_hint))
        self.err("\n".join(lines))

    # -- log records on the console never break the progress line ----------------------
    def attach_log_filter(self) -> None:
        if not self.progress.enabled:
            return
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.addFilter(self._filter)
                self._filtered.append(handler)

    def finish(self) -> None:
        self.progress.clear()
        for handler in self._filtered:
            handler.removeFilter(self._filter)
        self._filtered.clear()
        _flush(sys.stdout)
        _flush(sys.stderr)


# ==================================================================================
# errors shown by the CLI
# ==================================================================================
class _HintedError(UserInputError):
    """A user error whose hint the CLI computes (e.g. the record arrays found in the file)."""

    def __init__(self, key: str, /, *, hint: str | None = None, **params: Any) -> None:
        super().__init__(key, **params)
        self._hint = hint

    @property
    def hint(self) -> str | None:
        return self._hint if self._hint is not None else super().hint


class _OsError(UserInputError):
    """An :class:`OSError` with a localized message and hint; the raw OS text (often in the
    Windows display language) is shown as a detail line only."""

    def __init__(self, key: str, /, *, os_text: str = "", **params: Any) -> None:
        super().__init__(key, **params)
        self.os_text = os_text


def _hint_of(exc: JsonReshapeError) -> str | None:
    override = getattr(exc, CLI_HINT_ATTR, None)
    return override if override is not None else exc.hint


def _set_hint(exc: JsonReshapeError, hint: str) -> None:
    try:
        setattr(exc, CLI_HINT_ATTR, hint)
    except AttributeError:  # pragma: no cover - exceptions accept attributes
        pass


def _detail_lines(exc: JsonReshapeError) -> list[str]:
    """Caret lines of syntax errors (expression / path / JSON); the OS text of OS errors."""
    if isinstance(exc, QuerySyntaxError) and exc.text:
        return _caret_lines(exc.text, exc.pos)
    if isinstance(exc, JsonSyntaxError) and (exc.context_before or exc.context_after):
        before = _same_width_text(exc.context_before)
        after = _same_width_text(exc.context_after)
        return [before + after, " " * len(before) + "^"]
    if isinstance(exc, _OsError) and exc.os_text:
        return [t("cli.error.os_detail", detail=_display_text(exc.os_text))]
    return []


#: Windows error codes (``OSError.winerror``) of the common file-system failures
_WIN_NOT_FOUND = frozenset({2, 3, 53, 67, 161})   # file / path not found, bad network path / name, bad pathname
_WIN_ACCESS_DENIED = frozenset({5})
_WIN_IN_USE = frozenset({32, 33})                 # sharing violation, lock violation
_WIN_DISK_FULL = frozenset({39, 112})             # handle disk full, disk full
_WIN_EXISTS = frozenset({80, 183})                # file exists, already exists


def _read_only_file(path: Any) -> bool:
    """``path`` is an existing file with the read-only attribute (Windows) / not writable."""
    from .fileio import long_path

    try:
        lp = long_path(path)
        st = os.stat(lp)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    attributes = getattr(st, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 1))
    try:
        return not os.access(lp, os.W_OK)
    except (OSError, ValueError):  # pragma: no cover
        return False


def _os_error(exc: OSError) -> JsonReshapeError:
    """A localized error for an :class:`OSError` that reached the command line (the
    messages of :func:`jsonreshape.fileio.os_error` where it has them: file in use,
    read-only file, disk full)."""
    winerror = getattr(exc, "winerror", None)
    code = exc.errno
    raw = exc.strerror or str(exc)
    path = exc.filename if exc.filename is not None else exc.filename2
    if path is None:
        return UserInputError("cli.error.os", detail=raw)
    shown = _display_path(path)
    if winerror in _WIN_IN_USE:
        return _OsError("fileio.error.in_use", path=shown, os_text=raw)
    if winerror in _WIN_NOT_FOUND or isinstance(exc, FileNotFoundError) or code == errno.ENOENT:
        return _OsError("cli.error.os.not_found", path=shown, os_text=raw)
    if winerror in _WIN_DISK_FULL or code == errno.ENOSPC:
        return _OsError("fileio.error.disk_full", path=shown, os_text=raw)
    if winerror in _WIN_EXISTS or isinstance(exc, FileExistsError) or code == errno.EEXIST:
        return _OsError("cli.error.os.exists", path=shown, os_text=raw)
    if winerror in _WIN_ACCESS_DENIED or isinstance(exc, PermissionError) or code in (errno.EACCES, errno.EPERM):
        if _read_only_file(path):
            return _OsError("fileio.error.read_only", path=shown, os_text=raw)
        return _OsError("cli.error.os.access_denied", path=shown, os_text=raw)
    return UserInputError("cli.error.os", detail=f"{raw}: {shown}")


# ==================================================================================
# Ctrl+C / Ctrl+Break
# ==================================================================================
class _Interrupts:
    """SIGINT/SIGBREAK handlers that cancel ``token``; the second signal raises
    ``KeyboardInterrupt`` at once. Installed only in the main thread; restored afterwards."""

    def __init__(self, token: CancelToken) -> None:
        self.token = token
        self._saved: list[tuple[int, Any]] = []

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for name in ("SIGINT", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous = signal.signal(signum, self._handle)
            except (OSError, ValueError, RuntimeError):  # pragma: no cover
                continue
            self._saved.append((signum, previous))

    def _handle(self, signum: int, frame: Any) -> None:
        if self.token.cancelled:
            raise KeyboardInterrupt
        self.token.cancel()

    def restore(self) -> None:
        while self._saved:
            signum, previous = self._saved.pop()
            try:
                signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
            except (OSError, ValueError, RuntimeError):  # pragma: no cover
                pass


# ==================================================================================
# command context and shared steps
# ==================================================================================
class _Ctx:
    """Everything a command needs: arguments, console, cancel token, settings."""

    def __init__(self, args: argparse.Namespace, console: _Console, cancel: CancelToken) -> None:
        from .settings import get_settings

        self.args = args
        self.console = console
        self.cancel = cancel
        self.settings = get_settings()
        self.started = time.perf_counter()
        # warnings of the input documents (doc.info.warnings, see _doc_warnings): printed
        # already, and those they had when they were opened (printed by the command or never)
        self.warnings_printed: set[tuple[str, str, str]] = set()
        self.warnings_at_open: set[tuple[str, str, str]] = set()

    def stage(self, label: str) -> Progress | None:
        return self.console.progress.stage(label)

    def end_stage(self) -> None:
        self.console.progress.clear()

    def warn(self, text: str) -> None:
        self.console.warn(text)

    def info(self, text: str) -> None:
        self.console.info(text)

    def elapsed(self) -> float:
        return time.perf_counter() - self.started


def _check_input(path: str) -> None:
    from .fileio import long_path
    from .stdin_input import STDIN_MARK

    if path == STDIN_MARK:   # a "-" _stdin_inputs did not spool: PATCH, MAPPING, --schema are not INPUTs
        raise UserInputError("cli.error.stdin_unsupported")
    lp = long_path(path)
    if os.path.isdir(lp):
        raise UserInputError("fileio.error.is_directory", path=path)
    if not os.path.exists(lp):
        raise UserInputError("error.file_not_found", path=path)


def _same_file(a: str, b: str) -> bool:
    from .fileio import long_path, normalize_path

    try:
        if normalize_path(a) == normalize_path(b):
            return True
    except (OSError, ValueError):  # pragma: no cover
        pass
    try:
        return os.path.samefile(long_path(a), long_path(b))
    except (OSError, ValueError):
        return False


def _check_output(input_path: str, output_path: str) -> None:
    """Refuse an output that is the input itself (AGENT_PROMPT §1.4: the source is never
    modified), a folder, an existing read-only file, a path in a folder that does not
    exist, or a file with unsaved edits in this program (a tab or another instance edits
    it, or its edits are kept for recovery: replacing it would make them impossible to save;
    ``filetools.error.part_open_edited``, :func:`jsonreshape.writer.edited_targets`) --
    before any work."""
    from .fileio import long_path
    from .stdin_input import STDIN_MARK
    from .writer import refuse_edited_targets

    if output_path == STDIN_MARK:   # outputs are files (checked before the command by _stdin_inputs too)
        raise UserInputError("cli.error.stdout_output")
    if _same_file(input_path, output_path):
        raise UserInputError("writer.error.same_file", path=output_path)
    if os.path.isdir(long_path(output_path)):
        raise UserInputError("fileio.error.is_directory", path=output_path)
    if _read_only_file(output_path):
        raise UserInputError("fileio.error.read_only", path=output_path)
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent and not os.path.isdir(long_path(parent)):
        raise UserInputError("writer.error.no_dir", path=parent)
    refuse_edited_targets([output_path])


#: argparse destinations of the files a command reads as INPUT: one of them may be "-" (standard input)
STDIN_INPUT_DESTS = ("input", "inputs", "a", "b")
#: argparse destinations of everything a command writes: never "-" (outputs are files)
OUTPUT_DESTS = ("output", "output_opt", "out", "out_dir", "export", "report")


def _stdin_places(args: argparse.Namespace) -> list[tuple[str, int | None]]:
    """``(dest, index)`` of every INPUT argument given as ``-`` (``index``: its position in a list)."""
    from .stdin_input import STDIN_MARK

    places: list[tuple[str, int | None]] = []
    for dest in STDIN_INPUT_DESTS:
        value = getattr(args, dest, None)
        if isinstance(value, list):
            places.extend((dest, i) for i, item in enumerate(value) if item == STDIN_MARK)
        elif value == STDIN_MARK:
            places.append((dest, None))
    return places


@contextlib.contextmanager
def _stdin_inputs(ctx: _Ctx) -> Iterator[None]:
    """Standard input as INPUT (see "Standard input" in the module docstring). An INPUT given as ``-``
    is spooled to a temporary file in the work folder before the command starts
    (:func:`jsonreshape.stdin_input.spool_stdin`: 1 MiB pieces, a progress line with the bytes read
    so far, cancellation, the free-space rule) and the ``-`` in ``ctx.args`` is replaced by that file's
    path, so the command works on it as on a named file; one info line tells the bytes read. The spool
    file is removed when the block ends, also on an error. Refused before anything is read: ``-`` as an
    output, two ``-``, a terminal as standard input, and the command-specific cases (``patch
    --in-place``, ``split`` without ``-o``, ``import`` without ``--from``)."""
    from .appdirs import work_dir
    from .stdin_input import STDIN_MARK, spool_stdin, stdin_is_tty

    args = ctx.args
    for dest in OUTPUT_DESTS:
        if getattr(args, dest, None) == STDIN_MARK:
            raise UserInputError("cli.error.stdout_output")
    places = _stdin_places(args)
    if not places:
        yield
        return
    if len(places) > 1:
        raise UserInputError("cli.error.stdin_twice")
    if args.command == "patch" and getattr(args, "in_place", False):
        raise UserInputError("cli.error.stdin_in_place")
    if args.command == "split" and getattr(args, "out_dir", None) is None:
        raise UserInputError("cli.error.stdin_split_dir")
    if args.command == "import" and getattr(args, "from_fmt", "auto") == "auto":
        raise UserInputError("cli.error.stdin_import_format")
    if stdin_is_tty():
        raise UserInputError("cli.error.stdin_tty")
    try:
        spool = spool_stdin(work_dir(ctx.settings.cache_dir), progress=ctx.stage(t("cli.stdin.stage")),
                            cancel=ctx.cancel)
    finally:
        ctx.end_stage()
    try:
        shown = _display_path(spool.path)
        ctx.info(t("cli.stdin.summary", size=format_bytes(spool.size), path=shown))
        dest, index = places[0]
        if index is None:
            setattr(args, dest, shown)
        else:
            values = list(getattr(args, dest))
            values[index] = shown
            setattr(args, dest, values)
        yield
    finally:
        spool.remove()


def _input_changed(doc: Any) -> bool:
    """The (still open) input changed since it was opened; never raises."""
    try:
        return bool(doc.check_changed())
    except Exception:  # pragma: no cover - a failing check must not hide the real error
        log.debug("check_changed failed", exc_info=True)
        return False


def _refuse_record_path_on_values(ctx: _Ctx, path: str) -> None:
    """``--record-path`` selects the records inside ONE JSON document: an input detected
    (``--format auto``, without the record path) as JSON Lines or as several JSON values
    one after another is refused before any work -- exit 1, like a forced
    ``--format jsonl --record-path``."""
    from .reader import detect_format

    info = detect_format(path, settings=ctx.settings, progress=ctx.stage(t("cli.stage.preparing")),
                         cancel=ctx.cancel)
    ctx.end_stage()
    if info.kind in ("jsonl", "stream"):
        raise UserInputError("cli.error.record_path_values", path=_display_text(str(ctx.args.record_path)),
                             format=info.label)


def _doc_warnings(doc: Any) -> list[tuple[tuple[str, str, str], str]]:
    """``(identity, text)`` of the document's warnings in order. A warning is identified by
    its key, ``path`` parameter and document (commands with several inputs); one added more
    than once (e.g. with a newer byte count) appears once, at its first position, with its
    latest parameters."""
    info = doc.info
    keys = list(info.warnings)
    params = list(info.warning_params)
    texts = info.warning_texts()   # never shorter than keys (warnings are only appended)
    source = str(getattr(doc, "source_path", ""))
    latest: dict[tuple[str, str, str], int] = {}
    for i, key in enumerate(keys):
        p = params[i] if i < len(params) and isinstance(params[i], dict) else {}
        latest[(key, str(p.get("path", "")), source)] = i
    return [(ident, texts[i]) for ident, i in latest.items()]


def _shown_now(ctx: _Ctx, doc: Any) -> list[str]:
    """The document's warnings to show at this point -- not printed yet, not one of
    :data:`END_WARNINGS` -- marked as printed."""
    out = []
    for ident, text in _doc_warnings(doc):
        if ident[0] in END_WARNINGS or ident in ctx.warnings_printed:
            continue
        ctx.warnings_printed.add(ident)
        out.append(text)
    return out


def _late_warnings(ctx: _Ctx, doc: Any, label: str | None = None) -> None:
    """At the end of the command: print the warnings the document gained while it ran and
    the :data:`END_WARNINGS` (with their latest values), each once; a warning it had when
    it was opened but that the command never showed (it failed before) stays unprinted.
    ``label``: the input's name, put before the texts (commands with several inputs)."""
    prefix = "" if label is None else _display_text(label) + ": "
    for ident, text in _doc_warnings(doc):
        if ident in ctx.warnings_printed or ident in ctx.warnings_at_open:
            continue
        ctx.warnings_printed.add(ident)
        ctx.warn(prefix + text)


@contextlib.contextmanager
def _document(ctx: _Ctx, *, build: bool = True, path: str | None = None) -> Iterator[Any]:
    """INPUT (``path``: another input of a command with several -- ``merge``, ``diff``) opened with
    the input options and indexed unless ``build`` is false or a complete cached index exists,
    closed when the block ends. An error other than a cancellation raised while the document is
    open becomes :class:`FileChangedError` when another program changed the file meanwhile: that
    is the real cause, not the garbled data it produced. Warnings the document gains while the
    block runs are printed when it ends (:func:`_late_warnings`).

    ``--record-path``: refused on an input detected as JSON Lines / several values
    (:func:`_refuse_record_path_on_values`); a syntax error in it gets record-path
    examples as its hint."""
    from .reader import Document

    args = ctx.args
    source = args.input if path is None else path
    _check_input(source)
    fmt = None if getattr(args, "format", None) in (None, "auto") else args.format
    record_path = getattr(args, "record_path", None)
    try:
        if record_path is not None and fmt is None:
            _refuse_record_path_on_values(ctx, source)
        doc = Document.open(source, record_path=record_path, format=fmt, settings=ctx.settings,
                            use_cache=not getattr(args, "no_cache", False),
                            progress=ctx.stage(t("cli.stage.preparing")), cancel=ctx.cancel)
    except QuerySyntaxError as exc:
        if record_path is not None:  # the only path parsed while opening
            _set_hint(exc, t("cli.hint.record_path_syntax"))
        raise
    try:
        ctx.warnings_at_open |= {ident for ident, _ in _doc_warnings(doc) if ident[0] not in END_WARNINGS}
        ctx.end_stage()
        if build and not doc.index_complete:
            doc.build_index(progress=ctx.stage(t("cli.stage.indexing")), cancel=ctx.cancel)
            ctx.end_stage()
        yield doc
    except (Cancelled, KeyboardInterrupt, FileChangedError, _OutputClosed, GeneratorExit):
        raise
    except BaseException as exc:
        if _input_changed(doc):
            log.warning("the input changed while it was read; reported instead of %s: %s", type(exc).__name__, exc)
            raise FileChangedError(path=str(doc.source_path)) from exc
        raise
    finally:
        try:
            _late_warnings(ctx, doc, path)   # an input of several (path given) is named
        except Exception:  # pragma: no cover - a warning must never hide the command's own result
            log.debug("could not show the document's late warnings", exc_info=True)
        finally:
            doc.close()


def _line_reason(code: str, expected: str) -> str:
    """Why a JSON Lines line is broken, in line-oriented words: ``json.line_error.<code>``
    / ``json.line_expected.<code>`` where they exist (the general texts speak of the end of
    the whole file), else ``json.error.<code>`` / ``json.expected.<code>``."""
    key = f"json.line_error.{code}"
    text = t(key) if has(key) else t(f"json.error.{code}")
    if expected and expected != "none":
        key = f"json.line_expected.{expected}"
        exp = t(key) if has(key) else t(f"json.expected.{expected}")
        if exp:
            text = f"{text}; {exp}"
    return text


def _broken_line_message(b: Any) -> str:
    return t("reader.broken_line", line=b.line, column=b.column, reason=_line_reason(b.code, b.expected))


def _broken_lines_text(doc: Any) -> str:
    numbers = [str(b.line) for b in doc.broken_lines[:BROKEN_SHOWN]]
    if doc.broken_count > len(numbers):
        numbers.append(ELLIPSIS)
    return _tn("cli.warn.broken_lines", doc.broken_count, count=_num(doc.broken_count), lines=", ".join(numbers))


def _partial_hint(doc: Any) -> str:
    n = doc.record_count
    if n == 0:
        return t("cli.hint.no_records_before_error")
    return _tn("cli.hint.allow_partial", n, count=_num(n))


_VALUE_START = frozenset('{["-0123456789tfn')


def _several_values(doc: Any, err: JsonSyntaxError) -> str | None:
    """``"jsonl"`` / ``"stream"`` when the JSON error shows that the input holds several
    JSON values (or the reader's ``format_hint`` says so), else ``None``.

    The error must be ``trailing_data`` where the trailing data starts a JSON value: after
    a line break following a root value that ended on line 1, one value per line
    (``jsonl``); otherwise values one after another (``stream``)."""
    hint = getattr(doc, "format_hint", None)
    if callable(hint):
        try:
            hint = hint()
        except Exception:  # pragma: no cover - an optional reader helper
            hint = None
    if isinstance(hint, str) and hint in ("jsonl", "stream"):
        return hint
    if err.code != "trailing_data" or not err.context_after or err.context_after[0] not in _VALUE_START:
        return None
    before = err.context_before or ""
    value_part = before.rstrip()
    gap = before[len(value_part):]
    if "\n" in gap and value_part and err.line - gap.count("\n") == 1:
        return "jsonl"
    return "stream"


def _format_hint(ctx: _Ctx, doc: Any, err: JsonSyntaxError) -> str | None:
    """A ``--format jsonl`` / ``--format stream`` hint (instead of ``--allow-partial``) for
    an input that evidently holds several JSON values (blackbox F2: e.g. a JSON Lines file
    named ``.json`` whose first line is larger than the head used for format detection)."""
    kind = _several_values(doc, err)
    if kind is None:
        return None
    if getattr(ctx.args, "record_path", None) is not None:
        return t("cli.hint.record_path_values")
    return t("cli.hint.format_jsonl") if kind == "jsonl" else t("cli.hint.format_stream")


def _candidate_list(cands: Sequence[Any]) -> str:
    parts = []
    for c in cands[:CANDIDATES_SHOWN]:
        items = _num(c.items_in_head) + ("" if c.closed_in_head else "+")
        parts.append(_tn("cli.hint.candidate", c.items_in_head if c.closed_in_head else 2,
                         path=_display_text(c.text), items=items))
    if len(cands) > CANDIDATES_SHOWN:
        parts.append(ELLIPSIS)
    return ", ".join(parts)


def _record_path_hint(doc: Any, shown: str) -> str:
    cands = list(doc.info.candidates)
    if not cands:
        if doc.info.root_char == "[":
            return t("cli.hint.record_path_root_array")
        return t("cli.hint.record_path_none")
    texts = [c.text for c in cands]
    close = difflib.get_close_matches(shown, texts, n=1, cutoff=0.5)
    example = close[0] if close else texts[0]
    return t("cli.hint.record_path_candidates", candidates=_candidate_list(cands), example=_display_text(example))


def _record_path_error(ctx: _Ctx, doc: Any, err: JsonSyntaxError) -> JsonReshapeError:
    """A record path that matches nothing / a scalar: a user error (exit 1) listing the
    record arrays found at the start of the file -- not a JSON error of the file."""
    given = getattr(ctx.args, "record_path", None)
    shown = given if given is not None else (doc.info.record_path_text or "$")
    shown = _display_text(str(shown))
    hint = _record_path_hint(doc, shown)
    if err.code == "target_not_found":
        return _HintedError("cli.error.record_path_not_found", hint=hint, path=shown)
    return _HintedError("cli.error.record_path_not_container", hint=hint, path=shown)


def _start_warnings(ctx: _Ctx, doc: Any, label: str | None = None) -> None:
    """Format notes of the opened document (automatic record path, non-standard JSON, ...);
    the ones it gains later and the :data:`END_WARNINGS` are printed at the end
    (:func:`_late_warnings`). ``label``: the input's name, put before the texts."""
    prefix = "" if label is None else _display_text(label) + ": "
    for text in _shown_now(ctx, doc):
        ctx.warn(prefix + text)


def _check_index(ctx: _Ctx, doc: Any, *, broken: bool = True, json_hint: str | None = None,
                 label: str | None = None) -> int | None:
    """After indexing: the file must be unchanged; a record path that leads nowhere is a
    user error; a JSON error gives exit code 2 unless ``--allow-partial`` (then a warning);
    broken JSON Lines lines give a warning (``broken=False``: the command reports them itself).
    ``json_hint``: the advice after a JSON error of a command that cannot use ``--allow-partial``
    (default: ``--allow-partial`` with the records before the error). ``label``: the input as
    named on the command line, shown with these messages (commands with several inputs)."""
    prefix = "" if label is None else _display_text(label) + ": "
    doc.ensure_unchanged()
    err = doc.error
    if err is not None:
        if err.code in RECORD_PATH_CODES:
            raise _record_path_error(ctx, doc, err)
        hint = _format_hint(ctx, doc, err)
        if not getattr(ctx.args, "allow_partial", False):
            if label is not None:
                ctx.console.err(t("cli.error.in_file", path=_display_text(label)))
            ctx.console.error(err, hint=hint or json_hint or _partial_hint(doc))
            return EXIT_JSON
        ctx.warn(prefix + _tn("cli.warn.partial", doc.record_count, line=err.line, column=err.column,
                              count=_num(doc.record_count)))
        if hint:
            ctx.console.hint(hint)
    if broken and doc.broken_count:
        ctx.warn(prefix + _broken_lines_text(doc))
    return None


def _check_document(ctx: _Ctx, doc: Any, **kwargs: Any) -> int | None:
    _start_warnings(ctx, doc, kwargs.get("label"))
    return _check_index(ctx, doc, **kwargs)


# ==================================================================================
# inspect
# ==================================================================================
_RECORDS_MARK = "\x00jsonreshape:records\x00"   # stands for the record container inside the wrapper


def _wrapper_data(doc: Any) -> dict[str, Any] | None:
    """The outer wrapper of a wrapped document (AGENT_PROMPT §3.4): sizes of the parts
    before/after the record container and its members as compact JSON (the container
    itself, at any depth, shown as a placeholder). ``members`` is ``None`` when a part is
    larger than :data:`WRAPPER_READ_MAX` or the wrapper cannot be parsed; ``None`` for
    other documents and for a record container that never closed."""
    from . import codec

    if doc.info.kind != "wrapped" or not hasattr(doc, "wrapper_spans"):
        return None
    spans = doc.wrapper_spans()
    if spans is None:
        return None
    (ps, pe), (ss, se) = spans
    data: dict[str, Any] = {"bytes_before": int(pe - ps), "bytes_after": int(se - ss), "members": None}
    if pe - ps > WRAPPER_READ_MAX or se - ss > WRAPPER_READ_MAX:
        return data
    try:
        parts = doc.wrapper_parts()
        if parts is None:
            return data
        mark = codec.dumps(_RECORDS_MARK)
        value = codec.loads(parts[0] + mark + parts[1])
    except (TooLargeError, ValueError, RecursionError) as exc:
        log.info("inspect: the wrapper of %s is not shown: %s", doc.source_path, exc)
        return data
    except JsonReshapeError as exc:
        if isinstance(exc, FileChangedError):
            raise
        log.info("inspect: the wrapper of %s is not shown: %s", doc.source_path, exc)
        return data
    placeholder = "{" + ELLIPSIS + "}" if doc.member_records else "[" + ELLIPSIS + "]"
    mark_text = mark.decode("utf-8")
    if isinstance(value, dict):
        items: Iterable[tuple[Any, Any]] = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return data
    members = []
    for key, member in items:
        if isinstance(member, str) and member == _RECORDS_MARK:
            members.append({"key": key, "records": True, "json": None, "truncated": False})
            continue
        text = codec.dumps(member).decode("utf-8", "replace").replace(mark_text, placeholder)
        cut = len(text) > WRAPPER_JSON_CHARS
        members.append({"key": key, "records": False, "json": _shorten(text, WRAPPER_JSON_CHARS),
                        "truncated": cut})
    data["members"] = members
    return data


def _wrapper_lines(wrapper: dict[str, Any], records: int) -> list[str]:
    members = wrapper["members"]
    if members is None:
        return [t("cli.inspect.wrapper_unread", before=format_bytes(wrapper["bytes_before"]),
                  after=format_bytes(wrapper["bytes_after"]))]
    lines = []
    for m in members[:WRAPPER_MEMBERS_SHOWN]:
        key = m["key"]
        name = f"[{key}]" if isinstance(key, int) else _display_text(str(key))
        if m["records"]:
            value = t("cli.inspect.wrapper_records", count=_num(records))
        else:
            value = _display_text(_shorten(m["json"] or "", WRAPPER_TEXT_CHARS))
        lines.append(f"{name}: {value}")
    if len(members) > WRAPPER_MEMBERS_SHOWN:
        lines.append(ELLIPSIS)
    return lines


def _inspect_data(doc: Any, warnings: list[str] | None = None) -> dict[str, Any]:
    """Everything ``inspect`` reports, with exact values (also the ``--json`` output).
    ``warnings``: the document's warnings shown in the report (default: all of them)."""
    from . import core
    from .fileio import encoding_label

    info = doc.info
    stats = dict(doc.index_stats or {})
    flags = int(doc.flags)
    err = doc.error
    error = None
    if err is not None:
        error = err.to_dict()
        error["message"] = err.message
        error["records_before"] = doc.record_count
    broken = [{"line": b.line, "column": b.column, "code": b.code, "expected": b.expected,
               "message": _broken_line_message(b), "preview": b.preview} for b in doc.broken_lines[:BROKEN_JSON_MAX]]
    return {
        "file": str(doc.source_path),
        "size": int(doc.size),
        "data_size": int(doc.data_size),
        "encoding": info.encoding,
        "encoding_label": encoding_label(info.encoding),
        "compression": info.compression,
        "format": {
            "kind": info.kind,
            "label": info.label,
            "auto": bool(info.auto),
            "record_path": None if info.record_path is None else list(info.record_path),
            "record_path_text": info.record_path_text,
            "member_records": bool(doc.member_records),
            "candidates": [dict(c.to_dict(), text=c.text, items_kind=c.items_kind) for c in info.candidates],
        },
        "wrapper": _wrapper_data(doc),
        "records": int(doc.record_count),
        "index": {
            "complete": bool(doc.index_complete),
            "from_cache": bool(doc.index_from_cache),
            "seconds": stats.get("seconds"),
            "bytes": stats.get("bytes"),
            "mb_per_s": stats.get("mb_per_s"),
            "first_record_seconds": stats.get("first_record_seconds"),
            "backend": stats.get("backend"),
        },
        "core_backend": str(core.BACKEND),
        "flags": {
            "escapes": bool(flags & core.FLAG_ESCAPES),
            "u_escapes": bool(flags & core.FLAG_U_ESCAPES),
            "non_ascii": bool(flags & core.FLAG_NON_ASCII),
        },
        "error": error,
        "broken_lines": {"count": int(doc.broken_count), "first": broken},
        "warnings": info.warning_texts() if warnings is None else list(warnings),
    }


def _yes_no(flag: bool) -> str:
    return t("common.yes") if flag else t("common.no")


def _backend_label(name: str) -> str:
    if name == "rust":
        return t("env.backend.rust")
    if name == "python":
        return t("env.backend.python")
    return t("env.backend.none")


def _items_kind_label(kind: str) -> str:
    return {
        "objects": t("cli.inspect.items.objects"),
        "arrays": t("cli.inspect.items.arrays"),
        "mixed": t("cli.inspect.items.mixed"),
        "values": t("cli.inspect.items.values"),
    }.get(kind, t("cli.inspect.items.empty"))


def _inspect_lines(data: dict[str, Any], doc: Any) -> list[str]:
    rows: list[tuple[str, str]] = [(t("cli.inspect.file"), _display_text(data["file"]))]
    size = _tn("cli.inspect.size_value", data["size"], size=format_bytes(data["size"]), bytes=_num(data["size"]))
    if data["data_size"] != data["size"] or data["compression"]:
        size += "; " + t("cli.inspect.data_size", size=format_bytes(data["data_size"]))
    rows.append((t("cli.inspect.size"), size))
    encoding = data["encoding_label"] + (f", {data['compression']}" if data["compression"] else "")
    rows.append((t("cli.inspect.encoding"), encoding))
    fmt = data["format"]
    mode = t("cli.inspect.auto") if fmt["auto"] else t("cli.inspect.forced")
    rows.append((t("cli.inspect.format"), t("cli.inspect.format_value", label=fmt["label"], mode=mode)))
    if fmt["record_path_text"] is not None:
        rows.append((t("cli.inspect.record_path"), _display_text(fmt["record_path_text"])))
    if data.get("wrapper") is not None:
        rows.append((t("cli.inspect.wrapper"), "\n".join(_wrapper_lines(data["wrapper"], data["records"]))))
    if fmt["candidates"]:
        cands = []
        for c in fmt["candidates"][:CANDIDATES_SHOWN]:
            more = "" if c["closed_in_head"] else "+"
            count = c["items_in_head"] if c["closed_in_head"] else 2
            cands.append(_tn("cli.inspect.candidate", count, path=_display_text(c["text"]),
                             items=_num(c["items_in_head"]) + more, kind=_items_kind_label(c["items_kind"])))
        if len(fmt["candidates"]) > CANDIDATES_SHOWN:
            cands.append(ELLIPSIS)
        rows.append((t("cli.inspect.candidates"), "\n".join(cands)))
    records = _num(data["records"])
    if data["error"] is not None:
        records = t("cli.inspect.records_partial", count=records)
    rows.append((t("cli.inspect.records"), records))
    idx = data["index"]
    if idx["from_cache"]:
        index_text = t("cli.inspect.index_cached")
    elif idx["seconds"] is not None:
        index_text = t("cli.inspect.index_built", seconds=f"{float(idx['seconds']):.2f}",
                       rate=f"{float(idx['mb_per_s'] or 0.0):.1f}")
    else:  # pragma: no cover - the index is always built before inspect reports
        index_text = t("common.unknown")
    rows.append((t("cli.inspect.index"), index_text))
    rows.append((t("cli.inspect.backend"), _backend_label(data["core_backend"])))
    flags = data["flags"]
    rows.append((t("cli.inspect.strings"), t("cli.inspect.strings_value", escapes=_yes_no(flags["escapes"]),
                                                u_escapes=_yes_no(flags["u_escapes"]),
                                                non_ascii=_yes_no(flags["non_ascii"]))))
    err = doc.error
    if err is not None:
        lines = [err.message]
        lines.extend(_detail_lines(err))
        lines.append(_tn("cli.inspect.error_records", doc.record_count, count=_num(doc.record_count)))
        rows.append((t("cli.inspect.error"), "\n".join(lines)))
    if doc.broken_count:
        lines = [t("cli.inspect.broken_value", count=_num(doc.broken_count))]
        for b in doc.broken_lines[:BROKEN_SHOWN]:
            lines.append(t("cli.inspect.broken_line", message=_broken_line_message(b), preview=_display_text(b.preview)))
        if doc.broken_count > BROKEN_SHOWN:
            lines.append(ELLIPSIS)
        rows.append((t("cli.inspect.broken"), "\n".join(lines)))
    if data["warnings"]:
        rows.append((t("cli.inspect.warnings"), "\n".join(data["warnings"])))
    width = max(len(label) for label, _ in rows) + 2
    out: list[str] = []
    for label, value in rows:
        parts = value.split("\n")
        out.append((label + ":").ljust(width) + parts[0])
        out.extend(" " * width + part for part in parts[1:])
    return out


def _cmd_inspect(ctx: _Ctx) -> int:
    from .schema import infer_schema

    args = ctx.args
    sample = args.sample if args.sample is not None else ctx.settings.sample_size
    with _document(ctx) as doc:
        doc.ensure_unchanged()
        err = doc.error
        if err is not None and err.code in RECORD_PATH_CODES:
            raise _record_path_error(ctx, doc, err)
        blocked = err is not None and not args.allow_partial
        data = _inspect_data(doc, _shown_now(ctx, doc))   # later ones and END_WARNINGS: at the end
        schema = None
        if not blocked:
            schema = infer_schema(doc, sample=sample, progress=ctx.stage(t("cli.stage.schema")), cancel=ctx.cancel)
            ctx.end_stage()
        doc.ensure_unchanged()  # the report describes the file as it still is
        if args.json:
            data["schema"] = schema.to_dict() if schema is not None else None
            data["schema_summary"] = schema.summary() if schema is not None else None
            ctx.console.out(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            lines = _inspect_lines(data, doc)
            if schema is not None:
                width = _terminal_columns() - 1 if _isatty(sys.stdout) else None
                lines.extend(["", schema.format_table(width=width), "", schema.summary()])
            ctx.console.out_lines(lines)
        hint = _format_hint(ctx, doc, err) if err is not None else None
        if blocked:
            ctx.console.err(t("cli.error.prefix", message=t("cli.error.json_input")))
            ctx.console.hint(hint or _partial_hint(doc))
            return EXIT_JSON
        if hint:
            ctx.console.hint(hint)
    return EXIT_OK


# ==================================================================================
# search
# ==================================================================================
def _highlight(text: str, spans: Iterable[tuple[int, int]], color: bool) -> str:
    on, off = (HIGHLIGHT_ON, HIGHLIGHT_OFF) if color else (MARK_OPEN, MARK_CLOSE)
    out: list[str] = []
    pos = 0
    n = len(text)
    for s, e in sorted(spans):
        s = max(pos, min(int(s), n))
        e = max(s, min(int(e), n))
        if e <= s:
            continue
        out.append(_display_text(text[pos:s]))
        out.append(on + _display_text(text[s:e]) + off)
        pos = e
    out.append(_display_text(text[pos:]))
    return "".join(out)


class _HitPrinter:
    """Prints hits (``#record  path  [kalit|qiymat]  snippet``) up to ``limit``."""

    def __init__(self, console: _Console, limit: int, *, stop_on_close: bool = True) -> None:
        self.console = console
        self.limit = limit
        self.printed = 0
        self.stop_on_close = stop_on_close
        self.closed = False

    def line(self, hit: Any) -> str:
        where = t("search.where.key") if hit.where == "key" else t("search.where.value")
        return (f"#{hit.record}  {_display_text(hit.path_text)}  [{where}]  "
                f"{_highlight(hit.text, hit.spans, self.console.color)}")

    def add_many(self, hits: Iterable[Any]) -> None:
        if self.closed:
            return
        lines = []
        for hit in hits:
            if self.printed >= self.limit:
                break
            lines.append(self.line(hit))
            self.printed += 1
        if lines:
            try:
                self.console.out_lines(lines)
            except _OutputClosed:
                if self.stop_on_close:
                    raise
                self.closed = True


class _HitCounter:
    """Counts the hits (and distinct records) streamed to an export, printing the first
    ones; ``on_end`` runs when the hits are exhausted -- before the export is committed."""

    def __init__(self, printer: _HitPrinter, on_end: Callable[[], Any] | None = None) -> None:
        self.printer = printer
        self.on_end = on_end
        self.hits = 0
        self.records = 0
        self._last: int | None = None

    def wrap(self, hits: Iterable[Any]) -> Iterator[Any]:
        for hit in hits:
            self.hits += 1
            if hit.record != self._last:
                self.records += 1
                self._last = hit.record
            if self.printer.printed < self.printer.limit:
                self.printer.add_many((hit,))
            yield hit
        if self.on_end is not None:
            self.on_end()


def _hits_export_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in HIT_EXPORT_FORMATS:
        raise UserInputError("search.export.bad_format", fmt=ext or path)
    return ext


def _bytes_mode_possible(doc: Any, opts: Any) -> bool:
    """Would :func:`jsonreshape.search.search` use its byte prefilter (complete index)?"""
    q = opts.query
    if q.mode != "plain" or q.translit or doc.record_count == 0:
        return False
    return bool(q.prefilter_needles(doc.flags))


def _cmd_search(ctx: _Ctx) -> int:
    from .search import SearchHistory, SearchOptions, export_hits, iter_hits, search

    args = ctx.args
    mode = "regex" if args.regex else "number" if args.number else "plain"
    scope = "keys" if args.keys else "values" if args.values else "both"
    opts = SearchOptions.build(args.text, mode=mode, case_sensitive=args.case_sensitive, whole_word=args.whole_word,
                               translit=args.translit, scope=scope, path=args.path)
    export_fmt = None
    if args.export is not None:
        _check_output(args.input, args.export)
        export_fmt = _hits_export_format(args.export)
    show = 0 if args.count else min(args.limit, args.max_hits)
    # printing hits does not wait for the index: search() follows it while it is built
    follow = export_fmt is None and not args.count
    with _document(ctx, build=not follow) as doc:
        _start_warnings(ctx, doc)
        checked = False
        if doc.index_complete:
            rc = _check_index(ctx, doc)
            if rc is not None:
                return rc
            checked = True
        exported = None
        if export_fmt is None:
            printer = _HitPrinter(ctx.console, show)
            result = search(doc, opts, max_hits=show, progress=ctx.stage(t("cli.stage.searching")), cancel=ctx.cancel,
                            on_hits=printer.add_many)
            ctx.end_stage()
            result.close()
            hits, records = result.hit_count, result.record_count
            mode_used, scanned, elapsed = result.mode_used, result.bytes_scanned, result.elapsed
        else:
            printer = _HitPrinter(ctx.console, show, stop_on_close=False)
            counter = _HitCounter(printer, on_end=doc.ensure_unchanged)
            started = time.perf_counter()
            stream = iter_hits(doc, opts, progress=ctx.stage(t("cli.stage.searching")), cancel=ctx.cancel)
            exported = export_hits(counter.wrap(stream), args.export, fmt=export_fmt, cancel=ctx.cancel)
            ctx.end_stage()
            elapsed = time.perf_counter() - started
            hits, records = counter.hits, counter.records
            mode_used = "bytes" if _bytes_mode_possible(doc, opts) else "parse"
            scanned = max(0, int(doc.data_size) - int(doc.info.data_start))
        if not checked:
            # the search followed the index while it was built: its warnings and errors now
            doc.build_index(cancel=ctx.cancel)  # normally complete already; waits for a running build
            rc = _check_index(ctx, doc)
            if rc is not None:
                return rc
        if exported is None:  # an export was checked right before its commit (_HitCounter.on_end)
            doc.ensure_unchanged()
        if args.count:
            ctx.console.out(t("cli.search.count", hits=_hits_text(hits, str(hits)),
                              records=_records_text(records, str(records))))
        rate = scanned / 1e6 / elapsed if elapsed > 0 else 0.0
        ctx.info(t("cli.search.summary", hits=_hits_text(hits, _num(hits)), records=_records_text(records, _num(records)),
                   size=format_bytes(scanned), seconds=format_duration(elapsed), rate=f"{rate:.0f}",
                   mode=t(f"search.mode.{mode_used}")))
        if not args.count and printer.limit > 0 and hits > printer.printed:
            option = "--max-hits N" if args.max_hits < args.limit else "--limit N"
            key = "cli.search.more_export" if exported is not None else "cli.search.more"
            ctx.info(_tn(key, printer.printed, shown=_num(printer.printed), total=_num(hits), option=option))
        if exported is not None:
            ctx.info(_tn("cli.search.exported", exported, count=_num(exported), path=args.export))
    try:
        SearchHistory().add(args.text, opts.to_dict())
    except Exception:  # the history is a convenience: never fail the search because of it
        log.warning("could not save the search history", exc_info=True)
    return EXIT_OK


# ==================================================================================
# filter / sort / convert
# ==================================================================================
#: output option -> the formats it applies to (``--no-wrapper``: every format -- the writer honours
#: ``keep_wrapper`` for JSON Lines, CSV, XLSX and SQLite too and then gives no wrapper notice)
_FORMAT_OPTIONS: dict[str, tuple[str, ...]] = {
    "--compact": ("json",),
    "--indent": ("json",),
    "--sort-keys": ("json", "jsonl"),
    "--no-wrapper": OUTPUT_FORMATS,
    "--as-keyed": ("json",),
    "--columns": ("csv", "xlsx", "parquet"),
    "--array-mode": ("csv", "xlsx", "parquet"),
    "--explode": ("csv", "xlsx", "parquet"),
    "--sep": ("csv", "xlsx", "parquet"),
    "--delimiter": ("csv",),
    "--no-bom": ("csv",),
    "--xlsx-split": ("xlsx",),
    "--parquet-compression": ("parquet",),
}


def _output_format(args: argparse.Namespace, path: str | None = None) -> str:
    """``--jsonl`` / ``--to``, else the format of the output's extension (``path``, default OUTPUT)."""
    from .writer import format_for_path

    to = getattr(args, "to", None)
    if getattr(args, "jsonl", False):
        if to not in (None, "jsonl"):
            raise UserInputError("cli.error.jsonl_conflict", fmt=to)
        return "jsonl"
    if to is not None:
        return to
    output = args.output if path is None else path
    fmt = format_for_path(output)
    if fmt is None:
        raise UserInputError("cli.error.output_format", path=output)
    return fmt


def _split_columns(text: str) -> list[str]:
    """``a,b.c,d["x,y"]`` -> ``["a", "b.c", 'd["x,y"]']`` (commas inside brackets or
    quotes do not split)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
            buf.append(ch)
        elif ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _given_output_options(args: argparse.Namespace) -> list[str]:
    """The output options given on the command line (a command may have only some of them)."""
    def value(name: str) -> Any:
        return getattr(args, name, None)

    given = {
        "--compact": bool(value("compact")),
        "--indent": value("indent") is not None,
        "--sort-keys": value("sort_keys") is not None,
        "--no-wrapper": bool(value("no_wrapper")),
        "--as-keyed": bool(value("as_keyed")),
        "--columns": value("columns") is not None,
        "--array-mode": value("array_mode") is not None,
        "--explode": value("explode") is not None,
        "--sep": value("sep") is not None,
        "--delimiter": value("delimiter") is not None,
        "--no-bom": bool(value("no_bom")),
        "--xlsx-split": value("xlsx_split") is not None,
        "--parquet-compression": value("parquet_compression") is not None,
    }
    return [name for name, on in given.items() if on]


def _ignored_for_format(args: argparse.Namespace, fmt: str) -> list[str]:
    """The given output options that do not apply to the format ``fmt``."""
    return [name for name in _given_output_options(args) if fmt not in _FORMAT_OPTIONS[name]]


def _export_options(ctx: _Ctx, fmt: str) -> Any:
    from .writer import ExportOptions

    args = ctx.args

    def value(name: str) -> Any:
        return getattr(args, name, None)

    overrides: dict[str, Any] = {}
    if value("compact"):
        overrides["indent"] = None
    elif value("indent") is not None:
        overrides["indent"] = args.indent
    if value("sort_keys") is not None:
        overrides["sort_keys"] = args.sort_keys
    if value("no_wrapper"):
        overrides["keep_wrapper"] = False
    if value("as_keyed"):
        if fmt != "json":   # a user error, not an option that is ignored with a warning
            raise UserInputError("cli.error.as_keyed_format", fmt=fmt)
        overrides["as_keyed"] = True
    if value("columns") is not None:
        overrides["columns"] = _split_columns(args.columns)
    if value("array_mode") is not None:
        overrides["array_mode"] = args.array_mode
    if value("explode") is not None:
        overrides["explode"] = args.explode
    if value("sep") is not None:
        overrides["join_sep"] = args.sep
    if value("delimiter") is not None:
        overrides["csv_delimiter"] = args.delimiter
    if value("no_bom"):
        overrides["csv_bom"] = False
    if value("xlsx_split") is not None:
        overrides["xlsx_split"] = args.xlsx_split
    if value("parquet_compression") is not None:
        overrides["parquet_compression"] = args.parquet_compression
    try:
        return ExportOptions.from_settings(fmt, ctx.settings, **overrides)
    except UserInputError as exc:
        # a writer without key orders takes only a bool (its one order: code points)
        if exc.key != "writer.invalid_option" or exc.params.get("name") != "sort_keys" \
                or not isinstance(overrides.get("sort_keys"), str):
            raise
        log.warning("this writer has no key orders: --sort-keys %s sorts by code point", overrides["sort_keys"])
        overrides["sort_keys"] = True
        return ExportOptions.from_settings(fmt, ctx.settings, **overrides)


def _presets() -> Any:
    """The filter preset store (:class:`jsonreshape.query.FilterPresets`)."""
    from . import query

    store_cls = getattr(query, "FilterPresets", None)
    if store_cls is None:
        raise UserInputError("cli.error.presets_unavailable")
    return store_cls()


def _filter_source(args: argparse.Namespace) -> tuple[str, str] | None:
    """``(text, lang)`` of the filter given by ``--where`` / ``--jmespath`` / ``--preset``."""
    if getattr(args, "jmespath", None) is not None:
        return args.jmespath, "jmespath"
    if getattr(args, "where", None) is not None:
        return args.where, "expr"
    name = getattr(args, "preset", None)
    if name is not None:
        try:
            preset = _presets().get(name)
        except UserInputError as exc:
            if exc.key != "query.preset.not_found":
                raise
            raise UserInputError("cli.error.preset_not_found", name=name) from exc
        return str(preset["text"]), str(preset.get("lang") or "expr")
    return None


_JMES_COMPARATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})


def _jmespath_warnings(text: str) -> list[str]:
    """A double-quoted name right after ``==``, ``<`` etc. is a *field* in JMESPath, not a
    text (``city == "Toshkent"`` compares with the field Toshkent and matches nothing). On
    the left side (``"first name" == 'Ali'``) a quoted name is the usual way to write a field."""
    try:
        from jmespath import lexer

        tokens = list(lexer.Lexer().tokenize(text))
    except Exception:  # the expression compiled: never fail on this extra check
        return []
    out = []
    for i, tok in enumerate(tokens):
        if i > 0 and tok.get("type") == "quoted_identifier" and tokens[i - 1].get("type") in _JMES_COMPARATORS:
            out.append(t("cli.warn.jmespath_quoted", name=_display_text(str(tok.get("value")))))
    return out


def _compile_filter(ctx: _Ctx) -> Any:
    """The filter of ``--where`` / ``--jmespath`` / ``--preset`` (``None`` without one);
    ``--save-preset NAME`` stores it once it compiled."""
    from .query import compile_filter

    args = ctx.args
    source = _filter_source(args)
    save_as = getattr(args, "save_preset", None)
    if source is None:
        if save_as is not None:
            raise UserInputError("cli.error.save_preset_needs_filter")
        return None
    text, lang = source
    try:
        flt = compile_filter(text, lang=lang)
    except QuerySyntaxError as exc:
        if lang == "jmespath":
            _set_hint(exc, t("cli.hint.jmespath"))
        raise
    if lang == "jmespath":
        for warning in _jmespath_warnings(text):
            ctx.warn(warning)
    if save_as is not None:
        _presets().save(save_as, text, lang=lang)
        ctx.info(t("cli.preset.saved", name=save_as))
    return flt


def _sort_keys(args: argparse.Namespace) -> list[Any]:
    from .sortkeys import SortKey

    fields = getattr(args, "sort_fields", None) or []
    options = {"nulls": args.nulls, "collation": args.collation, "natural": not args.no_natural,
               "case_sensitive": bool(args.case_sensitive)}
    return [SortKey.parse(text, **options) for text in fields]


def _sort_memory(ctx: _Ctx) -> int:
    """Bytes of sort keys kept in memory: ``--memory-limit`` (raised to at least
    :data:`MIN_MEMORY_LIMIT`) or ``Settings.sort_memory_bytes``, and never more than half
    of ``Settings.memory_limit_bytes`` -- the overall memory budget."""
    settings = ctx.settings
    limit = int(settings.memory_limit_bytes)
    budget = max(MIN_MEMORY_LIMIT, limit // 2)
    given = getattr(ctx.args, "memory_limit", None)
    if given is None:
        return min(int(settings.sort_memory_bytes), budget)
    if given < MIN_MEMORY_LIMIT:
        ctx.warn(t("cli.warn.memory_limit_min", value=_mem_text(given), minimum=_mem_text(MIN_MEMORY_LIMIT)))
        given = MIN_MEMORY_LIMIT
    if given > budget:
        ctx.warn(t("cli.warn.memory_limit_cap", value=_mem_text(given), cap=_mem_text(budget),
                   limit=_mem_text(limit), setting=t("settings.field.memory_limit_bytes")))
        given = budget
    return given


SELECT_CHECK_COUNT = 1 << 62   # a record count no file has: --select syntax checked before INPUT is read


def _check_select(args: argparse.Namespace) -> None:
    """``--select SPEC`` checked before any work (a typo must not wait for the index of a big file):
    each comma item alone (one item gives a ``range``, never a materialized list); positions beyond the
    end of the file can only be told once it is indexed (:func:`_scope_source`)."""
    from .filetools import parse_select

    spec = getattr(args, "select", None)
    if spec is None:
        return
    items = spec.split(",")
    if not spec.strip() or any(not item.strip() for item in items):
        raise UserInputError("filetools.error.select_syntax", spec=spec.strip(), item="")
    for item in items:
        parse_select(item, SELECT_CHECK_COUNT)


def _compile_scope(ctx: _Ctx) -> Any:
    """The record scope of a command: ``--select`` checked, the filter compiled (``None`` without one)."""
    _check_select(ctx.args)
    return _compile_filter(ctx)


def _scope_source(ctx: _Ctx, doc: Any, flt: Any, sets: list[Any]) -> Any:
    """The records a command works on: ``--select SPEC`` (:func:`jsonreshape.filetools.parse_select`:
    0-based, in the given order), then the filter over them (a RecordSet in that order, appended to
    ``sets`` -- the caller closes it); ``None`` = every record."""
    from .filetools import parse_select
    from .recordset import filter_records

    source: Any = None
    spec = getattr(ctx.args, "select", None)
    if spec is not None:
        source = parse_select(spec, int(doc.record_count))
    if flt is not None:
        rs = filter_records(doc, flt, source, progress=ctx.stage(t("cli.stage.filtering")), cancel=ctx.cancel)
        sets.append(rs)
        ctx.end_stage()
        for text in rs.report.warning_texts():
            ctx.warn(text)
        source = rs
    return source


def _scope_size(doc: Any, source: Any) -> int:
    return int(doc.record_count) if source is None else len(source)


def _close_all(sets: Iterable[Any]) -> None:
    for rs in sets:
        try:
            rs.close()
        except Exception:  # pragma: no cover - a record set that cannot be released must not hide the result
            log.debug("could not close a record set", exc_info=True)


def _export_warnings(ctx: _Ctx, report: Any) -> None:
    """The writer's warnings, except the partial / broken-line ones given after indexing already."""
    for key, text in zip(report.warning_keys, report.warnings):
        if key not in ("writer.warn.partial", "writer.warn.broken_lines"):
            ctx.warn(text)


def _check_values_format(fmt: str) -> None:
    """``--node`` / ``--gather`` write JSON or JSON Lines only: refused before INPUT is opened."""
    from .writer import FORMAT_NAMES

    if fmt not in ("json", "jsonl"):
        raise UserInputError("filetools.error.extract_format", fmt=FORMAT_NAMES.get(fmt, fmt))


def _values_export_options(ctx: _Ctx, fmt: str, mode: str) -> Any:
    """Writer options for values (``--node`` / ``--gather``): only the JSON layout (``--gather``: and
    ``--sort-keys``) applies; other output options given are named in a warning."""
    from .writer import ExportOptions

    args = ctx.args
    applicable = {"--compact", "--indent"} if fmt == "json" else set()
    if mode == "--gather":
        applicable.add("--sort-keys")
    ignored = [name for name in _given_output_options(args) if name not in applicable]
    if ignored:
        ctx.warn(t("cli.warn.ignored_mode", options=", ".join(ignored), mode=mode, format=fmt))
    overrides: dict[str, Any] = {}
    if fmt == "json":
        if getattr(args, "compact", False):
            overrides["indent"] = None
        elif getattr(args, "indent", None) is not None:
            overrides["indent"] = args.indent
    if "--sort-keys" in applicable and getattr(args, "sort_keys", None) is not None:
        overrides["sort_keys"] = args.sort_keys
    return ExportOptions.from_settings(fmt, ctx.settings, **overrides)


def _write_node(ctx: _Ctx, doc: Any, output: str, opts: Any, location: str) -> Any:
    """One node of ``doc`` (a location text) -> ``output`` (:func:`jsonreshape.filetools.extract`)."""
    from .filetools import extract

    doc.ensure_unchanged()
    report = extract(doc, output, opts, node=location, progress=ctx.stage(t("cli.stage.extracting")),
                     cancel=ctx.cancel)
    ctx.end_stage()
    _export_warnings(ctx, report)
    ctx.info(t("cli.summary.node", node=_display_text(location), output=output,
               size=format_bytes(report.bytes_written), seconds=format_duration(ctx.elapsed())))
    log.info("%s: node %s -> %s (%d bytes) in %.2f s", ctx.args.command, location, output, report.bytes_written,
             ctx.elapsed())
    return report


def _export_node(ctx: _Ctx) -> int:
    """``convert INPUT OUTPUT --node LOCATION``: one node of INPUT (JSON / JSON Lines); a filter,
    ``--select`` or ``--sort-by`` cannot be combined with it."""
    args = ctx.args
    combined = [name for name, given in (("--where", args.where), ("--jmespath", args.jmespath),
                                         ("--preset", args.preset), ("--save-preset", args.save_preset),
                                         ("--select", args.select), ("--sort-by", args.sort_fields))
                if given is not None]
    if combined:
        raise UserInputError("cli.error.node_combined", options=", ".join(combined))
    _check_output(args.input, args.output)
    fmt = _output_format(args)
    _check_values_format(fmt)
    opts = _values_export_options(ctx, fmt, "--node")
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        _write_node(ctx, doc, args.output, opts, args.node)
    return EXIT_OK


def _cmd_export(ctx: _Ctx) -> int:
    """filter / sort / convert: [select] -> [filter] -> [sort] -> export, with a summary line;
    ``convert --node``: one node (:func:`_export_node`)."""
    from .recordset import sort_records
    from .writer import export

    args = ctx.args
    if getattr(args, "node", None) is not None:
        return _export_node(ctx)
    _check_output(args.input, args.output)
    fmt = _output_format(args)
    opts = _export_options(ctx, fmt)
    ignored = _ignored_for_format(args, fmt)
    flt = _compile_scope(ctx)
    keys = _sort_keys(args) if args.command in ("sort", "convert") else []
    memory = _sort_memory(ctx) if keys else None
    if ignored:
        ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format=fmt))
    if flt is not None:
        for text in flt.warnings:
            ctx.warn(text)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        total = doc.record_count
        sets: list[Any] = []
        try:
            selection = _scope_source(ctx, doc, flt, sets)
            if keys:
                selection = sort_records(doc, keys, selection, memory_limit=memory,
                                         progress=ctx.stage(t("cli.stage.sorting")), cancel=ctx.cancel)
                sets.append(selection)
                ctx.end_stage()
                for text in selection.report.warning_texts():
                    ctx.warn(text)
            doc.ensure_unchanged()  # nothing is written from a file that changed meanwhile
            report = export(doc, args.output, opts, source=selection, progress=ctx.stage(t("cli.stage.writing")),
                            cancel=ctx.cancel)
            ctx.end_stage()
        finally:
            _close_all(sets)
        # the partial/broken-line warnings were already given after indexing
        _export_warnings(ctx, report)
        _export_summary(ctx, report, total, fmt, args.output)
    return EXIT_OK


def _export_summary(ctx: _Ctx, report: Any, total: int, fmt: str, output: str) -> None:
    """``{selected} / {total} records -> OUTPUT (size, time)`` (+ rows of a table format, files)."""
    shown = str(report.files[0]) if len(report.files) > 1 else output
    # "{selected} / {total} records": the noun goes with the total
    summary = _tn("cli.summary.export", total, selected=_num(report.records), total=_num(total), output=shown,
                  size=format_bytes(report.bytes_written), seconds=format_duration(ctx.elapsed()))
    extra = []
    if fmt in ("csv", "xlsx", "sqlite", "parquet") and report.rows != report.records:
        extra.append(_tn("cli.summary.rows", report.rows, rows=_num(report.rows)))
    if len(report.files) > 1:
        extra.append(_tn("cli.summary.files", len(report.files), count=_num(len(report.files))))
    if extra:
        summary += "; " + ", ".join(extra)
    ctx.info(summary)
    log.info("%s: %d of %d records -> %s (%s, %d bytes) in %.2f s", ctx.args.command, report.records, total, shown,
             fmt, report.bytes_written, ctx.elapsed())


# ==================================================================================
# patch
# ==================================================================================
def _cmd_patch(ctx: _Ctx) -> int:
    """patch: apply a JSON Patch (RFC 6902) to INPUT and save the result in INPUT's kind and encoding --
    to OUTPUT, or in place (``--in-place``, ``.bak`` unless ``--no-backup``). The edit layer of the run lives
    in a temporary folder under the work folder (never the user's pending layers), deleted at the end."""
    import secrets as _secrets

    from .appdirs import work_dir
    from .fileio import long_path

    args = ctx.args
    if bool(args.in_place) == (args.output is not None):
        raise UserInputError("cli.error.patch_output")
    if args.no_backup and not args.in_place:
        raise UserInputError("cli.error.no_backup")
    _check_input(args.patch)
    if args.output is not None:
        _check_output(args.input, args.output)
    elif _read_only_file(args.input):
        raise UserInputError("fileio.error.read_only", path=args.input)
    layer_dir = work_dir(ctx.settings.cache_dir) / f"patch-{os.getpid()}-{_secrets.token_hex(4)}"
    try:
        with _document(ctx) as doc:
            rc = _check_document(ctx, doc)
            if rc is not None:
                return rc
            if doc.error is not None:            # --allow-partial: the records before the error cannot be patched
                from .errors import EditError

                ctx.console.error(EditError("edits.error.partial"))
                return EXIT_JSON
            return _patch_document(ctx, doc, layer_dir)
    finally:
        # after the document closed its handles on the layer's files
        shutil.rmtree(long_path(layer_dir), ignore_errors=True)


def _patch_document(ctx: _Ctx, doc: Any, layer_dir: Any) -> int:
    """The patch run on the opened, indexed INPUT (see :func:`_cmd_patch`)."""
    from . import jsonpatch
    from .edits import EditSession
    from .editstore import EditStore, LayerLock

    args = ctx.args
    lock = LayerLock.for_doc(doc)
    lock.acquire()
    try:
        session = EditSession.open(doc, settings=ctx.settings, lock=lock, restore=False)
    except BaseException:
        lock.release()                       # a lock given to a failing open stays ours
        raise
    try:
        session.store = EditStore.open(doc, settings=ctx.settings, dir=layer_dir, lock=lock)
        stats: dict[str, Any] = {}
        jsonpatch.apply_patch(session, args.patch, progress=ctx.stage(t("cli.stage.patching")), cancel=ctx.cancel,
                              stats=stats)
        ctx.end_stage()
        ops = int(stats.get("ops", 0))
        if args.in_place and not stats.get("changed"):
            ctx.info(_tn("cli.summary.patch_unchanged", ops, ops=_num(ops), output=args.input))
            return EXIT_OK
        try:
            report = session.save(None if args.in_place else args.output, backup=not args.no_backup,
                                  drop_broken=bool(args.allow_partial), progress=ctx.stage(t("cli.stage.writing")),
                                  cancel=ctx.cancel)
        except JsonReshapeError as exc:
            if exc.key == "saver.error.broken_lines":
                _set_hint(exc, t("cli.hint.patch_broken_lines"))
            raise
        ctx.end_stage()
        for text in report.warnings:
            ctx.warn(text)
        ctx.info(_tn("cli.summary.patch", ops, ops=_num(ops), output=str(report.target),
                     size=format_bytes(report.bytes_written), seconds=format_duration(ctx.elapsed())))
        if report.backup is not None:
            ctx.info(t("cli.summary.backup", path=str(report.backup)))
        log.info("patch: %d operations, %d records written -> %s (%d bytes) in %.2f s", ops,
                 int(stats.get("records", 0)), report.target, report.bytes_written, ctx.elapsed())
        return EXIT_OK
    finally:
        if not session.closed:
            try:
                session.discard()            # the temporary layer is deleted
            finally:
                session.close()


# ==================================================================================
# apply / flatten (structure builder, docs/ARCHITECTURE.md §13.4)
# ==================================================================================
def _apply_options(ctx: _Ctx) -> Any:
    """The :class:`jsonreshape.engine.ApplyOptions` of ``apply`` / ``flatten``: JSON Lines with ``--jsonl`` or an
    OUTPUT named ``.jsonl`` / ``.ndjson``; the indent of ``--compact`` / ``--indent``, else the "export indent"
    setting (JSON Lines are compact: the two options are ignored with a warning); ``--skip-huge``; ``--workers``.
    An OUTPUT named like another export format (``.csv``, ``.xlsx``, ``.sqlite``) or ``.gz`` is refused: the
    engine writes plain JSON."""
    from .engine import ApplyOptions
    from .writer import format_for_path, gzip_path

    args = ctx.args
    if gzip_path(args.output):
        raise UserInputError("cli.error.mapping_gzip", path=args.output)
    fmt = format_for_path(args.output)
    if fmt not in (None, "json", "jsonl"):
        raise UserInputError("cli.error.mapping_output_format", path=args.output, fmt=fmt)
    jsonl = bool(args.jsonl) or fmt == "jsonl"
    indent: Any
    if jsonl:
        ignored = [name for name, given in (("--compact", args.compact), ("--indent", args.indent is not None))
                   if given]
        if ignored:
            ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format="jsonl"))
        indent = None
    elif args.compact:
        indent = None
    elif args.indent is not None:
        indent = args.indent
    else:
        indent = ctx.settings.export_indent
    return ApplyOptions(jsonl=jsonl, indent=indent, skip_huge=bool(args.skip_huge), workers=args.workers)


def _stored_record_path(rp: Any) -> tuple | None:
    """``input.record_path`` of a mapping file as a record path (keys and indexes), or ``None``."""
    if not isinstance(rp, list) or not rp:
        return None
    for seg in rp:
        if isinstance(seg, bool) or not isinstance(seg, (str, int)) or (isinstance(seg, int) and seg < 0):
            return None
    return tuple(rp)


def _mapping_record_path(ctx: _Ctx, tree: Any, info: dict) -> bool:
    """``apply`` without ``--record-path``: a mapping made for an array inside an object stores where its records
    were (``input.record_path``), and INPUT is opened with that record path -- unless ``--format`` names another
    kind or INPUT is detected as JSON Lines / several values (where a record path is refused; the kind check
    then says what does not fit). Returns whether the stored record path is used."""
    from .paths import format_path
    from .reader import detect_format

    args = ctx.args
    if args.record_path is not None or getattr(tree, "root_kind", None) != "wrapped":
        return False
    if args.format not in (None, "auto", "wrapped"):
        return False
    rp = _stored_record_path(info.get("record_path"))
    if rp is None:
        return False
    if args.format in (None, "auto"):
        detected = detect_format(args.input, settings=ctx.settings, progress=ctx.stage(t("cli.stage.preparing")),
                                 cancel=ctx.cancel)
        ctx.end_stage()
        if detected.kind in ("jsonl", "stream"):
            return False
    args.record_path = format_path(rp)
    log.info("apply: INPUT is opened with the record path stored in the mapping: %s", args.record_path)
    return True


def _check_mapping_kind(doc: Any, tree: Any, info: dict) -> None:
    """:func:`jsonreshape.engine.check_kind` (the mapping was made for another kind of document: exit 1); when the
    mapping was made from a keyed, single-value or wrapped file, the hint names the ``--format`` (and
    ``--record-path``) that reads INPUT the same way."""
    from . import engine
    from .mapping import MappingError
    from .paths import format_path

    try:
        engine.check_kind(doc, tree)
    except MappingError as exc:
        kind = info.get("kind")
        if isinstance(kind, str) and kind in MAPPING_FORMAT_KINDS and kind != doc.kind:
            option = f"--format {kind}"
            rp = _stored_record_path(info.get("record_path"))
            if kind == "wrapped" and rp is not None:
                option += " --record-path " + _display_text(format_path(rp))
            _set_hint(exc, t("cli.hint.mapping_kind", label=t(f"reader.kind.{kind}"), option=option))
        raise


def _run_apply(ctx: _Ctx, doc: Any, tree: Any, opts: Any) -> Any:
    """:func:`jsonreshape.engine.apply` of ``tree`` to ``doc`` -> OUTPUT, with the progress line."""
    from .engine import apply

    doc.ensure_unchanged()  # nothing is written from a file that changed meanwhile (apply checks again at the end)
    report = apply(doc, tree, ctx.args.output, opts, progress=ctx.stage(t("cli.stage.applying")), cancel=ctx.cancel)
    ctx.end_stage()
    return report


def _report_apply(ctx: _Ctx, report: Any, *, unmapped_hint: str | None = None) -> None:
    """The engine's warnings (not those given after indexing already; ``unmapped_hint`` after the unmapped-fields
    one) and the summary line: records -> items -> OUTPUT (size, time, input MB/s; passes, processes)."""
    for key, text in zip(report.warning_keys, report.warnings):
        if key in ENGINE_SHOWN_WARNINGS:
            continue
        ctx.warn(text)
        if key == "engine.warn.unmapped" and unmapped_hint:
            ctx.console.hint(unmapped_hint)
    ctx.info(t("cli.summary.apply",
               records=_tn("cli.count.records", report.records_in, count=_num(report.records_in)),
               items=_tn("cli.count.items", report.items_out, count=_num(report.items_out)),
               output=ctx.args.output, size=format_bytes(report.bytes_written),
               seconds=format_duration(ctx.elapsed()), rate=f"{report.mb_per_s:.1f}",
               passes=_tn("cli.count.passes", report.passes, count=_num(report.passes)),
               workers=_tn("cli.count.workers", report.workers, count=_num(report.workers))))
    log.info("%s: %d records -> %d items -> %s (%d bytes, %d pass(es), %d process(es)) in %.2f s", ctx.args.command,
             report.records_in, report.items_out, ctx.args.output, report.bytes_written, report.passes, report.workers,
             ctx.elapsed())


def _cmd_apply(ctx: _Ctx) -> int:
    """apply: the mapping file MAPPING over the whole INPUT -> OUTPUT (JSON or JSON Lines), streamed."""
    from .mapping import load_mapping

    args = ctx.args
    _check_input(args.mapping)
    _check_input(args.input)
    _check_output(args.input, args.output)
    if _same_file(args.mapping, args.output):
        raise UserInputError("cli.error.output_is_mapping", path=args.output)
    opts = _apply_options(ctx)
    tree, info = load_mapping(args.mapping)
    from_mapping = _mapping_record_path(ctx, tree, info)
    with _document(ctx) as doc:
        try:
            rc = _check_document(ctx, doc)
        except _HintedError as exc:
            if from_mapping and exc.key in ("cli.error.record_path_not_found", "cli.error.record_path_not_container"):
                raise _HintedError("cli.error.mapping_record_path", hint=exc.hint, **exc.params) from exc
            raise
        if rc is not None:
            return rc
        _check_mapping_kind(doc, tree, info)
        report = _run_apply(ctx, doc, tree, opts)
        _report_apply(ctx, report)
    return EXIT_OK


class _Inferred:
    """The default mapping of INPUT for ``flatten``: the designer over the inferred tree, the number of record
    values the tree was inferred from, the number of records, and whether every record was read."""

    __slots__ = ("designer", "values", "total", "full")

    def __init__(self, designer: Any, values: int, total: int, full: bool) -> None:
        self.designer = designer
        self.values = values
        self.total = total
        self.full = full


def _record_values(ctx: _Ctx, doc: Any, source: Any, label: str) -> Iterator[Any]:
    """The parsed values of the records ``source`` (positions; ``None``: every record) of the pinned ``doc``
    (:func:`mapping.document_values`: keyed documents the member values; exact numbers; huge records left out)
    under the progress line ``label`` (bytes of the records read)."""
    from .mapping import document_values

    progress = ctx.stage(label)
    if progress is not None and source is not None:
        progress.set_total(sum(e - s for s, e in map(doc.record_span, source)))
    try:
        yield from document_values(doc, source, progress=progress, cancel=ctx.cancel)
    finally:
        ctx.end_stage()


def _infer_designer(ctx: _Ctx, doc: Any) -> _Inferred:
    """The default tree of INPUT (docs/ARCHITECTURE.md §13.0 "Tree source"; :func:`mapping.infer_document`, the
    same inference as the GUI's structure builder) in a :class:`mapping.Designer` whose recursive flatten
    re-infers from the same records: ``--sample N`` records at :func:`jsonreshape.schema.sample_positions`
    (default ``Settings.sample_size``) or every record (``--full-scan``). Wrapped documents add the wrapper (parsed
    with an empty record array), keyed ones map the member values, a single document its value (at most
    ``Settings.large_node_bytes``)."""
    from . import mapping

    args = ctx.args
    pdoc = doc.pinned()
    sample = None if args.full_scan else (args.sample if args.sample is not None else int(ctx.settings.sample_size))
    progress = ctx.stage(t("cli.stage.inferring"))
    try:
        inferred = mapping.infer_document(pdoc, sample=sample, progress=progress, cancel=ctx.cancel)
    finally:
        ctx.end_stage()
    source = inferred.source

    def population() -> Iterator[Any]:
        return _record_values(ctx, pdoc, source, t("cli.stage.flattening"))

    designer = mapping.Designer(inferred.tree, population=population, wrapper=inferred.wrapper,
                                record_path=inferred.record_path)
    return _Inferred(designer, inferred.values, inferred.total, inferred.full)


def _record_array(tree: Any) -> Any:
    """The array node of the records (what ``--mode records`` flattens): the root of a records tree or of a
    single value that is an array, the record array of a wrapped tree; ``None`` otherwise (keyed)."""
    root = tree.root
    if tree.root_kind == "records" or (tree.root_kind == "value" and root.kind == "array"):
        return root
    if tree.root_kind == "wrapped" and tree.record_src is not None:
        want = tuple(tree.record_src)
        for n in tree.iter_nodes():
            if n.kind == "array" and not n.is_elem and n.src is not None and tuple(n.src) == want:
                return n
    return None


def _record_level(tree: Any) -> list[Any]:
    """The nodes that stand for one record (``--mode nested`` looks for KEY among their children): the element
    nodes of the record array, else the root (the member value of a keyed tree, a single value)."""
    arr = _record_array(tree)
    if arr is not None:
        return list(arr.children)
    return [] if tree.root_kind == "wrapped" else [tree.root]


def _record_field(level: Sequence[Any], key: str) -> Any:
    """The node of ``level`` read from the record field ``key`` (by its source path), or ``None``."""
    for r in level:
        if r.kind != "object" or r.src is None:
            continue
        base = tuple(r.src)
        for c in r.children:
            if not c.is_elem and c.src is not None and len(c.src) == len(base) + 1 and tuple(c.src[:-1]) == base \
                    and c.src[-1] == key:
                return c
    return None


def _types_text(types: Iterable[str]) -> str:
    names = [t(f"mapping.type.{ty}") for ty in types if has(f"mapping.type.{ty}")]
    return ", ".join(names) if names else t("mapping.type.empty")


def _sample_hint(inf: _Inferred) -> str | None:
    """For a tree inferred from a sample: the advice to infer it from every record (``--full-scan``)."""
    if inf.full:
        return None
    return _tn("cli.hint.flatten_sample", inf.total, count=_num(inf.values), total=_num(inf.total))


def _not_recursive(inf: _Inferred, key: str, reason: str) -> JsonReshapeError:
    return _HintedError("cli.error.not_recursive", hint=_sample_hint(inf), key=_display_text(key),
                        reason=reason.rstrip(". "))


def _missing_field(inf: _Inferred, level: Sequence[Any], key: str) -> JsonReshapeError:
    """KEY is not a field of the records: the hint names their array fields (and ``--full-scan`` for a sample)."""
    objects = [r for r in level if r.kind == "object"]
    if not objects:
        parts = [t("cli.hint.records_not_objects")]
    else:
        arrays = list(dict.fromkeys(_display_text(c.key) for r in objects for c in r.children
                                    if c.kind == "array" and not c.is_elem))
        parts = [t("cli.hint.record_arrays", fields=", ".join(arrays)) if arrays else t("cli.hint.record_no_arrays")]
    sample = _sample_hint(inf)
    if sample:
        parts.append(sample)
    return _HintedError("cli.error.children_key_missing", hint=" ".join(parts), key=_display_text(key))


def _flatten_target(ctx: _Ctx, doc: Any, inf: _Inferred, key: str) -> Any:
    """The array node ``flatten`` flattens, checked (see the module docstring): ``--mode nested`` -- the record
    field KEY, whose items must hold a nested KEY array of objects; ``--mode records`` -- the record array, KEY
    being a record field that is an array of objects (the record's other recursive arrays are removed first so
    that the reference ``findRec`` picks KEY; the flatten re-infers them)."""
    from .mapping import find_rec

    d = inf.designer
    tree = d.tree
    records_mode = ctx.args.mode == "records"
    if records_mode:
        target = _record_array(tree)
        if target is None:
            raise UserInputError("cli.error.flatten_records_mode", format=doc.info.label)
        level = list(target.children)
    else:
        target = None
        level = _record_level(tree)
    field = _record_field(level, key)
    if field is None:
        raise _missing_field(inf, level, key)
    if field.kind != "array":
        raise _not_recursive(inf, key, t("cli.reason.not_array", types=_types_text(field.types)))
    if not any(e.kind == "object" for e in field.children):
        types = [ty for e in field.children for ty in e.types]
        if not types:
            raise _not_recursive(inf, key, t("cli.reason.always_empty"))
        raise _not_recursive(inf, key, t("cli.reason.items_not_objects", types=_types_text(dict.fromkeys(types))))
    if not records_mode:
        recs = [find_rec(field, e) for e in field.children]
        if not any(c is not None for c in recs) or any(c is not None and c.src[-1] != key for c in recs):
            raise _not_recursive(inf, key, t("cli.reason.no_nested", key=_display_text(key)))
        return field
    assert target is not None
    tid = target.id
    for eid in [e.id for e in target.children]:
        while True:
            c = find_rec(tree.find(tid), tree.find(eid))
            if c is None or c.src[-1] == key:
                break
            log.info("flatten: the recursive array %r is removed before flattening by %r", c.key, key)
            if not d.delete(c).applied:  # pragma: no cover - an ordinary field can always be removed
                break
    return tree.find(tid)


def _depth_field_name(args: argparse.Namespace) -> str:
    """``--depth-field`` trimmed like a rename (ECMAScript white space); empty: exit 1."""
    from .mapping import js_trim

    if args.depth_field is None:
        return DEFAULT_DEPTH_FIELD
    name = js_trim(args.depth_field)
    if not name:
        raise UserInputError("cli.error.depth_field_empty")
    return name


def _flatten_tree(ctx: _Ctx, inf: _Inferred, target: Any, key: str, depth_name: str) -> list[str]:
    """One recursive flatten of ``target`` by KEY (every flattened element must use KEY), then the level node
    renamed to ``depth_name`` (a name the data uses gets ``_2`` ... with a warning). Returns the level names."""
    d = inf.designer
    plan = d.plan_flatten(target, cancel=ctx.cancel)
    if plan is None or not plan.entries or any(entry[1] != key for entry in plan.entries):
        raise _not_recursive(inf, key, t("cli.reason.no_nested", key=_display_text(key)))
    res = d.flatten(target, plan=plan)
    if not res.applied:
        raise _not_recursive(inf, key, res.reason or t("mapping.why.not_flattenable"))
    log.info("flatten: %s", res.message)
    names: list[str] = []
    for entry in plan.entries:
        elem = d.tree.find(entry[0].id)
        if elem is None:  # pragma: no cover - the flattened element stays in the tree
            continue
        level = next((c for c in elem.children if c.computed == "depth" and c.ctx == tuple(elem.src)), None)
        if level is None:  # pragma: no cover - flatten always adds it
            continue
        if level.key != depth_name:
            d.rename(level.id, depth_name)
            level = d.tree.find(level.id)
        if level.key != depth_name:
            ctx.warn(t("cli.warn.depth_field_taken", name=_display_text(depth_name), new=_display_text(level.key)))
        names.append(level.key)
    return names


def _cmd_flatten(ctx: _Ctx) -> int:
    """flatten: the default tree of INPUT with one recursive flatten by KEY, applied to the whole INPUT."""
    from . import engine

    args = ctx.args
    key = args.children_key
    _check_input(args.input)
    _check_output(args.input, args.output)
    depth_name = _depth_field_name(args)
    opts = _apply_options(ctx)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        inf = _infer_designer(ctx, doc)
        done = None
        if inf.values == 0:
            if inf.total == 0:   # (records that are all huge: the apply stops at them or skips them)
                ctx.warn(t("cli.warn.flatten_no_records"))
        else:
            target = _flatten_target(ctx, doc, inf, key)
            names = _flatten_tree(ctx, inf, target, key, depth_name)
            done = _tn("cli.flatten.done_full" if inf.full else "cli.flatten.done_sample", inf.total,
                       key=_display_text(key), field=", ".join(_display_text(n) for n in dict.fromkeys(names)),
                       count=_num(inf.values), total=_num(inf.total))
        tree = inf.designer.tree
        engine.check_kind(doc, tree)
        report = _run_apply(ctx, doc, tree, opts)
        if done is not None:     # said once the result is written
            ctx.info(done)
        _report_apply(ctx, report, unmapped_hint=None if inf.full else t("cli.hint.full_scan"))
    return EXIT_OK


# ==================================================================================
# stage 5: shared helpers of the tool commands
# ==================================================================================
def _json_text(data: Any) -> str:
    """``--json`` output of the stage-5 commands: exact numbers (:func:`jsonreshape.codec.dumps`),
    non-ASCII unescaped, indented."""
    from . import codec

    return codec.dumps(data, indent=2).decode("utf-8", "replace")


def _json_line(data: Any) -> str:
    from . import codec

    return codec.dumps(data).decode("utf-8", "replace")


def _filter_warnings(ctx: _Ctx, flt: Any) -> None:
    if flt is not None:
        for text in flt.warnings:
            ctx.warn(text)


def _report_warnings(ctx: _Ctx, keys: Sequence[str], texts: Sequence[str], shown: Sequence[str]) -> None:
    """A report's warnings (``texts`` parallel to ``keys``) except those of ``shown`` (the CLI gave them
    already in its own words)."""
    for key, text in zip(keys, texts):
        if key not in shown:
            ctx.warn(text)


def _scope_data(ctx: _Ctx, flt: Any, size: int) -> dict[str, Any]:
    """The record scope of a ``--json`` report: records in scope, ``--select``, the filter."""
    return {"records": int(size), "select": getattr(ctx.args, "select", None),
            "filter": None if flt is None else {"text": flt.text, "lang": flt.lang}}


def _memory_option(ctx: _Ctx) -> int | None:
    """``--memory-limit`` of stats / dupes / merge / diff: ``None`` (the tool's own default) when not
    given; else at least :data:`MIN_MEMORY_LIMIT` and at most half of ``Settings.memory_limit_bytes``
    (each with a warning, like the sort's)."""
    given = getattr(ctx.args, "memory_limit", None)
    if given is None:
        return None
    limit = int(ctx.settings.memory_limit_bytes)
    budget = max(MIN_MEMORY_LIMIT, limit // 2)
    if given < MIN_MEMORY_LIMIT:
        ctx.warn(t("cli.warn.memory_limit_min", value=_mem_text(given), minimum=_mem_text(MIN_MEMORY_LIMIT)))
        given = MIN_MEMORY_LIMIT
    if given > budget:
        ctx.warn(t("cli.warn.memory_limit_cap_tool", value=_mem_text(given), cap=_mem_text(budget),
                   limit=_mem_text(limit), setting=t("settings.field.memory_limit_bytes")))
        given = budget
    return given


def _one_output(args: argparse.Namespace, *, required: bool = False) -> str | None:
    """The output file given as the positional OUTPUT or as ``-o FILE`` (not both)."""
    positional = getattr(args, "output", None)
    option = getattr(args, "output_opt", None)
    if positional is not None and option is not None:
        raise UserInputError("cli.error.output_twice", first=positional, second=option)
    output = positional if positional is not None else option
    if output is None and required:
        raise UserInputError("cli.error.output_missing")
    return output


def _ext_format(path: str, formats: Sequence[str], key: str) -> str:
    """The format named by the extension of ``path`` (``.gz`` not allowed): one of ``formats``,
    else ``UserInputError(key)``."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "ndjson" and "jsonl" in formats:
        ext = "jsonl"
    if ext not in formats:
        raise UserInputError(key, path=path, formats=", ".join("." + f for f in formats))
    return ext


def _write_bytes(path: str, data: bytes) -> None:
    """``data`` written atomically to ``path`` (gzip-compressed when the name ends with ``.gz``)."""
    from .fileio import atomic_write
    from .writer import gzip_path

    if gzip_path(path):
        import gzip as _gzip

        data = _gzip.compress(data, compresslevel=6, mtime=0)
    with atomic_write(path) as f:
        f.write(data)


def _names_text(paths: Sequence[Any], shown: int = NAMES_SHOWN) -> str:
    names = [_display_text(os.path.basename(str(p))) for p in paths]
    text = ", ".join(names[:shown])
    if len(names) > shown:
        text += ", " + ELLIPSIS
    return text


def _path_text(path: tuple | None) -> str:
    """A path inside a value for one-line output (``$`` for the value itself)."""
    from .paths import format_path

    if not path:
        return "$"
    return _display_text(format_path(path))


def _value_text(value: Any, limit: int = DIFF_VALUE_CHARS) -> str:
    """A value as compact JSON on one line, cut to ``limit`` characters."""
    from . import codec
    from .diff import Omitted
    from .paths import MISSING

    if value is MISSING:
        return ""
    if isinstance(value, Omitted):    # a value too large to hold: its kind and size
        return _display_text(value.text())
    try:
        text = codec.dumps(value).decode("utf-8", "replace")
    except (TypeError, ValueError):  # pragma: no cover - codec values
        text = repr(value)
    return _shorten(_display_text(text), limit)


# ==================================================================================
# stats (docs/ARCHITECTURE.md §14.1)
# ==================================================================================
def _cmd_stats(ctx: _Ctx) -> int:
    """stats: per-field statistics of the records in scope (:func:`jsonreshape.stats.field_stats`);
    without ``--field`` the default columns of a sample schema of the scope."""
    from .schema import default_columns, infer_schema
    from .stats import field_stats, format_stats

    args = ctx.args
    flt = _compile_scope(ctx)
    memory = _memory_option(ctx)
    _filter_warnings(ctx, flt)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        sets: list[Any] = []
        try:
            source = _scope_source(ctx, doc, flt, sets)
            size = _scope_size(doc, source)
            fields = list(args.fields or [])
            if not fields:
                schema = infer_schema(doc, source, sample=int(ctx.settings.sample_size),
                                      progress=ctx.stage(t("cli.stage.schema")), cancel=ctx.cancel)
                ctx.end_stage()
                fields = default_columns(schema)
                ctx.info(_tn("cli.stats.default_fields", len(fields), count=_num(len(fields))))
            results = field_stats(doc, fields, source, top=args.top, exact=bool(args.exact), sample=args.sample,
                                  memory_limit=memory, progress=ctx.stage(t("cli.stage.stats")), cancel=ctx.cancel)
            ctx.end_stage()
        finally:
            _close_all(sets)
        if args.json:
            data = {"file": str(doc.source_path), "records": int(doc.record_count),
                    "scope": dict(_scope_data(ctx, flt, size), sample=args.sample),
                    "elapsed": round(ctx.elapsed(), 6), "fields": [fs.to_dict() for fs in results]}
            ctx.console.out(_json_text(data))
        else:
            ctx.console.out(format_stats(results))
        examined = max((int(fs.scanned) for fs in results), default=0)
        ctx.info(t("cli.summary.stats", fields=_tn("cli.count.fields", len(results), count=_num(len(results))),
                   records=_records_text(examined, _num(examined)), seconds=format_duration(ctx.elapsed())))
        log.info("stats of %s: %d field(s) over %d records in %.2f s", doc.source_path.name, len(results), examined,
                 ctx.elapsed())
    return EXIT_OK


# ==================================================================================
# dupes (docs/ARCHITECTURE.md §14.2)
# ==================================================================================
def _distinct_values(rep: Any) -> int:
    """Distinct key values among the records with the key: every record not in a group counts once,
    every group once (``keep="none"`` marks every member of a group, the others all but one)."""
    if rep.keep == "none":
        return int(rep.records_with_key) - int(rep.duplicates) + int(rep.groups)
    return int(rep.records_with_key) - int(rep.duplicates)


def _group_lines(groups: Sequence[tuple[str, list[int]]], sizes: Sequence[int], *, numbered: bool) -> list[str]:
    out = []
    for i, (text, positions) in enumerate(groups):
        size = int(sizes[i]) if i < len(sizes) else len(positions)
        listed = list(positions)[:POSITIONS_SHOWN]
        shown = ", ".join(f"#{p}" for p in listed)
        if size > len(listed):
            shown += ", " + ELLIPSIS
        head = f"  {i + 1:>3}. " if numbered else "  "
        out.append(head + t("cli.dupes.group", key=_display_text(text), records=_records_text(size, _num(size)),
                            positions=shown))
    return out


def _dupes_lines(rep: Any) -> list[str]:
    lines = [t("cli.dupes.key", key=_display_text(rep.key)),
             t("cli.dupes.counts", groups=_num(rep.groups), duplicates=_num(rep.duplicates),
               keep=t(f"cli.dupes.keep.{rep.keep}")),
             t("cli.dupes.records", with_key=_num(rep.records_with_key), missing=_num(rep.missing),
               skipped=_num(rep.skipped), distinct=_num(_distinct_values(rep)))]
    if not rep.groups:
        lines.append(t("cli.dupes.none"))
        return lines
    if rep.examples:
        lines.append(_tn("cli.dupes.examples", len(rep.examples), count=_num(len(rep.examples))))
        lines.extend(_group_lines(rep.examples, rep.example_sizes, numbered=False))
    if rep.top_groups:
        lines.append(_tn("cli.dupes.top", len(rep.top_groups), count=_num(len(rep.top_groups))))
        lines.extend(_group_lines(rep.top_groups, rep.top_sizes, numbered=True))
    return lines


def _groups_data(groups: Sequence[tuple[str, list[int]]], sizes: Sequence[int]) -> list[dict[str, Any]]:
    return [{"key": text, "size": int(sizes[i]) if i < len(sizes) else len(positions), "positions": list(positions)}
            for i, (text, positions) in enumerate(groups)]


def _cmd_dupes(ctx: _Ctx) -> int:
    """dupes: the records whose KEY value occurs more than once (:func:`jsonreshape.dupes.find_duplicates`):
    counts, example groups, the largest groups; ``-o FILE`` exports the records marked as duplicates."""
    from .dupes import find_duplicates, parse_key
    from .writer import export

    args = ctx.args
    out = args.out
    fmt = opts = None
    if out is not None:
        _check_output(args.input, out)
        fmt = _output_format(args, out)
        opts = _export_options(ctx, fmt)
        ignored = _ignored_for_format(args, fmt)
        if ignored:
            ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format=fmt))
    else:
        unused = _given_output_options(args) + [name for name, on in (("--to", args.to), ("--jsonl", args.jsonl)) if on]
        if unused:
            ctx.warn(t("cli.warn.output_options_unused", options=", ".join(unused)))
    parse_key(args.key)              # a bad key is refused before INPUT is read
    flt = _compile_scope(ctx)
    memory = _memory_option(ctx)
    _filter_warnings(ctx, flt)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        sets: list[Any] = []
        try:
            source = _scope_source(ctx, doc, flt, sets)
            size = _scope_size(doc, source)
            rep = find_duplicates(doc, args.key, source, keep=args.keep, memory_limit=memory, top=args.top,
                                  examples=args.examples, progress=ctx.stage(t("cli.stage.dupes")), cancel=ctx.cancel)
            ctx.end_stage()
            try:
                written = None
                if out is not None:
                    doc.ensure_unchanged()   # nothing is written from a file that changed meanwhile
                    written = export(doc, out, opts, source=rep.records, progress=ctx.stage(t("cli.stage.writing")),
                                     cancel=ctx.cancel)
                    ctx.end_stage()
                for text in rep.warning_texts():
                    ctx.warn(text)
                if args.json:
                    data = {"file": str(doc.source_path), "key": rep.key, "keep": rep.keep,
                            "records": int(doc.record_count), "scope": _scope_data(ctx, flt, size),
                            "scanned": int(rep.scanned), "groups": int(rep.groups), "duplicates": int(rep.duplicates),
                            "records_with_key": int(rep.records_with_key), "missing": int(rep.missing),
                            "skipped": int(rep.skipped), "distinct": _distinct_values(rep),
                            "examples": _groups_data(rep.examples, rep.example_sizes),
                            "top": _groups_data(rep.top_groups, rep.top_sizes),
                            "warnings": rep.warning_texts(), "elapsed": round(float(rep.elapsed), 6),
                            "output": None if written is None else {
                                "file": str(out), "records": int(written.records),
                                "bytes": int(written.bytes_written)}}
                    ctx.console.out(_json_text(data))
                else:
                    ctx.console.out_lines(_dupes_lines(rep))
                if written is not None:
                    _export_warnings(ctx, written)
                    ctx.info(_tn("cli.summary.dupes_out", written.records, count=_num(written.records), output=out,
                                 size=format_bytes(written.bytes_written)))
                scanned = int(rep.scanned)
                log.info("dupes of %s by %s: %d groups, %d duplicates of %d records in %.2f s", doc.source_path.name,
                         rep.key, rep.groups, rep.duplicates, scanned, ctx.elapsed())
            finally:
                rep.close()
        finally:
            _close_all(sets)
        ctx.info(t("cli.summary.dupes", records=_records_text(scanned, _num(scanned)),
                   seconds=format_duration(ctx.elapsed())))
    return EXIT_OK


# ==================================================================================
# schema / validate (docs/ARCHITECTURE.md §14.3)
# ==================================================================================
def _cmd_schema(ctx: _Ctx) -> int:
    """schema: a JSON Schema (draft 2020-12) of INPUT (:func:`jsonreshape.jschema.generate_schema` over
    :func:`jsonreshape.schema.infer_schema`; every record by default) -> OUTPUT or stdout."""
    from .jschema import dump_schema, generate_schema
    from .schema import infer_schema

    args = ctx.args
    output = _one_output(args)
    if output is not None:
        _check_output(args.input, output)
    indent: int | str | None = None if args.compact else (args.indent if args.indent is not None else 2)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        schema = infer_schema(doc, sample=args.sample, progress=ctx.stage(t("cli.stage.schema")), cancel=ctx.cancel)
        ctx.end_stage()
        progress = ctx.stage(t("cli.stage.schema_strict")) if args.strict else None
        value = generate_schema(schema, doc=doc, document=not args.record, strict=bool(args.strict), title=args.title,
                                progress=progress, cancel=ctx.cancel)
        ctx.end_stage()
        data = dump_schema(value, indent=indent)
        doc.ensure_unchanged()   # the schema describes the file as it still is
        if output is None:
            ctx.console.out(data.decode("utf-8", "replace").rstrip("\n"))
        else:
            _write_bytes(output, data)
        if not schema.complete:
            ctx.warn(_tn("cli.warn.schema_sample", schema.total_records, count=_num(schema.scanned),
                         total=_num(schema.total_records)))
        n_fields = len(schema.fields())
        fields = _tn("cli.count.fields", n_fields, count=_num(n_fields))
        records = _records_text(schema.scanned, _num(schema.scanned))
        if output is None:
            ctx.info(t("cli.summary.schema_stdout", fields=fields, records=records,
                       seconds=format_duration(ctx.elapsed())))
        else:
            ctx.info(t("cli.summary.schema", output=output, fields=fields, records=records,
                       seconds=format_duration(ctx.elapsed())))
        log.info("schema of %s: %d fields from %d records -> %s in %.2f s", doc.source_path.name, n_fields,
                 schema.scanned, output or "stdout", ctx.elapsed())
    return EXIT_OK


class _IssueSink:
    """Writes validation issues (``validate(on_issues=)``) to an open binary file: JSON Lines (one
    ``Issue.to_dict()`` per line), a JSON array, or CSV (``record, key, path, keyword, schema_path,
    message, params``; BOM and delimiter of the CSV settings)."""

    def __init__(self, f: Any, fmt: str, settings: Any) -> None:
        self.f = f
        self.fmt = fmt
        self.count = 0
        self.text: Any = None
        self.rows: Any = None
        if fmt == "csv":
            import csv
            import io

            self.text = io.TextIOWrapper(f, encoding="utf-8-sig" if settings.csv_bom else "utf-8", newline="")
            self.rows = csv.writer(self.text, delimiter=settings.csv_delimiter, lineterminator="\r\n")
            self.rows.writerow(["record", "key", "path", "keyword", "schema_path", "message", "params"])
        elif fmt == "json":
            f.write(b"[")

    def add(self, issues: list[Any]) -> None:
        from . import codec

        for issue in issues:
            d = issue.to_dict()
            if self.fmt == "jsonl":
                self.f.write(codec.dumps(d) + b"\n")
            elif self.fmt == "json":
                self.f.write((b"\n" if self.count == 0 else b",\n") + codec.dumps(d))
            else:
                self.rows.writerow(["" if d["record"] is None else d["record"], "" if d["key"] is None else d["key"],
                                    d["path"], d["keyword"], d["schema_path"], d["message"],
                                    codec.dumps(d["params"]).decode("utf-8", "replace")])
            self.count += 1

    def finish(self) -> None:
        if self.fmt == "json":
            self.f.write(b"\n]\n" if self.count else b"]\n")
        elif self.text is not None:
            self.text.flush()
            self.text.detach()


def _issue_line(issue: Any) -> str:
    """``#record [key] path: message (keyword)``; ``(hujjat)`` for an issue of the document itself."""
    where = f"#{issue.record}" if issue.record is not None else t("cli.validate.document")
    key = f" [{_display_text(str(issue.key))}]" if issue.key is not None else ""
    return f"{where}{key} {_display_text(issue.path_text)}: {_display_text(issue.message)} ({issue.keyword})"


def _validate_data(ctx: _Ctx, doc: Any, report: Any, flt: Any, size: int, exported: int | None,
                   ok: bool) -> dict[str, Any]:
    """``validate --json``; ``ok``: the verdict of the exit code -- no issue found AND every record in scope
    validated (``skipped`` = 0)."""
    args = ctx.args
    return {
        "file": str(doc.source_path), "schema": str(args.schema), "scope": report.scope,
        "records": int(doc.record_count), "selection": _scope_data(ctx, flt, size), "ok": bool(ok),
        "scanned": int(report.scanned), "invalid": int(report.invalid), "issue_count": int(report.issue_count),
        "document_issues": int(report.document_issues), "counts": dict(report.counts),
        "top_paths": [{"path": p, "count": int(n)} for p, n in report.top_paths],
        "skipped": int(report.skipped), "too_large": int(report.too_large), "unreadable": int(report.unreadable),
        "too_deep": int(report.too_deep), "limited_records": int(report.limited_records),
        "truncated": bool(report.truncated), "formats": bool(report.formats),
        "elapsed": round(float(report.elapsed), 6),
        "warnings": report.warning_texts(), "issues": [issue.to_dict() for issue in report.issues],
        "exported": None if exported is None else {"file": str(args.export), "issues": int(exported)},
    }


def _cmd_validate(ctx: _Ctx) -> int:
    """validate: INPUT against a JSON Schema (:func:`jsonreshape.jschema.validate`); exit 5 when an issue
    was found."""
    from .fileio import atomic_write
    from .jschema import compile_schema, load_schema, validate

    args = ctx.args
    _check_input(args.schema)
    export_fmt = None
    if args.export is not None:
        _check_output(args.input, args.export)
        if _same_file(args.schema, args.export):
            raise UserInputError("cli.error.output_is_schema", path=args.export)
        export_fmt = _ext_format(args.export, ISSUE_EXPORT_FORMATS, "cli.error.issues_format")
    flt = _compile_scope(ctx)
    compiled = compile_schema(load_schema(args.schema), formats=bool(args.formats))   # schema errors first
    max_per_record = args.max_per_record or None
    _filter_warnings(ctx, flt)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        sets: list[Any] = []
        exported: int | None = None
        try:
            source = _scope_source(ctx, doc, flt, sets)
            size = _scope_size(doc, source)
            kwargs: dict[str, Any] = {"scope": args.scope, "max_per_record": max_per_record,
                                      "max_issues": args.max_issues, "formats": bool(args.formats),
                                      "progress": ctx.stage(t("cli.stage.validating")), "cancel": ctx.cancel}
            if args.export is None:
                report = validate(doc, compiled, source, **kwargs)
            else:
                report = None
                try:
                    with atomic_write(args.export) as f:
                        sink = _IssueSink(f, str(export_fmt), ctx.settings)
                        report = validate(doc, compiled, source, on_issues=sink.add, **kwargs)
                        sink.finish()
                except BaseException:
                    if report is not None:
                        report.close()
                    raise
                exported = sink.count
            ctx.end_stage()
        finally:
            _close_all(sets)
        try:
            issues = int(report.issue_count)
            skipped = int(report.skipped)      # records not validated: too large, unreadable, too deep
            ok = issues == 0 and skipped == 0
            _report_warnings(ctx, report.warnings, report.warning_texts(), VALIDATE_SHOWN_WARNINGS)
            if args.json:
                ctx.console.out(_json_text(_validate_data(ctx, doc, report, flt, size, exported, ok)))
            elif report.issues:
                ctx.console.out_lines(_issue_line(issue) for issue in report.issues)
            seconds = format_duration(ctx.elapsed())
            checked = _records_text(report.scanned, _num(report.scanned))
            unchecked = _records_text(skipped, _num(skipped))
            if ok:
                ctx.info(_tn("cli.validate.ok", report.scanned, records=checked, seconds=seconds))
            elif issues == 0:          # nothing found, but not every record was checked: not shown to match
                ctx.info(t("cli.validate.unchecked", records=checked, skipped=unchecked, seconds=seconds))
            else:
                ctx.info(t("cli.validate.failed", invalid=_num(report.invalid), records=checked,
                           issues=_tn("cli.count.issues", issues, count=_num(issues)), seconds=seconds))
                ctx.info(t("cli.validate.keywords",
                           items=", ".join(f"{kw} {_num(n)}" for kw, n in report.counts.items())))
                if skipped:
                    ctx.info(t("cli.validate.skipped", skipped=unchecked))
                if not args.json and args.max_issues and issues > len(report.issues):
                    ctx.info(_tn("cli.validate.more", len(report.issues), shown=_num(len(report.issues)),
                                 total=_num(issues)))
            if exported is not None:
                ctx.info(_tn("cli.validate.exported", exported, count=_num(exported), path=args.export))
            log.info("validate %s against %s: %d records, %d invalid, %d issues, %d not validated (scope %s) "
                     "in %.2f s", doc.source_path.name, args.schema, report.scanned, report.invalid, issues, skipped,
                     report.scope, ctx.elapsed())
        finally:
            report.close()
    return EXIT_OK if ok else EXIT_DIFFERENT


# ==================================================================================
# split / merge / extract (docs/ARCHITECTURE.md §14.4)
# ==================================================================================
def _split_export(ctx: _Ctx, doc: Any) -> Any:
    """The writer options of a split: ``None`` (the parts keep the document's own kind, byte for byte)
    unless ``--to`` / ``--jsonl`` / ``--compact`` / ``--indent`` / ``--sort-keys`` is given (other output
    options without ``--to`` are named in a warning)."""
    args = ctx.args
    given = _given_output_options(args)
    own = "jsonl" if doc.kind in LINE_KINDS else "json"
    fmt: str | None = None
    if args.jsonl:
        if args.to not in (None, "jsonl"):
            raise UserInputError("cli.error.jsonl_conflict", fmt=args.to)
        fmt = "jsonl"
    elif args.to is not None:
        fmt = args.to
    if fmt is None and not ({"--compact", "--indent", "--sort-keys"} & set(given)):
        ignored = [name for name in given if name != "--no-wrapper"]
        if ignored:
            ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format=own))
        return None
    fmt = fmt or own
    ignored = _ignored_for_format(args, fmt)
    if ignored:
        ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format=fmt))
    return _export_options(ctx, fmt)


def _cmd_split(ctx: _Ctx) -> int:
    """split: INPUT (or the records in scope) into parts (:func:`jsonreshape.filetools.split`); existing
    part files only with ``--force``, never one with unsaved edits; ``--remove-stale`` deletes the rest of
    an earlier split in the same commit; the parts written are listed on stdout, the files removed on
    stderr."""
    import dataclasses

    from .fileio import long_path
    from .filetools import SplitOptions, part_names, plan_split, split
    from .writer import refuse_edited_targets

    args = ctx.args
    out_dir = args.out_dir
    if out_dir is not None and not os.path.isdir(long_path(out_dir)):
        if os.path.exists(long_path(out_dir)):
            raise UserInputError("cli.error.out_dir_file", path=out_dir)
        raise UserInputError("filetools.error.no_dir", path=out_dir)
    base = SplitOptions(parts=args.parts, size=args.size, records=args.records, balance=args.balance or "bytes",
                        out_dir=out_dir, name=args.name, keep_wrapper=not args.no_wrapper,
                        drop_broken=bool(args.drop_broken),
                        remove_stale=bool(args.remove_stale))         # bad values refused before INPUT is read
    if args.balance is not None and args.parts is None:
        ctx.warn(t("cli.warn.balance_parts"))
    flt = _compile_scope(ctx)
    _filter_warnings(ctx, flt)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc, broken=False, json_hint=t("cli.hint.repair_first"))
        if rc is not None:
            return rc
        opts = dataclasses.replace(base, export=_split_export(ctx, doc))
        sets: list[Any] = []
        try:
            source = _scope_source(ctx, doc, flt, sets)
            plan = plan_split(doc, opts, source, cancel=ctx.cancel)
            names = part_names(doc, opts, source, count=len(plan))     # exactly the files split writes
            refuse_edited_targets(names)      # unsaved edits: not even --force replaces such a part
            existing = [p for p in names if os.path.lexists(long_path(p))]
            if existing and not args.force:
                raise _HintedError("cli.error.parts_exist", hint=t("cli.hint.force"), count=_num(len(existing)),
                                   names=_names_text(existing))
            doc.ensure_unchanged()   # nothing is written from a file that changed meanwhile
            report = split(doc, opts, source, progress=ctx.stage(t("cli.stage.splitting")), cancel=ctx.cancel)
            ctx.end_stage()
        finally:
            _close_all(sets)
        for key, text in zip(report.warning_keys, report.warnings):
            if key != "filetools.warn.stale_removed":       # (every removed file is listed below)
                ctx.warn(text)
        if report.stale and not args.remove_stale:
            ctx.console.hint(t("cli.hint.remove_stale"))
        # the parts in the folder as the user named it (-o DIR, else the input's folder)
        folder = out_dir if out_dir is not None else os.path.dirname(args.input)
        for p in report.removed:      # always shown (-q too): files were deleted
            ctx.console.err(t("cli.split.removed", path=_display_path(os.path.join(folder, p.name))))
        ctx.console.out_lines(_display_path(os.path.join(folder, p.path.name)) for p in report.parts)
        ctx.info(t("cli.summary.split", records=_records_text(report.records, _num(report.records)),
                   parts=_tn("cli.count.parts", len(report.parts), count=_num(len(report.parts))),
                   folder=_display_path(folder or os.curdir), size=format_bytes(report.bytes_written),
                   seconds=format_duration(ctx.elapsed())))
        log.info("split %s: %d records into %d parts (%s) in %.2f s", doc.source_path.name, report.records,
                 len(report.parts), report.engine, ctx.elapsed())
    return EXIT_OK


def _natural_key(path: str) -> tuple[bytes, str]:
    """Natural order of file names (``qism_2`` before ``qism_10``), Uzbek collation, case-insensitive."""
    from .sortkeys import collation_key

    return collation_key(path, "uz", natural=True), path


def _expand_inputs(ctx: _Ctx, items: Sequence[str], output: str) -> list[str]:
    """``merge`` INPUTs with ``*`` / ``?`` / ``[`` that are not existing files are patterns expanded
    here (cmd.exe and PowerShell pass them unexpanded), files only, sorted naturally; a pattern
    without a match is exit 1; OUTPUT itself is skipped (a warning)."""
    from .fileio import long_path

    out: list[str] = []
    for item in items:
        if not any(ch in item for ch in GLOB_CHARS) or os.path.lexists(long_path(item)):
            out.append(item)
            continue
        matches = [m for m in glob.glob(item, recursive=True) if os.path.isfile(long_path(m))]
        skipped = [m for m in matches if _same_file(m, output)]
        if skipped:
            ctx.warn(t("cli.warn.output_matched", path=output, pattern=item))
        matches = sorted((m for m in matches if not _same_file(m, output)), key=_natural_key)
        if not matches:
            raise UserInputError("cli.error.no_glob_match", pattern=item)
        out.extend(matches)
    return out


def _cmd_merge(ctx: _Ctx) -> int:
    """merge: several inputs into ONE JSON / JSON Lines file (:func:`jsonreshape.filetools.merge`),
    optionally without duplicates (``--dedupe-by KEY``)."""
    from .dupes import parse_key
    from .filetools import MergeOptions, merge

    args = ctx.args
    out = args.output
    inputs = _expand_inputs(ctx, args.inputs, out)
    for path in inputs:
        _check_input(path)
        _check_output(path, out)
    fmt = _output_format(args, out)
    opts = MergeOptions(export=_export_options(ctx, fmt), dedupe_by=args.dedupe_by, keep=args.keep or "first",
                        allow_partial=bool(args.allow_partial), memory_limit=_memory_option(ctx))
    ignored = _ignored_for_format(args, fmt)
    if ignored:
        ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format=fmt))
    if args.dedupe_by is not None:
        parse_key(args.dedupe_by)    # a bad key is refused before the inputs are read
    elif args.keep is not None:
        ctx.warn(t("cli.warn.keep_without_dedupe"))
    with contextlib.ExitStack() as stack:
        docs = []
        for path in inputs:
            doc = stack.enter_context(_document(ctx, path=path))
            rc = _check_document(ctx, doc, label=path)
            if rc is not None:
                return rc
            docs.append(doc)
        for doc in docs:
            doc.ensure_unchanged()   # nothing is written from a file that changed meanwhile
        report = merge(docs, out, opts, progress=ctx.stage(t("cli.stage.merging")), cancel=ctx.cancel)
        ctx.end_stage()
        _export_warnings(ctx, report)
        for given, (_path, _kind, records, written) in zip(inputs, report.inputs):   # as the user named them
            ctx.info(_tn("cli.merge.input", records, path=_display_path(given), written=_num(written),
                         records=_num(records)))
        summary = t("cli.summary.merge", inputs=_tn("cli.count.files", len(docs), count=_num(len(docs))), output=out,
                    records=_records_text(report.records, _num(report.records)),
                    size=format_bytes(report.report.bytes_written), seconds=format_duration(ctx.elapsed()))
        if args.dedupe_by is not None:
            summary += "; " + _tn("cli.summary.merge_dupes", report.duplicates, count=_num(report.duplicates),
                                  groups=_num(report.groups))
        ctx.info(summary)
        log.info("merge of %d inputs -> %s: %d records, %d duplicates removed in %.2f s", len(docs), out,
                 report.records, report.duplicates, ctx.elapsed())
    return EXIT_OK


def _cmd_extract(ctx: _Ctx) -> int:
    """extract: the records of the container at ``--path`` (or the detected one) without the wrapper,
    one node (``--node``) or the values of a pattern gathered from every record (``--gather``)."""
    from .filetools import extract

    args = ctx.args
    output = _one_output(args, required=True)
    assert output is not None
    _check_output(args.input, output)
    if args.path is not None:
        if args.record_path is not None and args.record_path != args.path:
            raise UserInputError("cli.error.extract_path_twice", path=args.path, record_path=args.record_path)
        args.record_path = args.path
    fmt = _output_format(args, output)
    if args.node is not None:
        combined = [name for name, given in (("--where", args.where), ("--jmespath", args.jmespath),
                                             ("--preset", args.preset), ("--save-preset", args.save_preset),
                                             ("--select", args.select)) if given is not None]
        if combined:
            raise UserInputError("cli.error.node_combined", options=", ".join(combined))
        _check_values_format(fmt)
        opts = _values_export_options(ctx, fmt, "--node")
        with _document(ctx) as doc:
            rc = _check_document(ctx, doc)
            if rc is not None:
                return rc
            _write_node(ctx, doc, output, opts, args.node)
        return EXIT_OK
    if args.gather is not None:
        _check_values_format(fmt)
        opts = _values_export_options(ctx, fmt, "--gather")
    else:
        opts = _export_options(ctx, fmt)
        ignored = _ignored_for_format(args, fmt)
        if ignored:
            ctx.warn(t("cli.warn.ignored_options", options=", ".join(ignored), format=fmt))
    flt = _compile_scope(ctx)
    _filter_warnings(ctx, flt)
    with _document(ctx) as doc:
        rc = _check_document(ctx, doc)
        if rc is not None:
            return rc
        sets: list[Any] = []
        try:
            source = _scope_source(ctx, doc, flt, sets)
            doc.ensure_unchanged()   # nothing is written from a file that changed meanwhile
            report = extract(doc, output, opts, path=args.gather, source=source,
                             progress=ctx.stage(t("cli.stage.extracting")), cancel=ctx.cancel)
            ctx.end_stage()
        finally:
            _close_all(sets)
        _export_warnings(ctx, report)
        if args.gather is None:
            _export_summary(ctx, report, int(doc.record_count), fmt, output)
        else:
            ctx.info(_tn("cli.summary.extract", report.records, values=_num(report.records), output=output,
                         size=format_bytes(report.bytes_written), seconds=format_duration(ctx.elapsed())))
            log.info("extract %s of %s: %d values -> %s in %.2f s", args.gather, doc.source_path.name,
                     report.records, output, ctx.elapsed())
    return EXIT_OK


# ==================================================================================
# repair (docs/ARCHITECTURE.md §14.5)
# ==================================================================================
def _show_repair(ctx: _Ctx, rep: Any, *, skip: str | None = None) -> None:
    """The repair report: ``--json`` (stdout), the headline and the summary lines (stderr; ``skip``: a
    line the error message repeats), "no problems found" only when nothing was fixed, cut or stopped,
    the data-changing fixes and the warnings (always shown), ``--report``."""
    args = ctx.args
    if args.report is not None:
        rep.write(args.report, _ext_format(args.report, REPAIR_REPORT_FORMATS, "cli.error.report_format"))
    if args.json:
        ctx.console.out(_json_text(rep.to_dict()))
    ctx.info(rep.headline())
    for line in rep.summary_lines():
        if line != skip:
            ctx.info(line)
    if rep.total == 0 and rep.problem is None and rep.truncated is None:
        ctx.info(t("repair.summary.none_check" if rep.check_only else "repair.summary.none"))
    data = rep.data_changes_line()
    if data:
        ctx.warn(data)
    if rep.problem is not None and rep.written:
        ctx.warn(t("cli.warn.repair_partial", line=rep.problem.line, column=rep.problem.column))
    for text in rep.warning_texts():
        ctx.warn(text)
    if args.report is not None:
        ctx.info(_tn("cli.summary.repair_report", rep.total, count=_num(rep.total), path=args.report))
    elif rep.total:
        ctx.info(t("cli.hint.repair_report"))


def _cmd_repair(ctx: _Ctx) -> int:
    """repair: a broken JSON / JSON Lines file -> a valid copy with a report of every fix
    (:func:`jsonreshape.repair.repair_file`); never in place; ``--check`` writes nothing."""
    from .repair import RepairOptions, UnrepairableError, repair_file

    args = ctx.args
    _check_input(args.input)
    output = _one_output(args)
    if args.check:
        if output is not None:
            ctx.warn(t("cli.warn.check_output", path=output))
            output = None
    else:
        if output is None:
            raise UserInputError("cli.error.repair_output")
        _check_output(args.input, output)
    if args.report is not None:
        _check_output(args.input, args.report)
        if output is not None and _same_file(output, args.report):
            raise UserInputError("cli.error.report_is_output", path=args.report)
        _ext_format(args.report, REPAIR_REPORT_FORMATS, "cli.error.report_format")
    fmt = None if args.format in (None, "auto") else args.format
    opts = RepairOptions(nan=args.nan, format=fmt, record_path=args.record_path,
                         output_kind="jsonl" if args.jsonl else None, allow_partial=bool(args.allow_partial))
    label = t("cli.stage.checking" if args.check else "cli.stage.repairing")
    try:
        rep = repair_file(args.input, output, opts, check_only=bool(args.check), settings=ctx.settings,
                          progress=ctx.stage(label), cancel=ctx.cancel)
    except UnrepairableError as exc:
        ctx.end_stage()
        rep = exc.report
        try:
            p = rep.problem
            skip = None if p is None else t("repair.summary.problem", reason=p.reason, line=p.line, column=p.column)
            _show_repair(ctx, rep, skip=skip)
            lines = [t("cli.error.prefix", message=exc.message)]
            if p is not None:
                lines.extend("  " + _same_width_text(line) for line in p.context().split("\n"))
            kept = rep.truncated.kept_records if rep.truncated is not None else 0
            lines.append(t("cli.error.hint", hint=_tn("cli.hint.repair_partial", kept, count=_num(kept))))
            ctx.console.err("\n".join(lines))
            log.info("repair of %s: unrepairable (%s)", args.input, p.code if p is not None else "?")
        finally:
            rep.close()
        return EXIT_JSON
    except JsonReshapeError as exc:
        ctx.end_stage()
        if exc.key == "repair.error.invalid_output":   # a bug guard: nothing was written
            log.error("repair: the repaired output failed its check: %s", exc.message)
            ctx.console.error(exc)
            return EXIT_INTERNAL
        raise
    ctx.end_stage()
    try:
        _show_repair(ctx, rep)
        problems = bool(rep.total) or rep.problem is not None or rep.truncated is not None
    finally:
        rep.close()
    if args.check and problems:
        return EXIT_JSON
    return EXIT_OK


# ==================================================================================
# diff (docs/ARCHITECTURE.md §14.6)
# ==================================================================================
class _DiffPrinter:
    """Shows the record diffs while :func:`jsonreshape.diff.iter_record_diffs` finds them (text lines
    or JSON Lines on stdout) and keeps the generator's report."""

    def __init__(self, ctx: _Ctx, key: str | None, *, limit: int, json_lines: bool, quiet: bool) -> None:
        self.ctx = ctx
        self.key = key
        self.limit = limit
        self.json_lines = json_lines
        self.quiet = quiet
        self.color = ctx.console.color
        self.report: Any = None
        self.total = 0
        self.printed = 0

    def tee(self, records: Any) -> Iterator[Any]:
        """``records`` passed through, each shown; returns (as a generator) the report."""
        try:
            while True:
                try:
                    rd = next(records)
                except StopIteration as stop:
                    self.report = stop.value
                    return stop.value
                self.show(rd)
                yield rd
        finally:
            records.close()

    def show(self, rd: Any) -> None:
        from .diff import record_dict

        self.total += 1
        if self.quiet:
            return
        if self.json_lines:
            self.ctx.console.out(_json_line(record_dict(rd)))
            return
        if self.limit and self.printed >= self.limit:
            return
        self.printed += 1
        self.ctx.console.out_lines(self.lines(rd))

    def paint(self, op: str, text: str) -> str:
        return DIFF_COLORS[op] + text + HIGHLIGHT_OFF if self.color else text

    def change_line(self, c: Any, indent: str) -> str:
        if c.op == "changed":
            body = f"{_path_text(c.path)}: {_value_text(c.a)} {ARROW} {_value_text(c.b)}"
        elif c.op == "added":
            body = f"{_path_text(c.path)}: {_value_text(c.b)}"
        elif c.op == "removed":
            body = f"{_path_text(c.path)}: {_value_text(c.a)}"
        else:   # moved
            body = f"{_path_text(c.a_path)} {ARROW} {_path_text(c.b_path)}"
        return indent + self.paint(c.op, f"{DIFF_MARKS.get(c.op, '?')} {body}")

    def lines(self, rd: Any) -> list[str]:
        from .paths import MISSING

        diff = rd.diff
        changes = list(diff.changes) if diff is not None else []
        if rd.scope == "document":
            out = [self.change_line(c, "") for c in changes]
            indent = ""
        else:
            if rd.scope == "wrapper":
                head = f"{DIFF_MARKS[rd.status]} {t('cli.diff.wrapper')}"
            else:
                parts = []
                if rd.key is not MISSING and self.key not in (None, "$"):
                    parts.append(f"{_display_text(self.key)}={_value_text(rd.key)}")
                where = [f"A #{rd.a_index}" if rd.a_index is not None else "",
                         f"B #{rd.b_index}" if rd.b_index is not None else ""]
                parts.append("(" + ", ".join(w for w in where if w) + ")")
                head = f"{DIFF_MARKS[rd.status]} " + "  ".join(parts)
            out = [self.paint(rd.status, head)]
            indent = "    "
            if rd.status != "changed":
                return out          # an added / removed record: its value is in --json / -o
            out.extend(self.change_line(c, indent) for c in changes)
        if diff is not None and diff.truncated:
            more = int(diff.total) - len(changes)
            out.append(indent + _tn("cli.diff.more_changes", more, count=_num(more)))
        return out


def _diff_summary(report: Any, key: str | None, seconds: str) -> str:
    if report.mode == "key" and key == "$":
        mode = t("cli.diff.mode.unordered")
    else:
        mode = t(f"cli.diff.mode.{report.mode}", key=_display_text(str(report.key or key or "")))
    a = _num(report.a_records)
    b = _num(report.b_records)
    if report.identical:
        return t("cli.diff.identical", a=a, b=b, mode=mode, seconds=seconds)
    text = t("cli.diff.summary", changed=_num(report.changed), removed=_num(report.removed), added=_num(report.added),
             equal=_num(report.equal), a=a, b=b, mode=mode, seconds=seconds)
    if report.wrapper is not None:
        text += "; " + t("cli.diff.wrapper_differs")
    if report.moved:
        text += "; " + _tn("cli.diff.moved", report.moved, count=_num(report.moved))
    return text


def _cmd_diff(ctx: _Ctx) -> int:
    """diff: the differences of two files record by record (:func:`jsonreshape.diff.iter_record_diffs`);
    ``--exit-code``: exit 5 when they differ."""
    from .diff import DiffOptions, iter_record_diffs, summary_dict, write_diff
    from .dupes import parse_key

    args = ctx.args
    _check_input(args.a)
    _check_input(args.b)
    out = args.out
    out_fmt = None
    if out is not None:
        _check_output(args.a, out)
        _check_output(args.b, out)
        out_fmt = _ext_format(out, DIFF_OUT_FORMATS, "cli.error.diff_format")
    key = args.key
    mode = "key" if key is not None else "position" if args.by_position else "align" if args.align else "auto"
    if args.ignore_order and mode == "auto":
        key, mode = "$", "key"        # records are matched by their whole value
    if key is not None:
        parse_key(key)                # a bad key is refused before the files are read
    options: dict[str, Any] = {"array_match": "unordered" if args.ignore_order else "auto",
                               "array_keys": tuple(args.array_keys or ()), "numbers": args.numbers}
    if args.max_changes is not None:
        options["max_changes"] = args.max_changes
    opts = DiffOptions(**options)
    memory = _memory_option(ctx)
    quiet = bool(args.summary)
    detailed = out is not None or (args.json and not quiet)
    max_details = None if detailed else (0 if quiet else (args.limit or None))
    with _document(ctx, path=args.a) as doc_a, _document(ctx, path=args.b) as doc_b:
        for doc, label in ((doc_a, args.a), (doc_b, args.b)):
            rc = _check_document(ctx, doc, label=label)
            if rc is not None:
                return rc
        printer = _DiffPrinter(ctx, key, limit=args.limit, json_lines=bool(args.json), quiet=quiet)
        records = iter_record_diffs(doc_a, doc_b, key=key, mode=mode, opts=opts, max_details=max_details,
                                    memory_limit=memory, progress=ctx.stage(t("cli.stage.comparing")),
                                    cancel=ctx.cancel)
        written = None
        stream = printer.tee(records)
        try:
            if out is not None:
                # the printer's generator hides the run from write_diff: name the compared files (sources
                # and data copies), so a target that is one of them is refused before the diff runs
                written = write_diff(stream, out, str(out_fmt),
                                     sources=(doc_a.source_path, doc_a.data_path, doc_b.source_path, doc_b.data_path))
            else:
                for _rd in stream:
                    pass
        finally:
            ctx.end_stage()
        report = printer.report
        try:
            if args.json:
                ctx.console.out(_json_line({"summary": summary_dict(report)}))
            _report_warnings(ctx, report.warnings, report.warning_texts(), DIFF_SHOWN_WARNINGS)
            ctx.info(_diff_summary(report, key, format_duration(ctx.elapsed())))
            if not quiet and not args.json and args.limit and printer.total > printer.printed:
                ctx.info(_tn("cli.diff.more", printer.printed, shown=_num(printer.printed), total=_num(printer.total)))
            if written is not None:
                ctx.info(_tn("cli.diff.written", written, count=_num(written), path=out))
            log.info("diff %s / %s (%s): %d changed, %d added, %d removed, %d equal in %.2f s", args.a, args.b,
                     report.mode, report.changed, report.added, report.removed, report.equal, ctx.elapsed())
            identical = bool(report.identical)
        finally:
            report.close()
    if args.exit_code and not identical:
        return EXIT_DIFFERENT
    return EXIT_OK


# ==================================================================================
# presets
# ==================================================================================
def _cmd_presets(ctx: _Ctx) -> int:
    """List the saved filters, or delete one (``--delete NAME``)."""
    args = ctx.args
    store = _presets()
    if args.delete is not None:
        if not store.delete(args.delete):
            raise UserInputError("cli.error.preset_not_found", name=args.delete)
        ctx.info(t("cli.preset.deleted", name=args.delete))
        return EXIT_OK
    items = list(store.list())
    if args.json:
        ctx.console.out(json.dumps(items, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    if not items:
        ctx.info(t("cli.preset.empty"))
        return EXIT_OK
    names = [_display_text(str(item.get("name", ""))) for item in items]
    width = max(len(name) for name in names)
    lines = []
    for name, item in zip(names, items):
        lines.append(t("cli.preset.line", name=name.ljust(width), lang=str(item.get("lang") or "expr"),
                       text=_display_text(str(item.get("text", "")))))
    ctx.console.out_lines(lines)
    return EXIT_OK


# ==================================================================================
# --version
# ==================================================================================
def _version_lines(backend: str | None, detail: str | None = None) -> list[str]:
    label = _backend_label(backend or "none")
    if detail:
        label = f"{label} ({detail})"
    return [
        t("cli.version", version=__version__),
        t("env.report.core", backend=label),
        t("env.report.python", version=platform.python_version(), implementation=platform.python_implementation(),
          bits=struct.calcsize("P") * 8),
    ]


# ==================================================================================
# main
# ==================================================================================
# ==================================================================================
# import (CSV / YAML / XML -> JSON / JSON Lines; docs/ARCHITECTURE.md §17)
# ==================================================================================
IMPORT_FORMATS = ("auto", "csv", "yaml", "xml")       # importers.FORMATS
IMPORT_OUTPUTS = ("json", "jsonl")                      # importers.OUTPUT_FORMATS
IMPORT_TYPES = ("auto", "string")                       # importers.TYPE_MODES
IMPORT_EMPTY = ("null", "string")                       # importers.EMPTY_MODES


def _add_import_command(sub: Any) -> None:
    """``import INPUT [OUTPUT | -o OUTPUT] [--from csv|yaml|xml] [--to json|jsonl | --jsonl] [--compact |
    --indent N] [--types auto|string] [--empty null|string] [--json] [CSV options] [XML options]``."""
    p = _tool_command(sub, "import")
    p.add_argument("input", metavar="INPUT", help=t("cli.help.import_input"))
    p.add_argument("output", metavar="OUTPUT", nargs="?", default=None, help=t("cli.help.import_output"))
    g = p.add_argument_group(t("cli.group.import"))
    _add_output_file_option(g, "output_opt", "cli.help.output_opt")
    g.add_argument("--from", "--format", dest="from_fmt", choices=IMPORT_FORMATS, default="auto",
                   help=t("cli.help.import_from"))
    g.add_argument("--to", dest="to_fmt", choices=IMPORT_OUTPUTS, default=None, help=t("cli.help.import_to"))
    g.add_argument("--jsonl", action="store_true", help=t("cli.help.import_jsonl"))
    _add_json_layout_options(g)
    g.add_argument("--types", choices=IMPORT_TYPES, default="auto", help=t("cli.help.import_types"))
    g.add_argument("--empty", choices=IMPORT_EMPTY, default="null", help=t("cli.help.import_empty"))
    g.add_argument("--json", action="store_true", help=t("cli.help.import_json"))
    g = p.add_argument_group(t("cli.group.import_csv"))
    g.add_argument("--delimiter", metavar="D", type=_delimiter_value, default=None, help=t("cli.help.import_delimiter"))
    g.add_argument("--no-header", action="store_true", help=t("cli.help.import_no_header"))
    g.add_argument("--no-nest", action="store_true", help=t("cli.help.import_no_nest"))
    g.add_argument("--encoding", metavar="ENC", default=None, help=t("cli.help.import_encoding"))
    g.add_argument("--skip-bad", action="store_true", help=t("cli.help.import_skip_bad"))
    g = p.add_argument_group(t("cli.group.import_xml"))
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--record-tag", metavar="TAG", default=None, help=t("cli.help.import_record_tag"))
    mx.add_argument("--record-path", metavar="P", default=None, help=t("cli.help.import_record_path"))
    _add_global_options(p, repeated=True)


def _cmd_import(ctx: _Ctx) -> int:
    """import: a CSV / TSV, YAML or XML file -> JSON / JSON Lines (:func:`jsonreshape.importers.import_file`);
    the summary line, the report's notes and warnings, ``--json``."""
    from .importers import ImportOptions, format_name, import_file

    args = ctx.args
    _check_input(args.input)
    output = _one_output(args, required=True)
    _check_output(args.input, output)
    indent: Any = None if args.compact else (args.indent if args.indent is not None else ctx.settings.export_indent)
    opts = ImportOptions(fmt=None if args.from_fmt == "auto" else args.from_fmt,
                         output="jsonl" if args.jsonl else args.to_fmt, indent=indent, types=args.types,
                         empty=args.empty, delimiter=args.delimiter, header=not args.no_header,
                         nest=not args.no_nest, encoding=args.encoding, skip_bad=bool(args.skip_bad),
                         record_tag=args.record_tag, record_path=args.record_path)
    try:
        rep = import_file(args.input, output, opts, settings=ctx.settings, progress=ctx.stage(t("cli.stage.importing")),
                          cancel=ctx.cancel)
    finally:
        ctx.end_stage()
    if args.json:
        ctx.console.out(_json_text(rep.to_dict()))
    ctx.info(_tn("cli.summary.import", rep.records, records=_num(rep.records), input=_display_path(args.input),
                 fmt=format_name(rep.fmt), output=_display_path(output), size=format_bytes(rep.bytes_written),
                 seconds=format_duration(rep.elapsed)))
    for text in rep.notes:
        ctx.info(text)
    for text in rep.warnings:
        ctx.warn(text)
    return EXIT_OK


_COMMANDS: dict[str, Callable[[_Ctx], int]] = {
    "inspect": _cmd_inspect,
    "search": _cmd_search,
    "filter": _cmd_export,
    "sort": _cmd_export,
    "convert": _cmd_export,
    "patch": _cmd_patch,
    "apply": _cmd_apply,
    "flatten": _cmd_flatten,
    "stats": _cmd_stats,
    "dupes": _cmd_dupes,
    "schema": _cmd_schema,
    "validate": _cmd_validate,
    "split": _cmd_split,
    "merge": _cmd_merge,
    "extract": _cmd_extract,
    "repair": _cmd_repair,
    "import": _cmd_import,
    "diff": _cmd_diff,
    "presets": _cmd_presets,
}


def _start_logging(verbose: int) -> str | None:
    """File logging always; ``-v``/``-vv`` add the console (INFO/DEBUG). Returns the log path."""
    from . import log as applog

    try:
        path = applog.setup_logging("DEBUG" if verbose >= 2 else "INFO", console=verbose > 0)
    except Exception:  # an unwritable data folder must not stop the command
        return None
    return str(path)


def _apply_backend(choice: str | None) -> str | None:
    """Set ``JSONRESHAPE_BACKEND`` from ``--backend``; a note when the core of this process
    is already loaded with another backend (in-process use)."""
    if choice is None:
        return None
    os.environ["JSONRESHAPE_BACKEND"] = choice
    loaded = sys.modules.get("jsonreshape.core")
    active = getattr(loaded, "BACKEND", None) if loaded is not None else None
    if active is not None and choice != "auto" and active != choice:
        log.warning("--backend %s ignored: the core of this process is already loaded with the %s backend",
                    choice, active)
        return t("cli.warn.backend_loaded", requested=choice, active=active)
    return None


def _load_core() -> Any:
    try:
        from . import core
    except ImportError as exc:
        raise UserInputError("cli.error.backend", backend=os.environ.get("JSONRESHAPE_BACKEND", "auto"),
                             detail=str(exc)) from exc
    if getattr(core, "BACKEND", "none") == "none":
        raise UserInputError("cli.error.backend", backend=os.environ.get("JSONRESHAPE_BACKEND", "auto"),
                             detail=t("env.backend.none"))
    return core


def _check_environment() -> None:
    try:
        from .env import check_environment

        check_environment(log_result=True)
    except Exception:  # pragma: no cover - diagnostics must never stop the command
        log.warning("environment check failed", exc_info=True)


def _peak_memory() -> int | None:
    """Peak memory of this process so far, in bytes: the peak working set on Windows
    (psutil), the maximum resident set size elsewhere; ``None`` when it cannot be told."""
    try:
        import psutil

        peak = getattr(psutil.Process().memory_info(), "peak_wset", None)
        if peak:
            return int(peak)
    except Exception:  # psutil missing or failing: the next source
        log.debug("psutil could not tell the peak memory", exc_info=True)
    try:
        import resource  # POSIX
    except ImportError:  # pragma: no cover - Windows without psutil
        return None
    try:  # pragma: no cover - POSIX only
        maxrss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (OSError, ValueError, AttributeError):  # pragma: no cover
        return None
    return maxrss if sys.platform == "darwin" else maxrss * 1024  # pragma: no cover - KiB on Linux


def _memory_warning(console: _Console, args: argparse.Namespace, peak_before: int | None) -> None:
    """One warning when the peak memory of the process, reached during this command,
    exceeds ``Settings.memory_limit_bytes`` (both numbers and what to change); the command
    is not aborted. Never raises."""
    try:
        from .settings import get_settings

        peak = _peak_memory()
        if peak is None or (peak_before is not None and peak <= peak_before):
            return  # unknown, or an earlier peak of an in-process caller: not this command's
        settings = get_settings()
        limit = int(settings.memory_limit_bytes)
        if limit <= 0 or peak <= limit:
            return
        sorting = args.command == "sort" or bool(getattr(args, "sort_fields", None))
        log.info("peak memory %d bytes is above the memory limit (%d bytes)", peak, limit)
        console.warn(t("cli.warn.memory_peak_sort" if sorting else "cli.warn.memory_peak",
                       used=_mem_text(peak), limit=_mem_text(limit), setting=t("settings.field.memory_limit_bytes"),
                       large_setting=t("settings.field.large_node_bytes"),
                       large=_mem_text(int(settings.large_node_bytes))))
    except Exception:  # pragma: no cover - a diagnostic must never change the result
        log.debug("memory check failed", exc_info=True)


def _print_usage_error(exc: _UsageError) -> None:
    try:
        _write(sys.stderr, exc.parser.format_usage())
        _write(sys.stderr, t("cli.argparse.error") % {"prog": exc.parser.prog, "message": exc.message})
        _write(sys.stderr, t("cli.usage.help_hint", prog=exc.parser.prog) + "\n")
    except _OutputClosed:  # pragma: no cover
        pass
    _flush(sys.stderr)


def _internal_error(console: _Console, exc: BaseException, log_file: str | None) -> None:
    log.critical("internal error", exc_info=(type(exc), exc, exc.__traceback__))
    lines = [t("cli.error.internal", detail=f"{type(exc).__name__}: {exc}")]
    if log_file:
        lines.append(t("cli.error.see_log", path=log_file))
    if console.verbose >= 2:
        lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
    console.err("\n".join(lines))


def _run(args: argparse.Namespace, console: _Console, cancel: CancelToken, log_file: str | None,
         backend_note: str | None) -> int:
    try:
        if args.version:
            try:
                core = _load_core()
            except UserInputError as exc:
                console.out_lines(_version_lines(None, str(exc.params.get("detail") or "")))
                return EXIT_USER
            console.out_lines(_version_lines(str(core.BACKEND)))
            return EXIT_OK
        _load_core()
        _check_environment()
        if backend_note:
            console.warn(backend_note)
        ctx = _Ctx(args, console, cancel)
        with _stdin_inputs(ctx):   # "-" as INPUT: spooled first, replaced by the spool file's path
            return _COMMANDS[args.command](ctx)
    except _OutputClosed:
        return EXIT_OK
    except (Cancelled, KeyboardInterrupt):
        log.info("cancelled")
        console.err(t("error.cancelled"))
        return EXIT_CANCELLED
    except FileChangedError as exc:
        log.info("input changed: %s", exc.message)
        console.error(exc)
        return EXIT_JSON
    except (JsonSyntaxError, FileFormatError) as exc:
        log.info("input error: %s", exc.message)
        console.error(exc)
        return EXIT_JSON
    except JsonReshapeError as exc:
        log.info("user error: %s", exc.message)
        if isinstance(exc, PathSyntaxError) and getattr(exc, CLI_HINT_ATTR, None) is None:
            # the general examples end with "#120", a record number (Ctrl+G), not a path
            _set_hint(exc, t("cli.hint.path_syntax"))
        console.error(exc)
        return EXIT_USER
    except OSError as exc:
        log.warning("file system error", exc_info=True)
        console.error(_os_error(exc))
        return EXIT_USER
    except Exception as exc:
        _internal_error(console, exc, log_file)
        return EXIT_INTERNAL


def main(argv: Sequence[str] | None = None, *, cancel: CancelToken | None = None) -> int:
    """Run the command line ``argv`` (default ``sys.argv[1:]``) and return the exit code.

    ``cancel``: a :class:`~jsonreshape.tasks.CancelToken` shared by every operation (a new one
    by default); Ctrl+C / Ctrl+Break cancel it.
    """
    started = time.perf_counter()
    peak_before = _peak_memory()  # the memory check counts only a peak reached by this command
    raw = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
    _setup_streams()
    log_file = _start_logging(0)
    set_language(_choose_language(_scan_option(raw, "--lang")))
    try:
        with localized_argparse():
            args = _parse(build_parser(), _join_dash_values(raw))
    except _ParserExit as exc:
        _flush(sys.stdout)
        return exc.status
    except _UsageError as exc:
        with localized_argparse():
            _print_usage_error(exc)
        log.info("usage error: %s (arguments: %s)", exc.message, raw)
        return EXIT_USER
    if args.lang:
        set_language(args.lang)
    quiet = bool(args.quiet)
    verbose = int(args.verbose or 0)
    if verbose:
        log_file = _start_logging(verbose) or log_file
    try:
        from . import log as applog

        applog.install_excepthooks()
    except Exception:  # pragma: no cover
        pass
    backend_note = _apply_backend(args.backend)
    token = cancel if cancel is not None else CancelToken()
    console = _Console(quiet=quiet, verbose=verbose)
    console.attach_log_filter()
    interrupts = _Interrupts(token)
    log.info("jsonreshape %s: %s", __version__, " ".join(raw))
    rc = EXIT_INTERNAL
    try:
        interrupts.install()
        rc = _run(args, console, token, log_file, backend_note)
    except KeyboardInterrupt:  # a second Ctrl+C outside the command itself
        console.err(t("error.cancelled"))
        rc = EXIT_CANCELLED
    finally:
        interrupts.restore()
        if args.command is not None and not args.version:
            _memory_warning(console, args, peak_before)
        console.finish()
        log.info("exit code %d after %.2f s", rc, time.perf_counter() - started)
        if verbose:
            _start_logging(0)  # remove the console handler again (in-process callers)
    if sys.stdout is not None and sys.stdout is sys.__stdout__:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            # the reader went away: silence the flush at interpreter exit as well
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
            except (OSError, ValueError):  # pragma: no cover
                pass
    return rc
