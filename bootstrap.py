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
import select
import subprocess
import shutil
import time
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


# ---------------------------------------------------------------------------
# Compose-file introspection
# ---------------------------------------------------------------------------

def _find_compose_files() -> list[Path]:
    """Return all docker-compose / compose YAML files in the current directory."""
    found: list[Path] = []
    for pattern in ("docker-compose*.yml", "docker-compose*.yaml",
                    "compose*.yml", "compose*.yaml"):
        found.extend(sorted(Path(".").glob(pattern)))
    return found


def _parse_compose_services(compose_path: Path) -> dict[str, str]:
    """
    Return {service_name: raw_block_text} for every service under `services:`.
    Does not resolve YAML anchors — callers apply regex to the raw text.
    """
    text = compose_path.read_text(encoding="utf-8")
    m = re.search(r'^services:\s*\n', text, re.MULTILINE)
    if not m:
        return {}

    services_text = text[m.end():]
    name_re = re.compile(r'^  ([A-Za-z0-9_-]+):\s*$', re.MULTILINE)
    matches = list(name_re.finditer(services_text))

    result: dict[str, str] = {}
    for i, sm in enumerate(matches):
        start = sm.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(services_text)
        result[sm.group(1)] = services_text[start:end]
    return result


def _resolve_compose_vars(value: str) -> str:
    """Replace ${VAR:-default} substitutions with their defaults."""
    value = re.sub(r'\$\{[^:}]+:-([^}]+)\}', r'\1', value)
    value = re.sub(r'\$\{[^}]+\}', '', value)
    return value.strip()


def _parse_ignition_gateways(compose_path: Path) -> list[dict]:
    """
    Return one dict per Ignition gateway service found in a compose file.
    Detection: service block contains GATEWAY_PUBLIC_ADDRESS (only set on
    bwdesigngroup/ignition-docker and similar images).

    Each dict has:
      name          str   — compose service name
      url           str   — http:// URL derived from GATEWAY_PUBLIC_ADDRESS
      projects      list  — [(local_path, project_name), ...] all /workdir/projects mounts
    """
    services = _parse_compose_services(compose_path)
    gateways: list[dict] = []

    vol_re = re.compile(r'(\./[^\s:]+):/workdir/projects/([A-Za-z0-9_-]+)')

    for svc_name, block in services.items():
        gpa_m = re.search(r'GATEWAY_PUBLIC_ADDRESS:\s*(\S+)', block)
        if not gpa_m:
            continue

        address = _resolve_compose_vars(gpa_m.group(1))
        url = f"http://{address}"

        # Collect every project mount — no filtering; user picks.
        seen: set[str] = set()
        projects: list[tuple[str, str]] = []
        for vm in vol_re.finditer(block):
            local, proj_name = vm.group(1), vm.group(2)
            if proj_name not in seen:
                seen.add(proj_name)
                projects.append((local, proj_name))

        gateways.append({"name": svc_name, "url": url, "projects": projects})

    return gateways


def _detect_views_dir(local_project_path: str) -> str:
    """
    Resolve the Perspective views directory for a project mounted at
    local_project_path. Falls back to 'ignition/views' if not found.
    """
    candidate = (
        Path(local_project_path)
        / "com.inductiveautomation.perspective"
        / "views"
    )
    if candidate.exists():
        return str(candidate).lstrip("./").lstrip("/")
    return "ignition/views"


# ---------------------------------------------------------------------------
# Interactive gateway + project selection
# ---------------------------------------------------------------------------

def _select_from_list(prompt: str, options: list[str], default: int = 1) -> int:
    """Prompt the user to pick from a numbered list. Returns 0-based index."""
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip() or str(default)
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(options)}.")


