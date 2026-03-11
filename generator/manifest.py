"""
manifest.py — Build, validate, diff, and write manifest.json.

The manifest is the stable contract between discovery and test generation.
It is committed to the project repo and only updated via --refresh.

Public API:
    build_manifest(config, views)          -> dict
    validate_manifest(manifest, schema)    -> list[str]   (errors)
    diff_manifest(old, new)                -> dict
    print_diff(diff)                       -> None
    write_manifest_atomic(manifest, dest)  -> None
    build_and_write(config, views, dest)   -> dict
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_VERSION = "1.0"

# Schema path relative to this file:
#   central repo:  generator/manifest.py -> ../config/schema.json
#   project repo:  _ignition_test/generator/manifest.py -> _ignition_test/config/schema.json
_SCHEMA_PATH = Path(__file__).parent.parent / "config" / "schema.json"


# ===========================================================================
# ID generation
# ===========================================================================

def path_to_id(path: str) -> str:
    """
    Convert a view path to a stable manifest ID.

    /Home            -> view__home
    /Reports/Daily   -> view__reports_daily
    /Nav/Top Nav     -> view__nav_top_nav
    """
    clean = path.lstrip("/").lower()
    # Replace any non-alphanumeric character (spaces, dashes, dots) with _
    clean = re.sub(r"[^a-z0-9/]", "_", clean)
    # Replace path separators with _
    clean = clean.replace("/", "_")
    # Collapse consecutive underscores
    clean = re.sub(r"_+", "_", clean).strip("_")
    return f"view__{clean}"


# ===========================================================================
# Build
# ===========================================================================

def build_manifest(config: dict, views: list) -> dict:
    """
    Assemble the manifest dict from gateway config and reconciled view list.

    The auth test flag mirrors requires_auth — only auth-protected views
    need the auth test scenario.
    """
    gateway_url  = config["gateway_url"].rstrip("/")
    project_name = config["project_name"]

    view_entries = []
    for v in views:
        path          = v["path"]
        requires_auth = v.get("requires_auth", False)

        entry: dict = {
            "id":            path_to_id(path),
            "path":          path,
            "url":           f"{gateway_url}/data/perspective/client/{project_name}{path}",
            "discovered_by": v.get("discovered_by", "api"),
            "reachable":     v.get("reachable", True),
            "requires_auth": requires_auth,
            "tests": {
                "smoke":      True,
                "navigation": True,
                "components": True,
                "auth":       requires_auth,
                "screenshot": True,
            },
        }

        # Optional fields — only emit if non-empty
        nav_path = v.get("nav_path") or []
        if nav_path:
            entry["nav_path"] = nav_path

        warnings = v.get("warnings") or []
        if warnings:
            entry["warnings"] = warnings

        view_entries.append(entry)

    return {
        "version":      MANIFEST_VERSION,
        "project":      project_name,
        "gateway":      gateway_url,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "views":        view_entries,
    }


# ===========================================================================
# Validate
# ===========================================================================

def validate_manifest(manifest: dict, schema_path: Path = _SCHEMA_PATH) -> list:
    """
    Validate manifest against config/schema.json.

    Returns a list of error strings (empty list = valid).
    Uses jsonschema if installed; falls back to manual field checks.
    """
    errors: list[str] = []

    # ---- jsonschema (preferred) ----
    try:
        import jsonschema  # type: ignore

        if schema_path.exists():
            with schema_path.open(encoding="utf-8") as fh:
                schema = json.load(fh)
            # Remove $id to prevent jsonschema from trying to fetch it over the network
            schema_copy = {k: v for k, v in schema.items() if k != "$id"}
            validator   = jsonschema.Draft7Validator(schema_copy)
            for err in sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path)):
                path_str = ".".join(str(p) for p in err.absolute_path) or "(root)"
                errors.append(f"{path_str}: {err.message}")
        else:
            print(
                f"  WARNING: Schema not found at {schema_path}. "
                "Falling back to manual validation.",
                file=sys.stderr,
            )
            errors = _manual_validate(manifest)

        return errors

    except ImportError:
        pass  # jsonschema not installed — fall through

    # ---- Manual validation (stdlib only) ----
    return _manual_validate(manifest)


def _manual_validate(manifest: dict) -> list:
    """Lightweight manual schema check (used when jsonschema is unavailable)."""
    errors: list[str] = []

    for key in ("version", "project", "gateway", "generated_at", "views"):
        if key not in manifest:
            errors.append(f"Missing required top-level field: '{key}'")

    if not re.match(r"^\d+\.\d+$", manifest.get("version", "")):
        errors.append(
            f"version must match \\d+\\.\\d+ pattern, got: {manifest.get('version')!r}"
        )

    if not isinstance(manifest.get("views"), list):
        errors.append("'views' must be an array")
        return errors

    valid_discovered = {"api", "browser", "both"}
    required_view    = ["id", "path", "url", "discovered_by", "reachable", "requires_auth", "tests"]
    required_tests   = ["smoke", "navigation", "components", "auth", "screenshot"]

    for i, v in enumerate(manifest["views"]):
        pfx = f"views[{i}]"
        for key in required_view:
            if key not in v:
                errors.append(f"{pfx}: missing required field '{key}'")

        if not re.match(r"^view__[a-z0-9_/]+$", v.get("id", "")):
            errors.append(f"{pfx}.id: invalid format: {v.get('id')!r}")

        if not str(v.get("path", "")).startswith("/"):
            errors.append(f"{pfx}.path: must start with '/'")

        if v.get("discovered_by") not in valid_discovered:
            errors.append(
                f"{pfx}.discovered_by: must be one of {sorted(valid_discovered)}, "
                f"got {v.get('discovered_by')!r}"
            )

        tests = v.get("tests", {})
        for key in required_tests:
            if key not in tests:
                errors.append(f"{pfx}.tests: missing '{key}'")
            elif not isinstance(tests.get(key), bool):
                errors.append(f"{pfx}.tests.{key}: must be boolean")

    return errors


# ===========================================================================
# Diff
# ===========================================================================

def diff_manifest(old: dict, new: dict) -> dict:
    """
    Compare two manifests and return a structured diff.

    Returns:
        added      list   — view entries present in new but not old
        removed    list   — view entries present in old but not new
        changed    list   — entries present in both but with differences
        unchanged  int    — count of identical entries
    """
    old_by_id = {v["id"]: v for v in old.get("views", [])}
    new_by_id = {v["id"]: v for v in new.get("views", [])}

    old_ids = set(old_by_id.keys())
    new_ids = set(new_by_id.keys())

    added   = [new_by_id[i] for i in sorted(new_ids - old_ids)]
    removed = [old_by_id[i] for i in sorted(old_ids - new_ids)]

    changed: list[dict] = []
    unchanged = 0

    # Fields that affect test generation — these are the ones we care about
    SIGNIFICANT = ("reachable", "requires_auth", "discovered_by", "tests")

    for vid in sorted(old_ids & new_ids):
        ov, nv = old_by_id[vid], new_by_id[vid]
        diffs: dict = {}
        for field in SIGNIFICANT:
            if ov.get(field) != nv.get(field):
                diffs[field] = {"old": ov.get(field), "new": nv.get(field)}
        if diffs:
            changed.append({"id": vid, "path": nv["path"], "changes": diffs})
        else:
            unchanged += 1

    return {
        "added":     added,
        "removed":   removed,
        "changed":   changed,
        "unchanged": unchanged,
    }


def print_diff(diff: dict) -> None:
    """Print a human-readable diff summary to stdout."""
    added     = diff["added"]
    removed   = diff["removed"]
    changed   = diff["changed"]
    unchanged = diff["unchanged"]

    if not added and not removed and not changed:
        print("  No manifest changes detected.")
        return

    if added:
        print(f"\n  Added ({len(added)}):")
        for v in added:
            print(f"    + {v['path']:<50}  [{v['id']}]")

    if removed:
        print(f"\n  Removed ({len(removed)}):")
        for v in removed:
            print(f"    - {v['path']:<50}  [{v['id']}]")

    if changed:
        print(f"\n  Changed ({len(changed)}):")
        for c in changed:
            print(f"    ~ {c['path']:<50}  [{c['id']}]")
            for field, vals in c["changes"].items():
                print(f"        {field}: {vals['old']!r} -> {vals['new']!r}")

    print(f"\n  Unchanged: {unchanged}")


# ===========================================================================
# Atomic write
# ===========================================================================

def write_manifest_atomic(manifest: dict, dest: Path) -> None:
    """
    Write manifest.json atomically: write to a .tmp file then rename.
    An existing manifest is never corrupted on partial failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        tmp.replace(dest)  # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    print(f"  Wrote {dest}  ({len(manifest['views'])} views)")


# ===========================================================================
# Combined entry point
# ===========================================================================

def build_and_write(config: dict, views: list, dest: Path) -> dict:
    """
    Build the manifest from config + views, validate, diff against any
    existing manifest, and write atomically.

    Returns the manifest dict.
    Raises RuntimeError if schema validation fails (does not write).
    """
    manifest = build_manifest(config, views)

    # --- Validate ---
    print("\n  Validating manifest against schema...")
    errors = validate_manifest(manifest)
    if errors:
        print(f"\n  VALIDATION FAILED ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"    - {e}", file=sys.stderr)
        raise RuntimeError(
            "Manifest failed schema validation — not writing. "
            "Fix the errors above and retry."
        )
    print("  Schema validation passed.")

    # --- Diff ---
    if dest.exists():
        print("\n  Comparing with existing manifest...")
        try:
            with dest.open(encoding="utf-8") as fh:
                old = json.load(fh)
            d = diff_manifest(old, manifest)
            print_diff(d)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"  WARNING: Could not parse existing manifest for diff: {exc}")

    # --- Write ---
    write_manifest_atomic(manifest, dest)
    return manifest
