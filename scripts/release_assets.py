"""Release helpers of the GitHub release workflow (``.github/workflows/release.yml``; docs/ARCHITECTURE.md section 16
"Distribution") -- a developer tool, not part of the program. It imports nothing of ``jsonreshape`` (the release job
runs it on a plain Python 3.11+ without the package installed) and needs only the standard library.

    python scripts/release_assets.py version
        prints the version of ``jsonreshape/__init__.py`` (read as text, nothing is imported)

    python scripts/release_assets.py check-version --tag v0.1.0
        the tag, ``jsonreshape/__init__.py``, ``pyproject.toml`` ([project] version) and ``rust/Cargo.toml``
        ([package] version) must carry the same version; exit 1 with every difference otherwise. A version without a
        CHANGELOG.md section is reported as a warning (the notes then use the "[Unreleased]" section).

    python scripts/release_assets.py sums <folder> [--out SHA256SUMS.txt]
        ``SHA256SUMS.txt`` over every file of the folder: the ``sha256sum`` format ("<hex>  <name>", two spaces),
        sorted by name, the sums file itself excluded, files hashed in 1 MiB pieces, written to a temporary file and
        renamed. Verify with ``sha256sum -c SHA256SUMS.txt --ignore-missing``.

    python scripts/release_assets.py notes --version 0.1.0 [--changelog CHANGELOG.md] [--assets <folder>]
                                           [--repo Erkinbek/jsonreshape] [--out release_notes.md]
        the release notes skeleton (Markdown, to stdout without --out): the CHANGELOG.md section of the version
        (``## [0.1.0] ...`` up to the next ``## `` heading, link definitions dropped; the "[Unreleased]" section and a
        warning when the version has none), how to install each asset found in the folder (installer, portable zip
        / tar.gz, wheels and sdist), the asset table with sizes and SHA-256, the checksum file. The author
        edits the draft release before publishing it.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMS_NAME = "SHA256SUMS.txt"
DEFAULT_REPO = "Erkinbek/jsonreshape"
CHUNK = 1 << 20

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_HEADING_RE = re.compile(r"^## (?:\[(?P<version>[^\]]+)\].*|(?P<other>.+))$", re.MULTILINE)
_LINK_DEF_RE = re.compile(r"^\[[^\]]+\]:\s+\S+\s*$")

#: (pattern on the asset name, how to install it) -- the first match wins; unmatched assets are listed without a line.
_INSTALL_ROWS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-setup-x64\.exe$"),
     "**Windows installer** -- for the current user (no administrator rights) or for all users; a Start menu "
     "shortcut, optionally a desktop shortcut and the command line (`jsonreshape`) on the PATH; uninstall from "
     "Settings > Apps."),
    (re.compile(r"-windows-x64\.zip$"),
     "**Windows, portable** -- unzip anywhere and run `jsonreshape-gui.exe` (the window) or `jsonreshape.exe` "
     "(the command line); the two share the `_internal` folder, so keep it next to them."),
    (re.compile(r"-linux-\w+\.tar\.gz$"),
     "**Linux (x86_64), portable** -- `tar xzf` and run `jsonreshape/jsonreshape-gui` or `jsonreshape/jsonreshape`."),
    (re.compile(r"-macos-\w+\.tar\.gz$"),
     "**macOS (Apple silicon), portable** -- `tar xzf` and run `jsonreshape/jsonreshape-gui` or "
     "`jsonreshape/jsonreshape` (an unsigned build: allow it once in System Settings > Privacy & Security)."),
    (re.compile(r"\.whl$|^jsonreshape-[\w.]+\.tar\.gz$"),
     "**Python 3.11+** -- `pip install jsonreshape` from PyPI, or `pip install <file>`; "
     "`pip install \"jsonreshape[gui]\"` adds the window (PySide6). One wheel per platform works with every "
     "Python >= 3.11 (abi3); the sdist builds the Rust core locally."),
)


# ==================================================================================================
# versions
# ==================================================================================================
def read_version(root: Path = ROOT) -> str:
    """``__version__`` of ``<root>/jsonreshape/__init__.py``."""
    text = (Path(root) / "jsonreshape" / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if match is None:
        raise ValueError("no __version__ in jsonreshape/__init__.py")
    return match.group(1)


def pyproject_version(root: Path = ROOT) -> str:
    data = tomllib.loads((Path(root) / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def cargo_version(root: Path = ROOT) -> str:
    data = tomllib.loads((Path(root) / "rust" / "Cargo.toml").read_text(encoding="utf-8"))
    return str(data["package"]["version"])


def check_versions(root: Path, tag: str) -> list[str]:
    """Every disagreement between ``tag`` (``v<version>``) and the three version sources (empty = release it)."""
    problems: list[str] = []
    if not tag.startswith("v") or len(tag) < 2:
        problems.append(f"the tag must be v<version>: {tag!r}")
    expected = tag[1:] if tag.startswith("v") else tag
    sources = (("jsonreshape/__init__.py", read_version), ("pyproject.toml", pyproject_version),
               ("rust/Cargo.toml", cargo_version))
    for name, reader in sources:
        try:
            found = reader(root)
        except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{name}: cannot read the version ({exc})")
            continue
        if found != expected:
            problems.append(f"{name} has version {found}, the tag says {expected}")
    return problems


# ==================================================================================================
# checksums
# ==================================================================================================
def sha256_file(path: Path) -> str:
    """Lower-case hex SHA-256 of a file, read in 1 MiB pieces."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def write_sums(folder: Path, out_name: str = SUMS_NAME) -> Path:
    """``<folder>/<out_name>`` in the ``sha256sum`` format over every file of ``folder`` (not the sums file)."""
    folder = Path(folder)
    files = sorted((p for p in folder.iterdir() if p.is_file() and p.name != out_name), key=lambda p: p.name)
    if not files:
        raise FileNotFoundError(f"no files to hash in {folder}")
    lines = [f"{sha256_file(p)}  {p.name}" for p in files]
    out = folder / out_name
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(out)
    return out


