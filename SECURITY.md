# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Write to **erkin.pardayev@gmail.com** (or Telegram
[@ErkinPardayev](https://t.me/ErkinPardayev)) with a description, the version and, if possible, a file that
reproduces it. You will get an answer within a few days; a fix is released as soon as it is ready and credited to you
if you wish.

## What the program does and does not do

* jsonreshape works **offline**: it never sends anything over the network — no telemetry, no update checks, no
  remote loading. URLs found in data are opened in the browser only after an explicit confirmation.
* The original file is never modified in place; saves and exports write a new file and rename it atomically.
* Filters and bulk operations are evaluated by the program's own parser — there is no `eval` / `exec` of user
  input (a static test enforces it).
* The Windows "Open with" registration writes only per-user keys under `HKCU\Software\Classes` and removes exactly
  what it wrote.
* Settings, caches, logs and edit layers live under `%LOCALAPPDATA%\jsonreshape` (or `JSONRESHAPE_HOME`); the log
  records paths, sizes and errors, never file contents.
