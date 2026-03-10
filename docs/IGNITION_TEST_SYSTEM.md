# Ignition Perspective Test Automation System

## Overview

A self-contained, droppable test automation system for Ignition Perspective projects. A single `bootstrap.py` script is added to any Perspective project repo. It pulls the latest templates and tooling from a central repository, discovers all views, generates a committed test manifest, and stamps out deterministic Playwright tests from a fixed set of templates. A `./test-start` script runs tests locally against a Docker gateway. A GitHub Action wraps the same script for CI.

---

## Goals

- Cover all Ignition Perspective views with basic automated tests (smoke, nav, component errors, auth, screenshots)
- Be droppable into any Perspective project repo with minimal manual setup
- Be maintainable as the app changes — no manually written tests
- Be deterministic and CI-compatible (GitHub Actions, self-hosted `docker-runner`)
- Use PR-scoped test runs — only test views that changed in the PR
- Support both persistent and ephemeral (Docker Compose) gateway environments
- Keep tooling in a central repo so updates propagate to all projects

---

## Repository Structure

### Central Repo (`ignition-test-system`)

The only repo that is ever edited when templates, logic, or tooling changes.

```
ignition-test-system/
├── bootstrap.py              # Entrypoint script dropped into project repos
├── generator/
│   ├── discover.py           # Two-pass discovery (API + browser)
│   ├── manifest.py           # Manifest build, diff, and update logic
│   └── generate.py           # Stamps out Playwright tests from templates
├── templates/
│   ├── smoke.ts.tmpl         # Page loads without error
│   ├── navigation.ts.tmpl    # Nav links resolve correctly
│   ├── components.ts.tmpl    # No broken Perspective components
│   ├── auth.ts.tmpl          # Auth wall — redirect and access behavior
│   └── screenshot.ts.tmpl    # Screenshot baseline per page
├── helpers/
│   ├── login.ts              # Reusable Playwright auth helper
│   ├── gateway.py            # Docker gateway spin-up, health check, teardown
│   └── readiness.py          # Gateway readiness polling
├── actions/
│   └── ignition-tests.yml    # Reusable GitHub Action definition
├── config/
│   └── schema.json           # manifest.json schema definition
└── VERSION                   # Semver string, checked on bootstrap run
```

### Per-Project Repo (after bootstrap)

```
project-repo/
├── bootstrap.py              # The only file manually added — fetches rest from central repo
├── test-start                # Generated shell script — local test runner entrypoint
├── gateway-config.json       # Gateway mode (persistent URL or compose path), project name
├── playwright.config.ts      # Generated Playwright config
├── .env.test.example         # Credential placeholders (never commit real values)
├── tests/
│   ├── manifest.json         # Committed source of truth — view map with metadata
│   ├── generated/            # Auto-generated Playwright tests (committed, auto-updated)
│   └── helpers/              # Static helper files copied from central repo
└── .github/workflows/
    └── ignition-tests.yml    # Generated GitHub Action
```

---

## Discovery System (Two-Pass)

### Pass 1 — API (Structure)

Queries the Ignition Gateway REST API to get the complete view tree for the project.

**Endpoint:**
```
GET http://{gateway}:8088/data/perspective/views?projectName={project}
```

This is the authoritative source — it returns every view that exists regardless of whether it is linked in navigation. The response is a nested tree that is flattened into a list of view paths.

### Pass 2 — Browser (Validation)

Launches the app via `agent-browser` (dogfood skill) and crawls the live app to:
- Confirm which API-discovered views are actually reachable via navigation
- Detect views that are linked in nav but absent from the API (e.g. dynamically injected)
- Identify whether a view requires authentication to reach
- Record the nav path that leads to each view

### Reconciliation

API results and browser results are merged into a single manifest. Each entry is tagged with how it was discovered (`api`, `browser`, `both`) and whether the browser pass could reach it. Discrepancies (views in API but not reachable, or vice versa) are flagged in the manifest as warnings.

---

## Manifest Schema (`manifest.json`)

```json
{
  "version": "1.0",
  "project": "MyIgnitionProject",
  "gateway": "http://gateway:8088",
  "generated_at": "2025-03-10T12:00:00Z",
  "views": [
    {
      "id": "view__home",
      "path": "/home",
      "url": "http://gateway:8088/data/perspective/client/MyIgnitionProject/home",
      "discovered_by": "both",
      "reachable": true,
      "requires_auth": true,
      "nav_path": ["Main Menu", "Home"],
      "tests": {
        "smoke": true,
        "navigation": true,
        "components": true,
        "auth": true,
        "screenshot": true
      }
    }
  ]
}
```