def _pick_gateway_and_project() -> tuple[str, str, str]:
    """
    Scan compose files, present gateways, let user select one,
    then select the Ignition project on that gateway.
    Returns (gateway_url, project_name, views_dir).
    """
    # Env-var short-circuit
    env_url = os.environ.get("IGNITION_GATEWAY_URL", "").strip()
    env_proj = os.environ.get("IGNITION_PROJECT_NAME", "").strip()
    if env_url and env_proj:
        views_dir = os.environ.get("IGNITION_VIEWS_DIR", "ignition/views")
        print(f"  Using environment: {env_url}  /  project: {env_proj}")
        return env_url.rstrip("/"), env_proj, views_dir

    # Discover gateways from all compose files
    all_gateways: list[dict] = []
    for cf in _find_compose_files():
        all_gateways.extend(_parse_ignition_gateways(cf))

    if not all_gateways:
        print("  No Ignition gateways found in compose files.")
        url = _prompt("Gateway URL", "IGNITION_GATEWAY_URL", "http://localhost:8088").rstrip("/")
        proj = _prompt("Ignition project name", "IGNITION_PROJECT_NAME")
        views = _prompt("Views directory", "IGNITION_VIEWS_DIR", "ignition/views")
        return url, proj, views

    # --- Pick gateway ---
    gw_labels = [f"{gw['name']}  →  {gw['url']}" for gw in all_gateways]
    gw_labels.append("Enter URL manually")
    print("\n  Ignition gateways found:")
    for i, label in enumerate(gw_labels, 1):
        print(f"    [{i}] {label}")

    gw_idx = _select_from_list("Select gateway", gw_labels)
    if gw_idx == len(all_gateways):
        url = _prompt("Gateway URL", default="http://localhost:8088").rstrip("/")
        proj = _prompt("Ignition project name")
        views = _prompt("Views directory", default="ignition/views")
        return url, proj, views

    chosen_gw = all_gateways[gw_idx]
    gateway_url = chosen_gw["url"]
    projects: list[tuple[str, str]] = chosen_gw["projects"]

    # --- Pick project ---
    if not projects:
        proj = _prompt("Ignition project name", "IGNITION_PROJECT_NAME")
        views = _prompt("Views directory", "IGNITION_VIEWS_DIR", "ignition/views")
        return gateway_url, proj, views

    if len(projects) == 1:
        local_path, proj_name = projects[0]
        print(f"\n  Detected project: {proj_name}  ({local_path})")
        override = input("  Press Enter to confirm, or type a different name: ").strip()
        proj_name = override if override else proj_name
        views_dir = _detect_views_dir(local_path)
    else:
        proj_labels = [f"{pn}  ({lp})" for lp, pn in projects]
        print(f"\n  Projects on {chosen_gw['name']}:")
        for i, label in enumerate(proj_labels, 1):
            print(f"    [{i}] {label}")
        proj_idx = _select_from_list("Select project", proj_labels)
        local_path, proj_name = projects[proj_idx]
        views_dir = _detect_views_dir(local_path)

    print(f"  Views directory detected: {views_dir}")
    views_override = input("  Press Enter to confirm, or type a different path: ").strip()
    views_dir = views_override if views_override else views_dir

    return gateway_url, proj_name, views_dir


# ---------------------------------------------------------------------------
# Config load / write
# ---------------------------------------------------------------------------

def load_existing_config() -> dict | None:
    """Load gateway-config.json if it exists, else return None."""
    if GATEWAY_CONFIG_FILE.exists():
        with GATEWAY_CONFIG_FILE.open(encoding="utf-8") as fh:
            return json.load(fh)
    return None