def read_sums(path: Path) -> dict[str, str]:
    """``{name: hex}`` of a sums file."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            out[name.lstrip("*")] = digest
    return out


# ==================================================================================================
# changelog and notes
# ==================================================================================================
def changelog_section(text: str, version: str) -> tuple[str, bool]:
    """``(body, found)``: the body of the ``## [version]`` section of a Keep-a-Changelog file (up to the next ``## ``
    heading, the link definitions at the end dropped, surrounding blank lines removed). Without such a section the
    ``[Unreleased]`` body comes back with ``found=False`` (``""`` when there is none either)."""
    headings = list(_HEADING_RE.finditer(text))
    wanted = version.lower()

    def body_of(index: int) -> str:
        start = headings[index].end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        lines = [ln.rstrip() for ln in text[start:end].splitlines() if not _LINK_DEF_RE.match(ln)]
        return "\n".join(lines).strip("\n")

    for i, heading in enumerate(headings):
        if (heading.group("version") or "").lower() in (wanted, "v" + wanted):
            return body_of(i), True
    for i, heading in enumerate(headings):
        if (heading.group("version") or "").lower() == "unreleased":
            return body_of(i), False
    return "", False


def _human_size(size: int) -> str:
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} MB"
    if size >= 1 << 10:
        return f"{size / (1 << 10):.1f} KB"
    return f"{size} B"


def release_notes(version: str, changelog_text: str, assets: list[Path] | None = None,
                  repo: str = DEFAULT_REPO) -> str:
    """The Markdown skeleton of the release notes (see the module docstring)."""
    section, found = changelog_section(changelog_text, version)
    out: list[str] = [f"# jsonreshape {version}", ""]
    if not found:
        out += [f"> **TODO:** CHANGELOG.md has no `## [{version}]` section yet; the text below is its "
                f"\"Unreleased\" section.", ""]
    if section:
        out += [section, ""]
    files = sorted((p for p in (assets or []) if p.is_file() and p.name != SUMS_NAME), key=lambda p: p.name)
    out += ["## Installation", ""]
    used: set[str] = set()
    for pattern, how in _INSTALL_ROWS:
        matching = [p.name for p in files if pattern.search(p.name)]
        if not matching and not files:
            matching = ["(see the assets)"]
        if matching:
            used.update(matching)
            out.append(f"- {how}  \n  " + ", ".join(f"`{n}`" for n in matching))
    out += ["", f"Verify a download against `{SUMS_NAME}`: `sha256sum -c {SUMS_NAME} --ignore-missing` "
                "(Linux / macOS) or `Get-FileHash <file> -Algorithm SHA256` (PowerShell).", ""]
    if files:
        out += ["## Assets", "", "| File | Size | SHA-256 |", "|---|---|---|"]
        for p in files:
            out.append(f"| `{p.name}` | {_human_size(p.stat().st_size)} | `{sha256_file(p)}` |")
        out.append("")
    out += ["jsonreshape is offline: no telemetry, no update check. Windows SmartScreen may warn about a download "
            "that is not code-signed (\"More info\" > \"Run anyway\").", "",
            f"Source of this release: https://github.com/{repo}/tree/v{version} -- documentation: "
            f"https://github.com/{repo}#readme", ""]
    return "\n".join(out)


# ==================================================================================================
# command line
# ==================================================================================================
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_assets.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the version of jsonreshape/__init__.py")
    check = sub.add_parser("check-version", help="the tag and the sources must agree")
    check.add_argument("--tag", required=True, help="the git tag, v<version>")
    check.add_argument("--root", default=str(ROOT))
    sums = sub.add_parser("sums", help="write SHA256SUMS.txt over a folder")
    sums.add_argument("folder")
    sums.add_argument("--out", default=SUMS_NAME, help="file name inside the folder (default SHA256SUMS.txt)")
    notes = sub.add_parser("notes", help="write the release notes skeleton")
    notes.add_argument("--version", required=True)
    notes.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    notes.add_argument("--assets", default=None, help="folder with the release assets (for the table)")
    notes.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/name")
    notes.add_argument("--out", default=None, help="output file (default: stdout)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "version":
        print(read_version(ROOT))
        return 0
    if args.command == "check-version":
        root = Path(args.root)
        problems = check_versions(root, args.tag)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if not problems:
            version = args.tag[1:]
            changelog = root / "CHANGELOG.md"
            if changelog.is_file() and not changelog_section(changelog.read_text(encoding="utf-8"), version)[1]:
                print(f"warning: CHANGELOG.md has no section for {version}", file=sys.stderr)
            print(f"ok: {args.tag} = jsonreshape/__init__.py = pyproject.toml = rust/Cargo.toml")
        return 1 if problems else 0
    if args.command == "sums":
        out = write_sums(Path(args.folder), args.out)
        print(f"{out} ({len(read_sums(out))} files)")
        return 0
    if args.command == "notes":
        changelog = Path(args.changelog)
        text = changelog.read_text(encoding="utf-8") if changelog.is_file() else ""
        assets = sorted(Path(args.assets).iterdir()) if args.assets else None
        notes = release_notes(args.version, text, assets, args.repo)
        if args.out:
            Path(args.out).write_text(notes, encoding="utf-8", newline="\n")
            print(f"{args.out} ({len(notes.splitlines())} lines; the release is a draft: review it before publishing)")
        else:
            sys.stdout.write(notes)
        return 0
    return 2  # pragma: no cover - argparse refuses unknown commands


if __name__ == "__main__":
    sys.exit(main())
