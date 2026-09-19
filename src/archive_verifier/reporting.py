"""Self-contained M2 HTML, CSV, and JSON reports."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from . import safety

NOTICE = (
    "This report verifies the integrity of the local Takeout files only. It does NOT prove that the export contains "
    "every item in Google Photos. Compare the total item count with the count shown in Google Photos "
    "before deciding anything."
)


def _filename(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def _write_exclusive(path: Path, content: str, encoding: str = "utf-8") -> Path:
    safety.assert_write_allowed(path, "M2 report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding=encoding, newline="") as stream:
        stream.write(content)
    return path


def write_reports(report_dir: str | Path, run_id: str, payload: dict[str, Any]) -> dict[str, Path]:
    directory = Path(report_dir).resolve()
    base = f"archive-report-{run_id}"
    json_payload = {"notice": NOTICE, **payload}
    json_path = _write_exclusive(
        _filename(directory, base, ".json"), json.dumps(json_payload, ensure_ascii=False, indent=2) + "\n"
    )
    rows = payload.get("failures", [])
    csv_path = _filename(directory, base, ".csv")
    safety.assert_write_allowed(csv_path, "M2 CSV report")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", encoding="utf-8-sig", newline="") as stream:
        stream.write(f"# {NOTICE}\n")
        writer = csv.DictWriter(stream, fieldnames=["relative_path", "media_type", "status", "reason", "last_error"])
        writer.writeheader()
        for row in rows:
            safe = {
                key: ("'" + str(value) if str(value).startswith(("=", "+", "-", "@", "\t", "\r")) else value)
                for key, value in row.items()
            }
            writer.writerow(safe)
    safety.assert_write_allowed(csv_path, "M2 CSV report")
    body = "".join(
        f"<li>{html.escape(str(item.get('relative_path', '')))}: {html.escape(str(item.get('reason', '')))}</li>"
        for item in rows
    )
    html_content = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Archive report</title></head><body>"
        f"<p>{html.escape(NOTICE)}</p><h1>Run {html.escape(run_id)}</h1><ul>{body}</ul></body></html>\n"
    )
    html_path = _write_exclusive(_filename(directory, base, ".html"), html_content)
    return {"json": json_path, "csv": csv_path, "html": html_path}
