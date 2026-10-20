"""Fill the winget manifest templates of jsonreshape for one release (docs/ARCHITECTURE.md section 16
"Distribution") -- a developer tool run by the release workflow and by the author; standard library only.

    python scripts/winget_manifest.py --version 0.1.0 \\
        --installer-url https://github.com/Erkinbek/jsonreshape/releases/download/v0.1.0/jsonreshape-0.1.0-setup-x64.exe \\
        --installer dist/jsonreshape-0.1.0-setup-x64.exe [--sha256 <hex>] [--release-date 2026-10-20] \\
        [--template-dir packaging/winget/Erkinbek.jsonreshape/0.1.0] [--out build/winget]

The templates (``packaging/winget/Erkinbek.jsonreshape/<version>/*.yaml``: the version, installer and default-locale
manifests, schema 1.6.0) carry the placeholders ``{{VERSION}}``, ``{{INSTALLER_URL}}``, ``{{INSTALLER_SHA256}}``
(upper-case hex: computed from ``--installer`` in 1 MiB pieces, or given with ``--sha256``) and ``{{RELEASE_DATE}}``
(today, UTC, when not given); their leading "TEMPLATE" comment lines are dropped (the schema comment stays). Without
``--template-dir`` the newest template folder (by version) is used, so a new release needs no new templates unless
the manifest content changes. The three files are written to
``<out>/manifests/e/Erkinbek/jsonreshape/<version>/`` -- the layout of the microsoft/winget-pkgs repository, ready to
copy into a fork -- and the file the URL points to must be the file that was hashed (a warning when the names
differ). A placeholder left over is an error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_assets import sha256_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "Erkinbek.jsonreshape"
PUBLISHER, PACKAGE = PACKAGE_ID.split(".", 1)
TEMPLATES = ROOT / "packaging" / "winget" / PACKAGE_ID
PLACEHOLDERS = ("VERSION", "INSTALLER_URL", "INSTALLER_SHA256", "RELEASE_DATE")
MANIFEST_NAMES = (f"{PACKAGE_ID}.yaml", f"{PACKAGE_ID}.installer.yaml", f"{PACKAGE_ID}.locale.en-US.yaml")

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
_SCHEMA_COMMENT = "# yaml-language-server:"


def _version_key(name: str) -> tuple:
    numbers = re.findall(r"\d+", name)
    return tuple(int(n) for n in numbers), name


def template_dir(version: str | None = None, templates: Path = TEMPLATES) -> Path:
    """``<templates>/<version>`` when it exists, else the newest template folder."""
    if version and (templates / version).is_dir():
        return templates / version
    folders = [p for p in templates.iterdir() if p.is_dir() and any(p.glob("*.yaml"))]
    if not folders:
        raise FileNotFoundError(f"no winget templates under {templates}")
    return max(folders, key=lambda p: _version_key(p.name))


def render(text: str, values: dict[str, str]) -> str:
    """Substitute every ``{{NAME}}``; ``KeyError`` naming the placeholders that have no value. The template's
    leading comment lines (its "TEMPLATE" header) are dropped, except the ``# yaml-language-server:`` schema line."""
    lines = text.splitlines()
    while lines and lines[0].startswith("#") and not lines[0].startswith(_SCHEMA_COMMENT):
        del lines[0]
    text = "\n".join(lines) + "\n"
    missing = sorted({m.group(1) for m in _PLACEHOLDER_RE.finditer(text)} - set(values))
    if missing:
        raise KeyError(f"no value for the placeholder(s) {', '.join(missing)}")
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)


def output_dir(out: Path, version: str) -> Path:
    """``<out>/manifests/e/Erkinbek/jsonreshape/<version>`` (the winget-pkgs layout)."""
    return Path(out) / "manifests" / PUBLISHER[0].lower() / PUBLISHER / PACKAGE / version


def fill(templates: Path, out: Path, values: dict[str, str]) -> list[Path]:
    """Render every ``*.yaml`` of ``templates`` into :func:`output_dir`; the written paths."""
    target = output_dir(out, values["VERSION"])
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for src in sorted(Path(templates).glob("*.yaml")):
        text = render(src.read_text(encoding="utf-8"), values)
        dest = target / src.name
        dest.write_text(text, encoding="utf-8", newline="\n")
        written.append(dest)
    if not written:
        raise FileNotFoundError(f"no *.yaml templates in {templates}")
    return written


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="winget_manifest.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True, help="the release version (PackageVersion)")
    parser.add_argument("--installer-url", required=True, help="download URL of the installer in the GitHub release")
    parser.add_argument("--installer", default=None, help="local path of that installer (hashed)")
    parser.add_argument("--sha256", default=None, help="its SHA-256 instead of hashing --installer")
    parser.add_argument("--release-date", default=None, help="YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--template-dir", default=None, help="template folder (default: the newest)")
    parser.add_argument("--out", default=str(ROOT / "build" / "winget"), help="output root (default build/winget)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.installer and not args.sha256:
        print("error: --installer <file> or --sha256 <hex> is required", file=sys.stderr)
        return 2
    if args.sha256:
        digest = args.sha256.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            print(f"error: not a SHA-256 hex digest: {digest!r}", file=sys.stderr)
            return 2
    else:
        installer = Path(args.installer)
        if not installer.is_file():
            print(f"error: installer not found: {installer}", file=sys.stderr)
            return 2
        digest = sha256_file(installer)
        url_name = args.installer_url.rstrip("/").rsplit("/", 1)[-1]
        if url_name != installer.name:
            print(f"warning: the URL ends with {url_name!r} but the hashed file is {installer.name!r}", file=sys.stderr)
    values = {
        "VERSION": args.version,
        "INSTALLER_URL": args.installer_url,
        "INSTALLER_SHA256": digest.upper(),
        "RELEASE_DATE": args.release_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
    }
    templates = Path(args.template_dir) if args.template_dir else template_dir(args.version)
    written = fill(templates, Path(args.out), values)
    for path in written:
        print(path)
    print(f"\ntemplates: {templates}\nnext: copy '{output_dir(Path(args.out), args.version).parent}' into a fork of "
          f"microsoft/winget-pkgs, run 'winget validate --manifest <folder>' and "
          f"'winget install --manifest <folder>' in a Windows Sandbox, then open the pull request "
          f"(or: wingetcreate submit <folder>).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
