#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bootstrap.py — Ignition Perspective Test Automation System
Drop this single file into any Perspective project repo.

Phase 1: Update-check mechanism.
Phase 2: Full bootstrap flow — config, dependencies, file generation.
Phase 3+: Discovery, manifest, test generation (see stubs below).
"""

from __future__ import annotations

import sys
import os
import subprocess
import shutil
import urllib.request
import urllib.error
import json
import re
import argparse
from pathlib import Path


# ---------------------------------------------------------------------------
# Central repo coordinates
# ---------------------------------------------------------------------------
# Set IGNITION_TEST_CENTRAL_REPO in .env to change the repo URL in one place.
# The default below is the current location; update it when the repo migrates.

_DEFAULT_CENTRAL_REPO = "https://raw.githubusercontent.com/nicholas-brock-bwdg/Automated-Testing-Suite/main"
CENTRAL_REPO_RAW = os.environ.get("IGNITION_TEST_CENTRAL_REPO", _DEFAULT_CENTRAL_REPO).rstrip("/")

# Files pulled from the central repo on an update.
# Paths are relative to the central repo root and written to the same
# relative paths in the project repo (under LOCAL_TOOLING_DIR).
UPDATABLE_FILES = [
    "generator/discover.py",
    "generator/manifest.py",
    "generator/generate.py",
    "templates/smoke.ts.tmpl",
    "templates/navigation.ts.tmpl",
    "templates/components.ts.tmpl",
    "templates/auth.ts.tmpl",
    "templates/screenshot.ts.tmpl",
    "helpers/login.ts",
    "helpers/gateway.py",
    "helpers/readiness.py",
    "config/schema.json",
]

# Local directory where pulled files are stored (inside the project repo).
LOCAL_TOOLING_DIR = Path("_ignition_test")

# Local VERSION cache — tracks what was last pulled.
LOCAL_VERSION_FILE = LOCAL_TOOLING_DIR / "VERSION"

# ---------------------------------------------------------------------------
# Phase 2 constants
# ---------------------------------------------------------------------------

GATEWAY_CONFIG_FILE = Path("gateway-config.json")
ENV_EXAMPLE_FILE    = Path(".env.test.example")
PLAYWRIGHT_CONFIG   = Path("playwright.config.ts")
TEST_START          = Path("test-start")
TESTS_DIR           = Path("tests")
GITHUB_WORKFLOW_DIR = Path(".github/workflows")


# ===========================================================================
# PHASE 1 — Update-check mechanism
# ===========================================================================

# ---------------------------------------------------------------------------
# Version utilities
# ---------------------------------------------------------------------------

def parse_semver(version_str: str) -> tuple[int, int, int]:
    """Parse a semver string into a (major, minor, patch) tuple."""
    version_str = version_str.strip()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if not match:
        raise ValueError(f"Invalid semver: {version_str!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def fetch_remote_version() -> str:
    """Fetch the VERSION file from the central repo."""
    url = f"{CENTRAL_REPO_RAW}/VERSION"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Failed to fetch remote VERSION (HTTP {exc.code}): {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error fetching remote VERSION: {exc.reason}") from exc


def read_local_version() -> str | None:
    """Read the locally cached VERSION, or None if not present."""
    if LOCAL_VERSION_FILE.exists():
        return LOCAL_VERSION_FILE.read_text().strip()
    return None


# ---------------------------------------------------------------------------
# File pull
# ---------------------------------------------------------------------------

def fetch_remote_file(relative_path: str) -> bytes:
    """Fetch a single file from the central repo by its relative path."""
    url = f"{CENTRAL_REPO_RAW}/{relative_path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Failed to fetch {relative_path} (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error fetching {relative_path}: {exc.reason}") from exc


def pull_tooling(remote_version: str) -> None:
    """Download all updatable files from the central repo and cache them locally."""
    print(f"Pulling tooling version {remote_version} from central repo...")
    LOCAL_TOOLING_DIR.mkdir(parents=True, exist_ok=True)

    failed = []
    for rel_path in UPDATABLE_FILES:
        dest = LOCAL_TOOLING_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = fetch_remote_file(rel_path)
            dest.write_bytes(content)
            print(f"  [ok] {rel_path}")
        except RuntimeError as exc:
            print(f"  [fail] {rel_path}: {exc}")
            failed.append(rel_path)

    if failed:
        raise RuntimeError(
            f"Pull incomplete — {len(failed)} file(s) failed: {', '.join(failed)}"
        )

    # Write the cached version only after a fully successful pull.
    LOCAL_VERSION_FILE.write_text(remote_version + "\n")
    print(f"Tooling updated to {remote_version}.")


# ---------------------------------------------------------------------------
# Update-check entry point
# ---------------------------------------------------------------------------

def check_for_updates(force: bool = False) -> bool:
    """Compare local and remote VERSION; pull if remote is newer (or forced).

    Returns True if an update was applied, False otherwise.
    """
    if sys.version_info < (3, 8):
        print(
            f"ERROR: Python 3.8+ required "
            f"(running {sys.version_info.major}.{sys.version_info.minor}).",
            file=sys.stderr,
        )
        sys.exit(1)

    local_raw = read_local_version()

    print("Checking for updates from central repo...")
    try:
        remote_raw = fetch_remote_version()
    except RuntimeError as exc:
        print(f"WARNING: Could not reach central repo — {exc}")
        print("Continuing with local tooling (if available).")
        return False

    remote_ver = parse_semver(remote_raw)

    if local_raw is None:
        print(f"No local tooling found. Pulling version {remote_raw}.")
        pull_tooling(remote_raw)
        return True

    local_ver = parse_semver(local_raw)

    if force:
        print(f"Force-update requested. Pulling version {remote_raw}.")
        pull_tooling(remote_raw)
        return True

    if remote_ver > local_ver:
        print(f"Update available: {local_raw} → {remote_raw}.")
        pull_tooling(remote_raw)
        return True

    print(f"Tooling is up to date (version {local_raw}).")
    return False


# ===========================================================================
# PHASE 2 — Bootstrap flow
# ===========================================================================

# ---------------------------------------------------------------------------
# Config interrogation
# ---------------------------------------------------------------------------

def _prompt(
    label: str,
    env_var: str | None = None,
    default: str | None = None,
    secret: bool = False,
) -> str:
    """Return a config value: env var → interactive prompt → default."""
    if env_var:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val

    hint = f" [{default}]" if default else ""
    display = f"  {label}{hint}: "

    if secret:
        import getpass
        entered = getpass.getpass(display).strip()
    else:
        entered = input(display).strip()

    return entered if entered else (default or "")


def load_existing_config() -> dict | None:
    """Load gateway-config.json if it exists, else return None."""
    if GATEWAY_CONFIG_FILE.exists():
        with GATEWAY_CONFIG_FILE.open() as fh:
            return json.load(fh)
    return None


def interrogate_config() -> dict:
    """Collect gateway config via env vars + interactive prompts."""
    print("\nConfiguring gateway connection (env vars take precedence over prompts):")

    gateway_url  = _prompt("Gateway URL",          "IGNITION_GATEWAY_URL",  "http://localhost:8088")
    project_name = _prompt("Ignition project name","IGNITION_PROJECT_NAME")
    views_dir    = _prompt("Views directory",       "IGNITION_VIEWS_DIR",    "ignition/views")

    mode_raw = _prompt(
        "Gateway mode",
        "IGNITION_GATEWAY_MODE",
        "persistent",
    )
    mode = mode_raw.lower().strip()
    if mode not in ("persistent", "ephemeral"):
        print(f"  WARNING: Unknown mode '{mode}', defaulting to 'persistent'.")
        mode = "persistent"

    config: dict = {
        "mode": mode,
        "project_name": project_name,
        "gateway_url": gateway_url.rstrip("/"),
        "readiness_timeout_seconds": 120,
        "views_directory": views_dir,
        "auth": {
            "username_env": "IGNITION_TEST_USER",
            "password_env": "IGNITION_TEST_PASSWORD",
        },
        "screenshot": {
            "threshold": 0.2,
            "mask_selectors": [],
        },
        "exclude_views": [],
    }

    if mode == "ephemeral":
        compose_file = _prompt(
            "Path to docker-compose.test.yml",
            "IGNITION_COMPOSE_FILE",
            "docker-compose.test.yml",
        )
        config["compose_file"] = compose_file

    return config


def write_gateway_config(config: dict) -> None:
    GATEWAY_CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    print(f"  Wrote {GATEWAY_CONFIG_FILE}")


# ---------------------------------------------------------------------------
# .env.test.example
# ---------------------------------------------------------------------------

def write_env_example() -> None:
    """Write .env.test.example with placeholder values for all env vars."""
    lines = [
        "# Ignition Test Automation — environment variable template",
        "# Copy this file to .env.test and fill in real values.",
        "# Never commit .env.test — it is listed in .gitignore.",
        "",
        "# Gateway connection",
        "IGNITION_GATEWAY_URL=http://localhost:8088",
        "IGNITION_PROJECT_NAME=MyIgnitionProject",
        "",
        "# Test credentials",
        "IGNITION_TEST_USER=admin",
        "IGNITION_TEST_PASSWORD=changeme",
        "",
        "# Gateway mode: persistent | ephemeral",
        "IGNITION_GATEWAY_MODE=persistent",
        "",
        "# Ephemeral mode only",
        "IGNITION_COMPOSE_FILE=docker-compose.test.yml",
        "",
        "# Views directory (relative to project root)",
        "IGNITION_VIEWS_DIR=ignition/views",
        "",
        "# Central repo override — only needed if the tooling repo URL changes",
        "# IGNITION_TEST_CENTRAL_REPO=https://raw.githubusercontent.com/YOUR_ORG/ignition-test-system/main",
    ]
    ENV_EXAMPLE_FILE.write_text("\n".join(lines) + "\n")
    print(f"  Wrote {ENV_EXAMPLE_FILE}")


# ---------------------------------------------------------------------------
# Node tooling
# ---------------------------------------------------------------------------

def _check_node() -> None:
    """Verify Node.js and npm are available; exit with a clear error if not."""
    if not shutil.which("node"):
        print(
            "ERROR: Node.js not found. Install Node.js 18+ before running bootstrap.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not shutil.which("npm"):
        print("ERROR: npm not found. Install npm before running bootstrap.", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(["node", "--version"], capture_output=True, text=True)
    print(f"  Node.js {result.stdout.strip()} detected.")


def _run(cmd: list[str], description: str) -> None:
    """Run a shell command, streaming output. Exit on failure."""
    print(f"\n{description}...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(
            f"\nERROR: '{' '.join(cmd)}' failed (exit {result.returncode}).",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


def install_node_deps() -> None:
    """Write a minimal package.json (if absent) and install npm dependencies."""
    _check_node()

    if not Path("package.json").exists():
        pkg = {
            "name": "ignition-tests",
            "version": "1.0.0",
            "private": True,
            "devDependencies": {
                "@playwright/test": "^1.44.0",
                "agent-browser":    "^0.1.0",
                "dotenv":           "^16.0.0",
            },
        }
        Path("package.json").write_text(json.dumps(pkg, indent=2) + "\n")
        print("  Wrote package.json")

    _run(["npm", "install"], "Installing npm dependencies")
    _run(
        ["npx", "playwright", "install", "chromium"],
        "Installing Playwright Chromium browser",
    )


def install_dogfood_skill() -> None:
    """Install the agent-browser dogfood skill via npx."""
    _run(
        ["npx", "skills", "add", "vercel-labs/agent-browser", "--skill", "dogfood"],
        "Installing dogfood skill (agent-browser)",
    )


# ---------------------------------------------------------------------------
# Ephemeral gateway validation
# ---------------------------------------------------------------------------

def _gwbk_mounted(compose_file: Path) -> bool:
    """Return True if the compose file references a .gwbk file in any volume."""
    return bool(re.search(r"\.gwbk", compose_file.read_text()))


def validate_ephemeral(config: dict) -> None:
    """Validate the docker-compose file exists and mounts a .gwbk backup."""
    compose_path = Path(config.get("compose_file", "docker-compose.test.yml"))

    if not compose_path.exists():
        print(
            f"\nERROR: Ephemeral mode requires '{compose_path}' but it was not found.\n"
            "Create a docker-compose.test.yml that starts an Ignition gateway and\n"
            "mounts a .gwbk backup file, then re-run bootstrap.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"  Found {compose_path}.")

    if not _gwbk_mounted(compose_path):
        print(
            "\nWARNING: No .gwbk file reference found in docker-compose.test.yml.\n"
            "The Ignition gateway must start with your project pre-loaded via a .gwbk\n"
            "gateway backup mounted into the container at startup.\n"
            "Add a volume mount for your .gwbk and set GATEWAY_RESTORE_ON_INIT=true\n"
            "in the compose service environment before running tests.\n",
            file=sys.stderr,
        )
        # Warn but do not halt — the user may have an alternative restore mechanism.


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------

def generate_playwright_config(config: dict) -> None:
    """Write playwright.config.ts configured for this project."""
    gateway_url = config["gateway_url"]
    content = (
        "import { defineConfig, devices } from '@playwright/test';\n"
        "import * as dotenv from 'dotenv';\n"
        "\n"
        "// Load test credentials from .env.test (not committed).\n"
        "dotenv.config({ path: '.env.test' });\n"
        "\n"
        "export default defineConfig({\n"
        "  testDir: './tests/generated',\n"
        "  timeout: 30_000,\n"
        "  retries: process.env.CI ? 1 : 0,\n"
        "  workers: process.env.CI ? 2 : undefined,\n"
        "\n"
        "  use: {\n"
        f"    baseURL: process.env.IGNITION_GATEWAY_URL ?? '{gateway_url}',\n"
        "    headless: true,\n"
        "    screenshot: 'only-on-failure',\n"
        "    video: 'retain-on-failure',\n"
        "  },\n"
        "\n"
        "  projects: [\n"
        "    {\n"
        "      name: 'chromium',\n"
        "      use: { ...devices['Desktop Chrome'] },\n"
        "    },\n"
        "  ],\n"
        "\n"
        "  reporter: [\n"
        "    ['html', { outputFolder: 'playwright-report', open: 'never' }],\n"
        "    ['list'],\n"
        "  ],\n"
        "\n"
        "  // Screenshot baselines are committed to the repo.\n"
        "  snapshotDir: './tests/snapshots',\n"
        "  snapshotPathTemplate: '{snapshotDir}/{testFilePath}/{arg}{ext}',\n"
        "});\n"
    )
    PLAYWRIGHT_CONFIG.write_text(content)
    print(f"  Wrote {PLAYWRIGHT_CONFIG}")


def generate_test_start(config: dict) -> None:
    """Write the test-start shell script and make it executable."""
    mode         = config["mode"]
    compose_file = config.get("compose_file", "docker-compose.test.yml")

    if mode == "ephemeral":
        gateway_up = (
            "# Phase 5: spin up ephemeral gateway\n"
            f'python3 _ignition_test/helpers/gateway.py up "{compose_file}"\n'
        )
        gateway_down = (
            "# Phase 5: tear down ephemeral gateway\n"
            f'python3 _ignition_test/helpers/gateway.py down "{compose_file}"\n'
        )
    else:
        gateway_up = (
            "# Phase 5: verify persistent gateway health\n"
            "# TODO (Phase 5): python3 _ignition_test/helpers/gateway.py healthcheck\n"
        )
        gateway_down = ""

    lines = [
        "#!/usr/bin/env bash",
        "# test-start — Ignition Perspective test runner",
        "# Generated by bootstrap.py. Do not edit manually — re-run bootstrap to regenerate.",
        "#",
        "# Usage:",
        "#   ./test-start                       Run all tests",
        "#   ./test-start --view /home          Run tests for a specific view",
        "#   ./test-start --refresh             Re-run discovery, update manifest",
        "#   ./test-start --update-snapshots    Regenerate screenshot baselines",
        "set -euo pipefail",
        "",
        "# ---------------------------------------------------------------------------",
        "# Load environment variables (.env.test takes precedence over .env)",
        "# ---------------------------------------------------------------------------",
        "if [ -f .env.test ]; then",
        "  set -a; source .env.test; set +a",
        "elif [ -f .env ]; then",
        "  set -a; source .env; set +a",
        "fi",
        "",
        "# ---------------------------------------------------------------------------",
        "# Parse flags",
        "# ---------------------------------------------------------------------------",
        'VIEW=""',
        "REFRESH=false",
        "UPDATE_SNAPSHOTS=false",
        "EXTRA_ARGS=()",
        "",
        "while [[ $# -gt 0 ]]; do",
        '  case "$1" in',
        "    --view)",
        '      VIEW="$2"; shift 2;;',
        "    --refresh)",
        "      REFRESH=true; shift;;",
        "    --update-snapshots)",
        "      UPDATE_SNAPSHOTS=true; shift;;",
        "    *)",
        '      EXTRA_ARGS+=("$1"); shift;;',
        "  esac",
        "done",
        "",
        "# ---------------------------------------------------------------------------",
        "# Tooling update check",
        "# ---------------------------------------------------------------------------",
        "python3 bootstrap.py --update-check",
        "",
        "# ---------------------------------------------------------------------------",
        "# Manifest refresh (Phase 3)",
        "# ---------------------------------------------------------------------------",
        'if [ "$REFRESH" = true ]; then',
        '  echo "Refreshing manifest..."',
        "  # TODO (Phase 3): python3 bootstrap.py --refresh",
        '  echo "  --refresh not yet implemented. Complete Phase 3 first."',
        "fi",
        "",
        "# ---------------------------------------------------------------------------",
        "# Gateway lifecycle",
        "# ---------------------------------------------------------------------------",
        gateway_up.rstrip(),
        "",
        "# ---------------------------------------------------------------------------",
        "# Run Playwright",
        "# ---------------------------------------------------------------------------",
        "PLAYWRIGHT_ARGS=()",
        "",
        'if [ -n "$VIEW" ]; then',
        "  # Derive grep pattern from view path: /home/detail -> view__home_detail",
        '  GREP=$(echo "$VIEW" | sed \'s|^/||; s|/|_|g\')',
        '  PLAYWRIGHT_ARGS+=(--grep "view__${GREP}")',
        "fi",
        "",
        'if [ "$UPDATE_SNAPSHOTS" = true ]; then',
        "  PLAYWRIGHT_ARGS+=(--update-snapshots)",
        "fi",
        "",
        'if [ ${#EXTRA_ARGS[@]} -gt 0 ]; then',
        '  PLAYWRIGHT_ARGS+=("${EXTRA_ARGS[@]}")',
        "fi",
        "",
        'npx playwright test "${PLAYWRIGHT_ARGS[@]}"',
        "EXIT_CODE=$?",
        "",
    ]

    if gateway_down:
        lines += [
            "# ---------------------------------------------------------------------------",
            "# Gateway teardown",
            "# ---------------------------------------------------------------------------",
            gateway_down.rstrip(),
            "",
        ]

    lines.append("exit $EXIT_CODE")
    lines.append("")

    TEST_START.write_text("\n".join(lines))
    TEST_START.chmod(0o755)
    print(f"  Wrote {TEST_START} (executable)")


def _generate_github_action_stub() -> None:
    """Write a stub GitHub Actions workflow. Full implementation is Phase 6."""
    GITHUB_WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    workflow_file = GITHUB_WORKFLOW_DIR / "ignition-tests.yml"

    # Note: ${{ }} is GitHub Actions expression syntax — written as a plain string,
    # not an f-string, so no escaping is needed.
    content = (
        "# ignition-tests.yml — Ignition Perspective test automation\n"
        "# Generated by bootstrap.py. Full implementation: Phase 6.\n"
        "# TODO (Phase 6): PR-scoped view diff, gateway lifecycle, artifact upload.\n"
        "\n"
        "name: Ignition Tests\n"
        "\n"
        "on:\n"
        "  pull_request:\n"
        "    paths:\n"
        "      - 'ignition/views/**'\n"
        "\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: [self-hosted, docker-runner]\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "\n"
        "      - name: Set up Python\n"
        "        uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: '3.11'\n"
        "\n"
        "      - name: Set up Node\n"
        "        uses: actions/setup-node@v4\n"
        "        with:\n"
        "          node-version: '20'\n"
        "\n"
        "      - name: Bootstrap (update check)\n"
        "        run: python3 bootstrap.py --update-check\n"
        "        env:\n"
        "          IGNITION_GATEWAY_URL:    ${{ secrets.IGNITION_GATEWAY_URL }}\n"
        "          IGNITION_TEST_USER:      ${{ secrets.IGNITION_TEST_USER }}\n"
        "          IGNITION_TEST_PASSWORD:  ${{ secrets.IGNITION_TEST_PASSWORD }}\n"
        "\n"
        "      # TODO (Phase 6): gateway lifecycle, PR diff scoping, test run, artifact upload\n"
    )
    workflow_file.write_text(content)
    print(f"  Wrote {workflow_file}")


# ===========================================================================
# PHASE 3 STUB — Discovery & Manifest
# ===========================================================================

def run_discovery(config: dict) -> None:
    """
    TODO (Phase 3): Two-pass view discovery and manifest generation.

    Pass 1 — API (Structure):
        GET {gateway_url}/data/perspective/views?projectName={project_name}
        Flatten the nested view tree into a list of view paths.

    Pass 2 — Browser (Validation):
        Launch agent-browser (dogfood skill), crawl the live app.
        Confirm reachable views, detect auth requirements, record nav paths.
        Detect views linked in nav but absent from the API.

    Reconciliation:
        Merge API and browser results. Tag each view with discovered_by
        ("api", "browser", "both"). Flag discrepancies as warnings.

    Output:
        Write tests/manifest.json conforming to config/schema.json.
        The manifest is the stable contract — do not regenerate it on
        every CI run; only update it via an explicit --refresh PR.

    Signature contract (Phase 3 must honour):
        run_discovery(config: dict) -> None
        Reads:  config["gateway_url"], config["project_name"],
                config["views_directory"], config["exclude_views"]
        Writes: tests/manifest.json
        Raises: RuntimeError on unrecoverable discovery failure
    """
    print("\n[Phase 3 TODO] Discovery not yet implemented — skipping manifest generation.")
    print("  Complete Phase 3, then re-run bootstrap or ./test-start --refresh.")


# ===========================================================================
# PHASE 5 STUBS — Gateway Lifecycle
# ===========================================================================

def spin_up_gateway(config: dict) -> None:
    """
    TODO (Phase 5): Start or verify the gateway before tests run.

    Persistent mode:
        GET {gateway_url}/data/ignition/ping
        Exit with a clear error if unreachable — do not proceed.

    Ephemeral mode:
        docker compose -f {compose_file} up -d
        Poll GET /data/ignition/ping every 5 s up to readiness_timeout_seconds.
        On timeout: print container logs and exit with error.

    Signature contract (Phase 5 must honour):
        spin_up_gateway(config: dict) -> None
        Reads:  config["mode"], config["gateway_url"],
                config.get("compose_file"), config["readiness_timeout_seconds"]
        Raises: RuntimeError if gateway does not become healthy in time
    """
    print("\n[Phase 5 TODO] Gateway spin-up not yet implemented — skipping.")


def tear_down_gateway(config: dict) -> None:
    """
    TODO (Phase 5): Tear down the ephemeral gateway after tests.

    Ephemeral mode only:
        docker compose -f {compose_file} down

    Signature contract (Phase 5 must honour):
        tear_down_gateway(config: dict) -> None
        Reads:  config["mode"], config.get("compose_file")
    """
    if config.get("mode") == "ephemeral":
        print("\n[Phase 5 TODO] Gateway teardown not yet implemented — skipping.")


# ===========================================================================
# BOOTSTRAP ORCHESTRATION
# ===========================================================================

def bootstrap(args: argparse.Namespace) -> None:
    """Full Phase 2+ bootstrap flow, called after check_for_updates()."""
    print("\n=== Ignition Perspective Test Automation — Bootstrap ===")

    # ------------------------------------------------------------------
    # 1. Config — load existing or interrogate
    # ------------------------------------------------------------------
    existing = load_existing_config()
    if existing and not args.reconfigure:
        config = existing
        print(
            f"\nLoaded existing {GATEWAY_CONFIG_FILE}:"
            f"\n  project : {config.get('project_name')}"
            f"\n  gateway : {config.get('gateway_url')}"
            f"\n  mode    : {config.get('mode')}"
        )
        print("  (Pass --reconfigure to re-run setup prompts.)")
    else:
        config = interrogate_config()
        print("\nWriting config files...")
        write_gateway_config(config)

    # ------------------------------------------------------------------
    # 2. .env.test.example
    # ------------------------------------------------------------------
    print("\nWriting environment template...")
    write_env_example()

    # ------------------------------------------------------------------
    # 3. Node dependencies + dogfood skill
    # ------------------------------------------------------------------
    print("\nChecking Node.js tooling...")
    install_node_deps()
    install_dogfood_skill()

    # ------------------------------------------------------------------
    # 4. Ephemeral-specific validation
    # ------------------------------------------------------------------
    if config.get("mode") == "ephemeral":
        print("\nValidating ephemeral gateway configuration...")
        validate_ephemeral(config)

    # ------------------------------------------------------------------
    # 5. Generate project files
    # ------------------------------------------------------------------
    print("\nGenerating project files...")
    generate_playwright_config(config)
    generate_test_start(config)
    _generate_github_action_stub()

    # Ensure test directory skeleton exists (populated in Phase 3/4)
    for subdir in ("generated", "helpers", "snapshots"):
        (TESTS_DIR / subdir).mkdir(parents=True, exist_ok=True)
    print(f"  Ensured {TESTS_DIR}/{{generated,helpers,snapshots}}/ exist")

    # ------------------------------------------------------------------
    # 6. Gateway spin-up (Phase 5 stub)
    # ------------------------------------------------------------------
    spin_up_gateway(config)

    # ------------------------------------------------------------------
    # 7. Discovery (Phase 3 stub)
    # ------------------------------------------------------------------
    manifest = TESTS_DIR / "manifest.json"
    if not manifest.exists() or args.refresh:
        run_discovery(config)
    else:
        print(f"\nFound existing {manifest} — skipping discovery.")
        print("  Pass --refresh to re-run discovery and update the manifest.")

    # ------------------------------------------------------------------
    # 8. Gateway teardown (Phase 5 stub)
    # ------------------------------------------------------------------
    tear_down_gateway(config)

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    print("\n=== Bootstrap complete ===")
    print("Next steps:")
    print("  1. Copy .env.test.example → .env.test and fill in real credentials.")
    print("  2. Complete Phase 3 (discovery) so manifest.json is generated.")
    print("  3. Run ./test-start once Phases 3–5 are implemented.")


# ===========================================================================
# CLI
# ===========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ignition Perspective test automation bootstrap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phases implemented:
  Phase 1  Update-check mechanism
  Phase 2  Config, dependency install, file generation (current)
  Phase 3+ Discovery, manifest, test generation (stubs — see source)
        """,
    )
    parser.add_argument(
        "--update-check",
        action="store_true",
        default=False,
        help="Only check for and apply tooling updates, then exit.",
    )
    parser.add_argument(
        "--force-update",
        action="store_true",
        default=False,
        help="Force a re-pull of all tooling from the central repo even if already current.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        default=False,
        help="(Phase 3) Re-run discovery and update the manifest.",
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        default=False,
        help="Re-run config prompts even if gateway-config.json already exists.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Phase 1: always run update-check first.
    check_for_updates(force=args.force_update)

    if args.update_check:
        sys.exit(0)

    # Phase 2+: full bootstrap flow.
    bootstrap(args)


if __name__ == "__main__":
    main()
