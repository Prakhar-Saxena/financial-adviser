"""Validate a browsing run's manifest.json and run_report.json (SPEC §9.1, §9.6).

The manifest is the agent's claim about each file. Validation here is structural:
schema, known card ids, files exist inside the run folder, nothing unlisted.
Parsers later check each document's content, and the document wins over the manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

from fin.config import REPO_ROOT

SCHEMA_PATH = REPO_ROOT / "sync" / "manifest.schema.json"
# Files a run folder may hold that aren't listed in the manifest.
RUN_FOLDER_EXTRAS = {"manifest.json", "run_report.json", "playbook_proposed.md", "guard.log"}
SESSION_LOG_SUFFIXES = (".py", ".yml", ".yaml", ".log")


@dataclass
class ManifestCheck:
    manifest: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _validator(def_name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    return jsonschema.Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/{def_name}"}, registry=registry
    )


def _schema_errors(doc: Any, def_name: str, label: str) -> list[str]:
    errs = sorted(_validator(def_name).iter_errors(doc), key=lambda e: list(e.path))
    return [f"{label}: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errs]


def _load(path: Path, errors: list[str]) -> Any:
    if not path.exists():
        errors.append(f"{path.name} is missing")
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        errors.append(f"{path.name} is not valid JSON: {e.msg} (line {e.lineno})")
        return None


def is_session_log(name: str) -> bool:
    """Playwright --save-session / --codegen output kept for Phase 6."""
    return name.startswith(("session", "trace")) or name.endswith(SESSION_LOG_SUFFIXES)


def validate_run(
    run_dir: Path, site: str, allowed_card_ids: set[str], run_date: str | None = None,
    imported_dir: Path | None = None,
) -> ManifestCheck:
    """Check a run folder. `imported_dir` (raw/<site>/<date>/) also counts as "present",
    so a folder can be re-imported after its files moved there."""
    check = ManifestCheck()
    manifest = _load(run_dir / "manifest.json", check.errors)
    report = _load(run_dir / "run_report.json", check.errors)

    if manifest is not None:
        check.errors += _schema_errors(manifest, "manifest", "manifest.json")
    if report is not None:
        check.errors += _schema_errors(report, "run_report", "run_report.json")
    if check.errors:
        return check
    check.manifest, check.report = manifest, report

    for doc, name in ((manifest, "manifest.json"), (report, "run_report.json")):
        if doc["site"] != site:
            check.errors.append(f"{name}: site is {doc['site']!r}, expected {site!r}")
        if run_date and doc["run_date"] != run_date:
            check.errors.append(f"{name}: run_date {doc['run_date']} != {run_date}")

    listed: set[str] = set()
    for i, entry in enumerate(manifest["files"]):
        where = f"manifest.json: files/{i} ({entry['file']})"
        if entry["file"] in listed:
            check.errors.append(f"{where}: listed twice")
        listed.add(entry["file"])
        path = run_dir / entry["file"]
        if imported_dir is not None and not path.exists():
            path = imported_dir / entry["file"]
        if not path.is_file() or path.is_symlink():
            check.errors.append(f"{where}: file not found in run folder")
        card = entry["card_id"]
        if card is not None and card not in allowed_card_ids:
            check.errors.append(f"{where}: unknown card_id {card!r}")
        if (entry["kind"] == "selftest") != (site == "selftest"):
            check.errors.append(f"{where}: kind {entry['kind']!r} not allowed for site {site}")
        ps, pe = entry["period_start"], entry["period_end"]
        if ps and pe and ps > pe:
            check.errors.append(f"{where}: period_start after period_end")

    for item in report["collected"] + report["gaps"]:
        card = item["card_id"]
        if card is not None and card not in allowed_card_ids:
            check.errors.append(f"run_report.json: unknown card_id {card!r}")

    for p in sorted(run_dir.iterdir()):
        evidence = p.suffix == ".png" and f"{p.stem}.json" in listed  # transcript screenshot
        known = (p.name in listed or p.name in RUN_FOLDER_EXTRAS or is_session_log(p.name)
                 or evidence)
        if p.is_file() and not known:
            check.unlisted.append(p.name)
    return check
