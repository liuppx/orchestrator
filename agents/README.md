# Agents

This directory contains agent implementations managed by the repository.

Current contents:

- `openclaw/`: OpenClaw-based agent subsystems currently managed by the control
  plane.
- `nanobot/`: nanobot implementation grouped under the same agent tree.

Rule of thumb:

- New managed agent implementations should be added under `agents/`.
- Control-plane code should stay outside this directory.
