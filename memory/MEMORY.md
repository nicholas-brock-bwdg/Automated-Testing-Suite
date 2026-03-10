# Project Memory — Ignition Test System

## What this repo is
Central repo for the Ignition Perspective test automation system.
Full spec: @docs/IGNITION_TEST_SYSTEM.md

## Phase status
- Phase 1 DONE: directory structure, VERSION (0.1.0), config/schema.json, bootstrap.py update-check
- Phase 2 DONE: full bootstrap flow (config, npm install, dogfood, file generation, Phase 3/5 stubs)
- Phase 3 NEXT: two-pass discovery (API + browser), manifest.json generation
- Phases 4–6: not started

## Key files
- [bootstrap.py](bootstrap.py) — all Phase 1+2 logic; Phase 3/5 stubs with signature contracts
- [VERSION](VERSION) — `0.1.0`
- [config/schema.json](config/schema.json) — JSON Schema (draft-07) for manifest.json
- [generator/](generator/) — stubs for Phase 3/4
- [templates/](templates/) — five .ts.tmpl stubs for Phase 4
- [helpers/](helpers/) — stubs for Phase 4/5
- [actions/](actions/) — stub for Phase 6

## bootstrap.py — Phase 2 design
- `interrogate_config()` — env-var-first, falls back to interactive prompts
  - Env vars: IGNITION_GATEWAY_URL, IGNITION_PROJECT_NAME, IGNITION_VIEWS_DIR,
    IGNITION_GATEWAY_MODE, IGNITION_COMPOSE_FILE
- `load_existing_config()` — reads gateway-config.json; skips prompts if found
- `--reconfigure` flag forces re-prompting even if gateway-config.json exists
- `write_gateway_config(config)` — writes gateway-config.json (spec schema)
- `write_env_example()` — writes .env.test.example with all env var placeholders
- `install_node_deps()` — writes package.json if absent, npm install, playwright install chromium
- `install_dogfood_skill()` — npx skills add vercel-labs/agent-browser --skill dogfood
- `validate_ephemeral(config)` — checks compose file exists; warns (does not halt) if no .gwbk found
- `generate_playwright_config(config)` — writes playwright.config.ts
- `generate_test_start(config)` — writes test-start (chmod 755); handles --view, --refresh, --update-snapshots
- `_generate_github_action_stub()` — writes .github/workflows/ignition-tests.yml Phase 6 stub
- `spin_up_gateway(config)` — Phase 5 stub with full signature contract in docstring
- `tear_down_gateway(config)` — Phase 5 stub
- `run_discovery(config)` — Phase 3 stub with full signature contract in docstring
- `bootstrap(args)` — orchestrates all 8 steps; skips discovery if manifest.json exists
- `main()` — always runs check_for_updates() first, then bootstrap()

## Phase 3 stub contract (from bootstrap.py docstring)
- Signature: `run_discovery(config: dict) -> None`
- Reads: config["gateway_url"], config["project_name"], config["views_directory"], config["exclude_views"]
- Writes: tests/manifest.json conforming to config/schema.json
- Raises: RuntimeError on unrecoverable failure

## Phase 5 stub contract (from bootstrap.py docstring)
- `spin_up_gateway(config)` — persistent: ping /data/ignition/ping; ephemeral: compose up + poll
- `tear_down_gateway(config)` — ephemeral only: compose down
- Both read: config["mode"], config["gateway_url"], config.get("compose_file"), config["readiness_timeout_seconds"]

## Env var reference
| Var | Purpose |
|-----|---------|
| IGNITION_TEST_CENTRAL_REPO | Raw GitHub URL for central repo (in .env) |
| IGNITION_GATEWAY_URL | Gateway base URL |
| IGNITION_PROJECT_NAME | Ignition project name |
| IGNITION_GATEWAY_MODE | persistent or ephemeral |
| IGNITION_COMPOSE_FILE | Path to docker-compose.test.yml (ephemeral only) |
| IGNITION_VIEWS_DIR | Views directory relative to project root |
| IGNITION_TEST_USER | Test auth username |
| IGNITION_TEST_PASSWORD | Test auth password |
