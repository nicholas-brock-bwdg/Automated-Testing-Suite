"""
generate.py — Stamp out Playwright tests from manifest + templates.

Reads tests/manifest.json and renders one .spec.ts per view per enabled
test type into tests/generated/.  Only regenerates files whose source
view entry or template content has changed since the last run.

Public API:
    generate(manifest_path, templates_dir, output_dir, config) -> dict
        Returns: {written: int, skipped: int, total: int}
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Test types in the order they appear in the manifest tests flags.
TEST_TYPES = ["smoke", "navigation", "components", "auth", "screenshot"]

# State file persisted inside tests/generated/ to track what was last rendered.
_STATE_FILE = ".generate-state.json"


# ===========================================================================
# Internal helpers
# ===========================================================================

def _hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def _render(template: str, variables: dict) -> str:
    """Replace {{key}} placeholders with values from variables."""
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


def _load_state(output_dir: Path) -> dict:
    state_file = output_dir / _STATE_FILE
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict, output_dir: Path) -> None:
    state_file = output_dir / _STATE_FILE
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ===========================================================================
# Public API
# ===========================================================================

def generate(
    manifest_path: Path,
    templates_dir: Path,
    output_dir: Path,
    config: dict,
) -> dict:
    """
    Generate Playwright test files from manifest + templates.

    For each view in the manifest, checks which test types are enabled in the
    'tests' flags and renders the corresponding template.  auth.spec.ts is
    only written for views where requires_auth is true.

    Files are skipped if neither the view entry nor the template has changed
    since the last run (tracked via SHA-256 hashes in .generate-state.json).

    Args:
        manifest_path:  Path to tests/manifest.json
        templates_dir:  Directory containing *.ts.tmpl files
        output_dir:     Destination for generated .spec.ts files
        config:         gateway-config.json dict (for screenshot settings)

    Returns:
        dict with keys: written, skipped, total
    """
    # --- Load manifest ---
    with manifest_path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)

    views = manifest.get("views", [])
    project_name = manifest.get("project", "")

    # Screenshot settings from gateway config
    screenshot_cfg = config.get("screenshot", {})
    threshold = screenshot_cfg.get("threshold", 0.2)
    mask_selectors = screenshot_cfg.get("mask_selectors", [])
    mask_selectors_json = json.dumps(mask_selectors)

    output_dir.mkdir(parents=True, exist_ok=True)

    state   = _load_state(output_dir)
    written = 0
    skipped = 0
    total   = 0

    for view in views:
        view_id       = view["id"]
        view_path     = view["path"]
        view_url      = view["url"]
        requires_auth = view.get("requires_auth", False)
        tests_flags   = view.get("tests", {})

        for test_type in TEST_TYPES:
            if not tests_flags.get(test_type, False):
                continue
            # auth template only makes sense for auth-protected views
            if test_type == "auth" and not requires_auth:
                continue

            total += 1
            state_key   = f"{view_id}.{test_type}"
            output_file = output_dir / f"{view_id}.{test_type}.spec.ts"

            # Load template
            tmpl_file = templates_dir / f"{test_type}.ts.tmpl"
            if not tmpl_file.exists():
                print(
                    f"  WARNING: Template not found: {tmpl_file}",
                    file=sys.stderr,
                )
                continue

            template_content = tmpl_file.read_text(encoding="utf-8")

            # Hash inputs to detect changes
            view_hash = _hash(json.dumps(view, sort_keys=True))
            tmpl_hash = _hash(template_content)

            prev = state.get(state_key, {})
            if (
                prev.get("view_hash") == view_hash
                and prev.get("tmpl_hash") == tmpl_hash
                and output_file.exists()
            ):
                skipped += 1
                continue

            # Render template
            variables = {
                "view_id":             view_id,
                "view_path":           view_path,
                "view_url":            view_url,
                "requires_auth":       str(requires_auth).lower(),
                "project_name":        project_name,
                "threshold":           threshold,
                "mask_selectors_json": mask_selectors_json,
            }
            rendered = _render(template_content, variables)
            output_file.write_text(rendered, encoding="utf-8")

            state[state_key] = {"view_hash": view_hash, "tmpl_hash": tmpl_hash}
            print(f"  {output_file.name}")
            written += 1

    _save_state(state, output_dir)

    print(
        f"\n  Summary: {written} written, {skipped} skipped, {total} total"
        f"  ({len(views)} views)"
    )
    return {"written": written, "skipped": skipped, "total": total}
