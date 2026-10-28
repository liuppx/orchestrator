# Repository Layout

This repository currently mixes three kinds of content: active product code,
supporting assets, and historical/reference material. The goal of this file is
to make those boundaries explicit.

## Active product path

These directories define the current Orchestrator runtime and managed agent set and should be treated as
the main delivery path:

- `backend/`: Python control-plane service and API implementation.
- `web/`: React control-plane UI source and build configuration.
- `agents/openclaw/`: current OpenClaw-based managed agent implementations.
- `agents/nanobot/`: current nanobot code kept under the unified agent tree.
- `scripts/`: bootstrap, run, stop, package, and verification entrypoints for
  the Orchestrator stack.
- `backend/.env.template`: the single Orchestrator service configuration template; copy it to `backend/.env`. Agent-specific templates remain within their owning agent directory.
- `docs/`: current product and runbook documentation.

## Active support directories

These directories are still useful to the current repository, but they are not
the primary production entrypoint:

- `runtime/`: ignored local control-plane state, logs, and managed instance data.
- `data/`: sample or domain data used by templates and experiments.
- `ops/`: operational helper scripts and sidecar workflows.
- `examples/`: example prompt or scenario material.

## Historical reference material

- `docs/archive/`: archived documentation that remains useful as background, but
  should not be treated as the current source of truth.

## Practical rules

1. New production code should default to the active product path, not to a new
   top-level directory.
2. New agent implementations should go under `agents/`.
3. New docs should go under `docs/`, and legacy docs should be moved to
   `docs/archive/` once they are no longer current.
4. If a directory exists mainly for migration support, document that status in
   its local `README.md`.

## Orchestrator web structure

The Orchestrator frontend is currently shipped as one SPA, but its source is now
organized for a future split into agent-specific sites:

- `web/src/shell/`: Orchestrator shell, app router, and shared navigation.
- `web/src/platform/`: cross-app platform concerns such as auth,
  UCAN-adjacent session wiring, API clients, shared types, and query setup.
- `web/src/apps/`: agent-facing application modules. Each agent app
  should keep its own screens and business UI here.
- `web/dist/`: built web UI artifacts served by the Python backend.
  This directory is runtime/package output and should not be committed as source.

Current rule of thumb:

1. Add shared control-plane concerns to `platform/`.
2. Add global layout and routing concerns to `shell/`.
3. Add agent-specific UI to `apps/<agent>/`.
4. Keep new agent apps separable so they can later evolve into independent
   sites without rewriting platform and shell concerns first.