The manifest is the stable input to test generation. CI always tests against the committed manifest, not a freshly generated one. This is what makes runs deterministic.

---

## Test Templates

Each template takes a view entry from the manifest and generates a standalone Playwright test file. Templates are the contract — adding a new template type means every view in every project automatically gets it on the next bootstrap update.

### Template: Smoke (`smoke.ts.tmpl`)
- Navigate to the view URL
- Assert HTTP 200 response
- Assert no JavaScript console errors
- Assert Perspective root component mounts within timeout

### Template: Navigation (`navigation.ts.tmpl`)
- Find all `<a>` anchor elements on the page
- Assert each resolves without a 404
- Assert internal links stay within the same Perspective project

### Template: Components (`components.ts.tmpl`)
- Assert no Perspective error boundary elements are present (`.ia_componentError`, `[class*="error-boundary"]`)
- Assert no components are in a loading-failed state

### Template: Auth (`auth.ts.tmpl`)
- Unauthenticated request: assert redirect to login page
- Authenticated request: assert page loads without redirect
- Post-logout: assert page is no longer accessible

### Template: Screenshot (`screenshot.ts.tmpl`)
- Wait for `networkidle` (with fallback timeout for views with live tag subscriptions)
- Capture full-page screenshot
- Compare against committed baseline with configurable pixel diff threshold
- Support masking of dynamic regions (live data labels, timestamps)

---

## Bootstrap Script (`bootstrap.py`)

The single file that is manually added to a project repo. Everything else is generated.

### Behavior on First Run

1. Check for Python 3.8+ and Node.js
2. Fetch latest version from central repo, compare against local `VERSION` if present
3. Pull latest templates, helpers, and generator scripts from central repo
4. Prompt for or read from environment: gateway URL, project name, credentials
5. Write `gateway-config.json` with mode (`persistent` or `ephemeral`) and config
6. Install npm dependencies (`@playwright/test`, `agent-browser`)
7. Run `npx playwright install chromium`
8. Install dogfood skill: `npx skills add vercel-labs/agent-browser --skill dogfood`
9. If ephemeral mode: validate `docker-compose.test.yml` exists and `.gwbk` is mounted
10. Spin up gateway (if ephemeral), wait for readiness
11. Run two-pass discovery, write `manifest.json`
12. Generate Playwright test files from templates into `tests/generated/`
13. Write `playwright.config.ts`, `.env.test.example`, `test-start`, `.github/workflows/ignition-tests.yml`

### Behavior on Subsequent Runs (Update Check)

1. Fetch `VERSION` from central repo
2. If newer: pull updated templates and helpers, re-generate any tests affected by template changes
3. Re-run discovery if `--refresh` flag is passed, produce a manifest diff, open a PR with changes

---

## Gateway Lifecycle (`gateway.py`)

Handles both gateway modes transparently.

### Persistent Gateway Mode
- Read URL from `gateway-config.json`
- Perform a health check against `GET /data/ignition/ping`
- If unreachable, exit with a clear error — do not proceed

### Ephemeral Gateway Mode
- Run `docker compose -f docker-compose.test.yml up -d`
- Poll `GET /data/ignition/ping` every 5 seconds up to a configurable timeout (default 120s)
- On timeout, print container logs and exit with error
- After test run: `docker compose -f docker-compose.test.yml down`

### `.gwbk` Requirement
Ephemeral gateways must start with the Ignition project pre-loaded. This requires a `.gwbk` gateway backup file to be mounted into the container at startup. Bootstrap validates this is configured — if not, it warns and halts. The `docker-compose.test.yml` is expected to mount the `.gwbk` and configure the gateway to restore it on first boot.

---

## Local Test Runner (`test-start`)

Generated shell script. The single command a developer runs.

```
./test-start                  # Run all tests
./test-start --view /home     # Run tests for a specific view
./test-start --refresh        # Re-run discovery, update manifest
./test-start --update-snapshots  # Regenerate screenshot baselines
```

Internally: checks for updates from central repo, manages gateway lifecycle, invokes Playwright with the appropriate filter.

---

## PR-Scoped Test Runs

