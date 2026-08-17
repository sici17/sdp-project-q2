"""Generate one QR code per machine in the fleet.

On a plant floor an operator identifies the machine in front of them by
scanning a code applied to it. Each code here encodes a URL to this platform
pointing at one machine, using its **serial number**, because that is what is
stamped on the machine plate and what names its manual:

    https://<host>/machines/17203

The platform resolves either the serial number or the machine identifier, so a
code printed against either scheme keeps working.

Usage
-----
    python -m pip install segno
    python scripts/generate_qr.py --base-url http://192.168.1.20:5173

Writes an SVG per machine plus a printable index into ``docs/qr/``. The SVGs are
self-contained, so the sheet prints from any browser with no assets to resolve.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = REPO_ROOT / "data" / "arol_q2.sqlite"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "qr"
DEFAULT_BASE_URL = "http://localhost:5173"


def fleet(database: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """
            SELECT m.machineId, m.serialNumber, m.plantLocation, mm.modelCode
            FROM Machines m
            JOIN MachineModels mm ON mm.modelId = m.modelId
            ORDER BY m.companyId, m.machineId;
            """
        ).fetchall()
    finally:
        connection.close()


def build_index(rows: list[tuple[sqlite3.Row, str]]) -> str:
    cards = "\n".join(
        f"""  <figure class="tag">
    <img src="{escape(row['serialNumber'])}.svg" alt="QR code for machine {escape(row['machineId'])}" />
    <figcaption>
      <strong>{escape(row['modelCode'])}</strong>
      <span>{escape(row['machineId'])} &middot; serial {escape(row['serialNumber'])}</span>
      <span>{escape(row['plantLocation'])}</span>
      <code>{escape(url)}</code>
    </figcaption>
  </figure>"""
        for row, url in rows
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>AROL Q2 - machine QR tags</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1c2b36; }}
  h1 {{ font-size: 1.3rem; }}
  p {{ color: #56707c; max-width: 40rem; }}
  .sheet {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: 1rem; }}
  .tag {{ margin: 0; border: 1px solid #d3dedf; border-radius: 8px; padding: 1rem; text-align: center; }}
  .tag img {{ width: 100%; max-width: 11rem; }}
  figcaption {{ display: grid; gap: 0.2rem; margin-top: 0.6rem; font-size: 0.8rem; }}
  code {{ font-size: 0.68rem; color: #56707c; word-break: break-all; }}
  @media print {{ .tag {{ break-inside: avoid; }} p {{ display: none; }} }}
</style>
</head>
<body>
<h1>Machine QR tags</h1>
<p>One tag per machine in the fleet. Each encodes a link to this platform scoped
to that machine, keyed by the serial number stamped on the machine plate.
Print and apply to the corresponding machine.</p>
<div class="sheet">
{cards}
</div>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        import segno
    except ModuleNotFoundError:
        print(
            "error: segno is required to generate QR codes.\n"
            "Install it with: python -m pip install segno",
            file=sys.stderr,
        )
        return 2

    if not args.database.is_file():
        print(f"error: fleet dataset not found at {args.database}", file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    args.output.mkdir(parents=True, exist_ok=True)

    written = []
    for row in fleet(args.database):
        url = f"{base}/machines/{row['serialNumber']}"
        target = args.output / f"{row['serialNumber']}.svg"
        # Error correction M keeps the code readable with a little wear or
        # grease on a plant floor without making the tag much denser.
        segno.make(url, error="m").save(str(target), scale=6, border=2)
        written.append((row, url))
        print(f"  {row['machineId']:<10} {row['serialNumber']:<8} -> {target.name}")

    index = args.output / "index.html"
    index.write_text(build_index(written), encoding="utf-8")
    print(f"\nWrote {len(written)} QR codes and {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
