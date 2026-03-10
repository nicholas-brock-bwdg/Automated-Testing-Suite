## Project
Central repo for the Ignition Perspective test automation system.
See IGNITION_TEST_SYSTEM.md for full technical spec — reference it with @IGNITION_TEST_SYSTEM.md.

## Stack
- Python 3.8+ (bootstrap, discovery, manifest, generator)
- Node.js / TypeScript (Playwright tests)
- agent-browser / dogfood skill (browser discovery)
- Docker Compose (gateway lifecycle)

## Key Constraints
- Never write tests manually — all tests are generated from templates
- manifest.json is always the input to generation, never bypassed
- Credentials always via env vars, never hardcoded
- bootstrap.py must work standalone — it is the only file dropped into project repos

## Commands
- Run tests locally: ./test-start
- Refresh manifest: ./test-start --refresh
- Update snapshots: ./test-start --update-snapshots
```

### 2. Use a `docs/` Folder

Put the markdown spec file (`IGNITION_TEST_SYSTEM.md`) in a `docs/` folder in the central repo. Reference it in conversations with `@docs/IGNITION_TEST_SYSTEM.md` rather than in CLAUDE.md directly — this saves context tokens and lets Claude load it on demand.

### 3. Session Strategy

Build one phase at a time and use `/clear` between phases. Don't try to build Phases 1-6 in a single session. Each phase is a natural `/clear` boundary.

### 4. Start Your First Session With
```
@docs/IGNITION_TEST_SYSTEM.md

Let's start with Phase 1 — set up the central repo structure, VERSION file, 
config schema, and the update-check mechanism in bootstrap.py. 
Don't implement any other phases yet.