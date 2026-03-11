# Project Memory — Ignition Test System

## What this repo is
Central repo for the Ignition Perspective test automation system.
Full spec: @docs/IGNITION_TEST_SYSTEM.md

## Phase status
- Phase 1 DONE: directory structure, VERSION (0.1.0), config/schema.json, bootstrap.py update-check
- Phase 2 DONE: full bootstrap flow (config, npm install, dogfood, file generation)
- Phase 3 DONE: two-pass discovery + manifest write (not yet tested against live gateway — pending project repo setup)
- Phase 4 NEXT: test template generation (5 templates → tests/generated/)
- Phases 5–6: not started

## Key files
- [bootstrap.py](bootstrap.py) — all orchestration; Phase 3 live via run_discovery()
- [VERSION](VERSION) — `0.1.0`
- [config/schema.json](config/schema.json) — JSON Schema (draft-07) for manifest.json
- [generator/discover.py](generator/discover.py) — Phase 3 discovery implementation
- [generator/manifest.py](generator/manifest.py) — Phase 3 manifest build/validate/diff/write
- [generator/generate.py](generator/generate.py) — Phase 4 stub
- [templates/](templates/) — five .ts.tmpl stubs for Phase 4
- [helpers/](helpers/) — stubs for Phase 4/5
- [actions/](actions/) — stub for Phase 6

## generator/discover.py — design (filesystem-first)
- `filesystem_pass(views_directory, exclude_views, *, debug)` → list[str]
  - Walk views_directory for view.json files; folder path relative to views_directory = view path
  - No gateway, no network, no auth required; sorted + deduped
- `gateway_pass(gateway_url, project_name, fs_paths)` → (gateway_paths, probe_results, error|None)
  - Non-blocking: returns ([], [], error_str) if gateway unreachable
  - Step 1: _api_fetch() — GET /data/perspective/views?projectName={project}
    - Tries unauthenticated, retries with Basic auth on 401
    - Handles list[str], list[dict], {views:[...]}, tree {name,children}, unknown shape
  - Step 2: HTTP probe each filesystem URL — auth via 401/403, URL signals, HTML scan
  - nav_path always [] — agent-browser future phase
- `reconcile(fs_paths, gateway_paths, probe_results)` → list[dict]
  - Tags: "filesystem" (reachable=None) / "both" (probe values) / "gateway_only" (warning)
  - Filesystem is source of truth
- `run(config, *, debug)` → list[dict] — main entry point

## generator/manifest.py — design
- `path_to_id(path)` → `view__home_detail` format (lowercase, slashes→underscore)
- `build_manifest(config, views)` → manifest dict (auth test flag mirrors requires_auth)
- `validate_manifest(manifest, schema_path)` → list[str] errors
  - Uses jsonschema if installed; falls back to manual checks
  - Strips $id from schema to prevent network fetch attempts
- `diff_manifest(old, new)` → {added, removed, changed, unchanged}
  - Compares significant fields: reachable, requires_auth, discovered_by, tests
- `print_diff(diff)` — human-readable diff output
- `write_manifest_atomic(manifest, dest)` — writes .tmp then rename
- `build_and_write(config, views, dest)` — validate → diff → write

## bootstrap.py — Phase 3 wiring
- `_load_generator_module(name)` — finds generator/ in _ignition_test/ or local generator/
  - Deletes from sys.modules before re-importing (supports hot updates)
- `run_discovery(config)` — imports discover + manifest, calls discover.run() then manifest.build_and_write()
- `generate_test_start()` — --refresh now calls `python3 bootstrap.py --refresh` (was TODO)

## Env var reference
| Var | Purpose |
|-----|---------|
| IGNITION_TEST_CENTRAL_REPO | Raw GitHub URL for central repo (in .env) |
| IGNITION_GATEWAY_URL | Gateway base URL |
| IGNITION_PROJECT_NAME | Ignition project name |
| IGNITION_GATEWAY_MODE | persistent or ephemeral |
| IGNITION_COMPOSE_FILE | Path to docker-compose.test.yml (ephemeral only) |
| IGNITION_VIEWS_DIR | Views directory relative to project root |
| IGNITION_TEST_USER | Test auth username (also used by discover.py) |
| IGNITION_TEST_PASSWORD | Test auth password (also used by discover.py) |

## Phase 4 contract (what generate.py needs from Phase 3)
- Input: `tests/manifest.json` — committed, conforming to config/schema.json
- For each view entry: id, path, url, requires_auth, tests flags
- Templates in `templates/*.ts.tmpl` — one output file per view per enabled test type
- Output: `tests/generated/{view_id}.{test_type}.spec.ts`
- Generator reads manifest, not config — manifest is the stable contract
