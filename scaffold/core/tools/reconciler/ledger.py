"""
Ledger module — load, validate, and append to reconcile/ledger.yaml.

The ledger is an append-only YAML list of op dicts. It is the single source
of truth for KB repo-lifecycle events (rename/relocate/retire/absorb).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

KNOWN_OPS: frozenset[str] = frozenset({"retire", "relocate", "rename", "absorb"})


def load_ledger(path: Path | str) -> list[dict[str, Any]]:
    """Load ops from the ledger YAML file.

    Returns an empty list for a missing, empty, or comment-only file.
    All values are returned as Python strings/dicts exactly as written
    (dates stay strings — we load with yaml.safe_load(str_value, Loader)
    but emit with quoted strings to prevent implicit date coercion on
    round-trip).
    """
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    # Strip comment and blank header lines to get the YAML data portion.
    # yaml.safe_load handles comment lines natively, but we need to
    # ensure date strings survive the round-trip (see _dump_ops).
    data = yaml.safe_load(text)
    if not data:
        return []
    if not isinstance(data, list):
        raise ValueError(f"ledger.yaml must be a YAML list, got {type(data)}")
    # Normalise date values: yaml.safe_load may parse unquoted ISO dates as
    # datetime.date objects. Convert them back to strings so callers always
    # receive plain dicts with string values, matching the original op shape.
    return [_normalise_op(op) for op in data]


def _normalise_op(op: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of op with datetime.date/datetime.datetime values
    converted to ISO-8601 strings."""
    import datetime

    if not isinstance(op, dict):
        raise ValueError(
            f"Each ledger entry must be a mapping, got {type(op)}: {op!r}"
        )

    result = {}
    for k, v in op.items():
        if isinstance(v, datetime.date):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "retire": frozenset({"owner"}),
    "relocate": frozenset({"name", "to", "group"}),
    "rename": frozenset({"old", "new"}),  # to/repo optional
    "absorb": frozenset({"from", "into", "subpath"}),
}


def validate_op(op: dict[str, Any]) -> None:
    """Raise ValueError if op is missing required fields or has an unknown op type."""
    if "op" not in op:
        raise ValueError("op dict must have an 'op' field")
    if "date" not in op:
        raise ValueError("op dict must have a 'date' field")
    if op["op"] not in KNOWN_OPS:
        raise ValueError(
            f"Unknown op {op['op']!r}. Known ops: {sorted(KNOWN_OPS)}"
        )
    missing = REQUIRED_FIELDS.get(op["op"], frozenset()) - op.keys()
    if missing:
        raise ValueError(
            f"op {op['op']!r} missing required field(s): {sorted(missing)}"
        )


def append_op(path: Path | str, op: dict[str, Any]) -> None:
    """Append op to the ledger file at path.

    - Validates op before writing.
    - No-op (deduplication) if an identical op already exists.
    - Preserves the file's leading comment/blank header lines.
    - Writes atomically (temp file + os.replace via vault.atomic_write).
    - Dates are emitted as quoted strings to survive yaml round-trips.
    """
    from tools.reconciler import vault as vault_mod

    validate_op(op)
    path = Path(path)

    # Read existing content (or start fresh).
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""

    # Extract leading comment/blank lines (the header block).
    header_lines: list[str] = []
    for line in existing_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped == "" or stripped == "\n":
            header_lines.append(line)
        else:
            # First non-comment, non-blank content line: stop.
            break

    # Load existing ops (reuse load_ledger for date normalisation).
    existing_ops = load_ledger(path)

    # Deduplicate: if the exact same op (after normalisation) already exists,
    # do nothing. Normalise the incoming op for the comparison too.
    normalised_incoming = _normalise_op(op)
    if normalised_incoming in existing_ops:
        return

    new_ops = existing_ops + [normalised_incoming]

    # Serialise: use yaml.dump with default_flow_style=False (block style),
    # sort_keys=False. Dates must be emitted as quoted strings so that
    # yaml.safe_load on re-read returns a string, not datetime.date.
    body = _dump_ops(new_ops)

    # Reconstruct: header + body.
    header = "".join(header_lines)
    # Ensure a single newline separator between header and YAML list.
    if header and not header.endswith("\n\n"):
        # Remove any trailing newlines and add exactly one.
        header = header.rstrip("\n") + "\n"
    content = header + body

    vault_mod.atomic_write(path, content)


def _dump_ops(ops: list[dict[str, Any]]) -> str:
    """Serialise the op list to a YAML string.

    Dates are forced to strings via a custom representer so that yaml.safe_load
    on re-read returns '2026-06-22' (str), not datetime.date(2026, 6, 22).
    """
    import datetime

    class _Dumper(yaml.SafeDumper):
        pass

    def _represent_str(dumper: _Dumper, data: str) -> yaml.ScalarNode:  # type: ignore[override]
        # For strings that look like ISO dates, emit them with single-quote
        # style so the YAML reader never tries to parse them as dates.
        import re
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", data):
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="'")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    def _represent_date(dumper: _Dumper, data: datetime.date) -> yaml.ScalarNode:  # type: ignore[override]
        return dumper.represent_scalar("tag:yaml.org,2002:str", data.isoformat(), style="'")

    _Dumper.add_representer(str, _represent_str)  # type: ignore[arg-type]
    _Dumper.add_representer(datetime.date, _represent_date)  # type: ignore[arg-type]
    _Dumper.add_representer(datetime.datetime, _represent_date)  # type: ignore[arg-type]

    return yaml.dump(ops, Dumper=_Dumper, default_flow_style=False, sort_keys=False, allow_unicode=True)