def interrogate_config() -> dict:
    """Collect gateway config via compose introspection + interactive prompts."""
    print("\nConfiguring gateway connection:")
    gateway_url, project_name, views_dir = _pick_gateway_and_project()

    return {
        "mode": "persistent",
        "project_name": project_name,
        "gateway_url": gateway_url,
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


def write_gateway_config(config: dict) -> None:
    GATEWAY_CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    print(f"  Wrote {GATEWAY_CONFIG_FILE}")


# ---------------------------------------------------------------------------
# .env.test.example
# ---------------------------------------------------------------------------

def update_gitignore() -> None:
    """Append bootstrap-generated paths to .gitignore (skips entries already present)."""
    entries = [
        ("# Ignition test automation — local tooling (pulled from central repo)", None),
        ("_ignition_test/", "_ignition_test/"),
        ("", None),
        ("# Generated project files (local use only)", None),
        ("gateway-config.json", "gateway-config.json"),
        ("playwright.config.ts", "playwright.config.ts"),
        ("test-start", "test-start"),
        (".env.test.example", ".env.test.example"),
        ("tests/", "tests/"),
        (".github/", ".github/"),
        ("", None),
        ("# Node / Playwright", None),
        ("node_modules/", "node_modules/"),
        ("package.json", "package.json"),
        ("package-lock.json", "package-lock.json"),
        ("playwright-report/", "playwright-report/"),
        ("test-results/", "test-results/"),
        ("", None),
        ("# Dogfood skill (installed by bootstrap)", None),
        (".agents/", ".agents/"),
        ("skills-lock.json", "skills-lock.json"),
        ("", None),
        ("# Test credentials — never commit real values", None),
        (".env.test", ".env.test"),
    ]

    gitignore = Path(".gitignore")
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""

    new_lines: list[str] = []
    for line, check in entries:
        if check is None or check not in existing:
            new_lines.append(line)

    if not new_lines:
        print("  .gitignore already up to date.")
        return

    separator = "\n" if existing and not existing.endswith("\n") else ""
    gitignore.write_text(existing + separator + "\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"  Updated .gitignore ({len([l for l in new_lines if l and not l.startswith('#')])} entries added)")


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
    """
    Install the agent-browser dogfood skill.

    The `npx skills add` installer is a TUI that requires a pseudo-TTY.
    We create one via the stdlib `pty` module and send Enter keypresses to
    accept the default selection (Universal .agents/skills — always included).
    Falls back to a manual instruction on non-Unix platforms.
    """
    print("\nInstalling dogfood skill (agent-browser)...")
    try:
        import pty
    except ImportError:
        print(
            "  [Skipped] pty module not available on this platform.\n"
            "  Run manually:\n"
            "    npx skills add vercel-labs/agent-browser --skill dogfood"
        )
        return

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["npx", "skills", "add", "vercel-labs/agent-browser", "--skill", "dogfood"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
    )
    os.close(slave_fd)

    last_enter = 0.0
    try:
        while proc.poll() is None:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                try:
                    os.read(master_fd, 4096)   # drain output so the PTY doesn't block
                except OSError:
                    break
            now = time.monotonic()
            if now - last_enter >= 0.5:
                try:
                    os.write(master_fd, b"\r\n")
                    last_enter = now
                except OSError:
                    break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    proc.wait()
    if proc.returncode not in (0, None):
        print(
            f"  WARNING: dogfood skill installer exited {proc.returncode}.\n"
            "  Run manually if needed:\n"
            "    npx skills add vercel-labs/agent-browser --skill dogfood"
        )
    else:
        print("  Dogfood skill installed.")


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
        "# Manifest refresh",
        "# ---------------------------------------------------------------------------",
        'if [ "$REFRESH" = true ]; then',
        '  echo "Refreshing manifest (re-running discovery)..."',
        "  python3 bootstrap.py --refresh",
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
# PHASE 3 — Discovery & Manifest
# ===========================================================================

def _load_generator_module(name: str):
    """
    Dynamically import a generator module (discover or manifest).

    Search order:
      1. _ignition_test/generator/  — project repo (pulled from central repo)
      2. generator/                 — central repo / development
    """
    import importlib

    gen_dirs = [
        LOCAL_TOOLING_DIR / "generator",   # project repo
        Path("generator"),                  # central / dev
    ]
    gen_dir = next((d for d in gen_dirs if (d / f"{name}.py").exists()), None)

    if gen_dir is None:
        raise RuntimeError(
            f"Cannot locate generator/{name}.py. "
            "Run bootstrap.py to pull tooling before running discovery."
        )

    if str(gen_dir) not in sys.path:
        sys.path.insert(0, str(gen_dir))

    # Reload in case the module was already imported from a different location
    if name in sys.modules:
        del sys.modules[name]

    return importlib.import_module(name)


def run_discovery(config: dict) -> None:
    """
    Two-pass view discovery and manifest generation (Phase 3).

    Imports generator/discover.py and generator/manifest.py dynamically
    so they can be updated via the central repo pull without touching bootstrap.py.

    Reads:  config["gateway_url"], config["project_name"],
            config["views_directory"], config["exclude_views"]
    Writes: tests/manifest.json
    Raises: RuntimeError on unrecoverable failure
    """
    print("\n=== Phase 3 — View Discovery ===")

    try:
        discover = _load_generator_module("discover")
        manifest = _load_generator_module("manifest")
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Two-pass discovery
    views = discover.run(config)

    # Build, validate, diff, and write manifest
    TESTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest.build_and_write(config, views, TESTS_DIR / "manifest.json")

    print("\nManifest written to tests/manifest.json.")


# ===========================================================================
# PHASE 4 — Test Generation
# ===========================================================================

def _copy_login_helper() -> None:
    """Copy helpers/login.ts into tests/helpers/ so spec files can import it."""
    helper_dirs = [
        LOCAL_TOOLING_DIR / "helpers",
        Path("helpers"),
    ]
    src = next((d / "login.ts" for d in helper_dirs if (d / "login.ts").exists()), None)

    if src is None:
        print(
            "  WARNING: helpers/login.ts not found — auth tests may fail.",
            file=sys.stderr,
        )
        return

    dest = TESTS_DIR / "helpers" / "login.ts"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"  Copied {src} -> {dest}")


def run_generation(config: dict) -> None:
    """
    Phase 4: Render Playwright spec files from manifest + templates.

    Imports generator/generate.py dynamically (same search order as discover/manifest).
    Reads:  tests/manifest.json, templates/*.ts.tmpl
    Writes: tests/generated/{view_id}.{test_type}.spec.ts
            tests/helpers/login.ts
    """
    print("\n=== Phase 4 — Test Generation ===")

    manifest_path = TESTS_DIR / "manifest.json"
    if not manifest_path.exists():
        print(
            "  No manifest found — run discovery first (python3 bootstrap.py --refresh).",
            file=sys.stderr,
        )
        return

    try:
        generate = _load_generator_module("generate")
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Find templates directory: _ignition_test/templates/ first, then local templates/
    tmpl_dirs = [LOCAL_TOOLING_DIR / "templates", Path("templates")]
    templates_dir = next((d for d in tmpl_dirs if d.exists()), None)
    if templates_dir is None:
        print("  ERROR: templates/ directory not found.", file=sys.stderr)
        return

    output_dir = TESTS_DIR / "generated"
    generate.generate(manifest_path, templates_dir, output_dir, config)

    # Copy the login helper into tests/helpers/ so auth specs can import it
    _copy_login_helper()

    print("\nTest files written to tests/generated/.")


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
    # 2. .gitignore + .env.test.example
    # ------------------------------------------------------------------
    print("\nUpdating .gitignore...")
    update_gitignore()
    print("\nWriting environment template...")
    write_env_example()

    # ------------------------------------------------------------------
    # 3. Node dependencies + dogfood skill
    # ------------------------------------------------------------------
    print("\nChecking Node.js tooling...")
    install_node_deps()
    install_dogfood_skill()

    # ------------------------------------------------------------------
    # 4. Generate project files
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
    # 7. Discovery (Phase 3)
    # ------------------------------------------------------------------
    manifest_file = TESTS_DIR / "manifest.json"
    if not manifest_file.exists() or args.refresh:
        run_discovery(config)
    else:
        print(f"\nFound existing {manifest_file} — skipping discovery.")
        print("  Pass --refresh to re-run discovery and update the manifest.")

    # ------------------------------------------------------------------
    # 8. Test generation (Phase 4)
    # ------------------------------------------------------------------
    if not args.skip_generate:
        run_generation(config)
    else:
        print("\nSkipping test generation (--skip-generate).")

    # ------------------------------------------------------------------
    # 9. Gateway teardown (Phase 5 stub)
    # ------------------------------------------------------------------
    tear_down_gateway(config)

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    print("\n=== Bootstrap complete ===")
    print("Next steps:")
    print("  1. Copy .env.test.example -> .env.test and fill in real credentials.")
    print("  2. Run ./test-start to execute tests.")
    print("  3. Run: python3 bootstrap.py --refresh to re-run discovery.")
    print("  4. Run: python3 bootstrap.py --refresh --skip-generate to update manifest only.")


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
  Phase 2  Config, dependency install, file generation
  Phase 3  Two-pass discovery, manifest build/validate/write
  Phase 4  Test file generation from templates
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
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        default=False,
        help="(Phase 4) Skip test file generation after discovery.",
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