### How It Works

1. GitHub Action diffs the PR against `main` to find changed files
2. Changed file paths are matched against the Ignition views directory (e.g. `ignition/views/**`)
3. Matched view names are looked up in `manifest.json` to get their test IDs
4. Playwright is invoked with `--grep` scoped to only those test IDs

### Why the Manifest Is the Link

The manifest maps view file paths to test IDs. This is why it must be committed and kept current — without it, the Action cannot scope the run. When the manifest is updated (views added or removed), a separate PR is opened with the manifest and generated test changes before they affect CI.

---

## GitHub Action (`ignition-tests.yml`)

- Runs on: `self-hosted`, `docker-runner` label
- Triggered by: PRs that touch view files
- Steps:
  1. Checkout repo
  2. Set up Python and Node
  3. Run `bootstrap.py` in update-check mode (pulls latest templates if needed)
  4. Read `gateway-config.json` to determine mode
  5. If ephemeral: spin up Docker Compose gateway, wait for readiness
  6. Diff PR to scope which views changed
  7. Run `./test-start --view {changed_views}`
  8. Upload Playwright HTML report as Action artifact
  9. If ephemeral: tear down Docker Compose gateway
  10. On failure: trigger dogfood scan of affected area (future phase)

---

## Tooling Stack

| Tool | Role |
|------|------|
| Python 3.8+ | Bootstrap, discovery, manifest, generator scripts |
| Node.js + npm | Playwright runtime |
| `@playwright/test` | Deterministic test runner |
| `agent-browser` (Vercel) | Browser-pass discovery and dogfood exploration |
| `dogfood` skill | Exploratory testing, post-failure blast radius scan |
| Docker Compose | Ephemeral gateway lifecycle |
| GitHub Actions | CI orchestration |
| Self-hosted `docker-runner` | Runner with network access to persistent gateways |

---

## Configuration Reference (`gateway-config.json`)

```json
{
  "mode": "ephemeral",
  "project_name": "MyIgnitionProject",
  "gateway_url": "http://localhost:8088",
  "compose_file": "docker-compose.test.yml",
  "readiness_timeout_seconds": 120,
  "views_directory": "ignition/views",
  "auth": {
    "username_env": "IGNITION_TEST_USER",
    "password_env": "IGNITION_TEST_PASSWORD"
  },
  "screenshot": {
    "threshold": 0.2,
    "mask_selectors": []
  },
  "exclude_views": []
}
```

Credentials are always read from environment variables — never committed. `.env.test.example` provides the template.

---

## Phased Build Plan

### Phase 1 — Central Repo Foundation
Build the central repo structure. Establish VERSION, schema, and the update-check mechanism. No project-specific logic yet.

### Phase 2 — Bootstrap Script
Implement `bootstrap.py`. Covers dependency installation, config interrogation, file generation, and the initial end-to-end flow. Goal: `./test-start` works locally by end of this phase.

### Phase 3 — Discovery & Manifest
Implement two-pass discovery. API pass against Ignition views endpoint. Browser pass via `agent-browser`. Reconciliation logic. Manifest write and diff.

### Phase 4 — Test Templates
Implement all five template types. Wire generator to stamp out test files from manifest entries. Validate generated tests run correctly against a live gateway.

### Phase 5 — Docker Orchestration
Implement `gateway.py`. Persistent health check. Ephemeral spin-up, readiness polling, teardown. `.gwbk` validation.

### Phase 6 — GitHub Action
Implement `ignition-tests.yml`. PR diff scoping. Gateway mode branching. Report artifact upload. Wire to `docker-runner`.

---

## Key Design Decisions

**Manifest is the stable contract.** CI always runs against a committed manifest, never a freshly generated one. This ensures determinism — tests don't change between runs unless a manifest PR is merged.

**Templates are the only extension point.** Adding test coverage means adding a template to the central repo. No per-project editing of test files.

**Bootstrap is self-updating.** Every run checks the central repo VERSION and pulls updated templates if newer. Projects stay current without manual intervention.

**Human-gated manifest updates.** When discovery finds new or removed views, changes are proposed as a PR — not auto-merged. This keeps the test contract intentional and reviewable.

**Dogfood is exploratory, not deterministic.** It runs on schedule or post-failure, produces a report, and findings are triaged manually. Recurring findings that represent a class of bug earn a new Playwright template.
