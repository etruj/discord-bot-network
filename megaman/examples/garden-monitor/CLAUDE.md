# Project: garden-monitor

> A small dashboard that shows soil moisture from my garden sensors

## Stack
- Language: Python
- Framework: FastAPI

## Project Folder
This session is strictly confined to: `/tmp/mm-projects-esfy8o1g/garden-monitor`
Do not read, write, or reference any files outside this directory.
Do not navigate to parent folders or sibling projects.

## Quick Start
- Run: `uvicorn main:app --reload`
- Test: `pytest`

## Goals
- Read the sensor JSON, chart 24h of moisture, no login

## Change Log (hard rule)
Read `LOG.md` first when starting a session — it is the change journal and
continuity mechanism. After every prompt that changes anything, append an entry
(timestamp, high-level summary, files edited). Full rule in `RULES.md`.

## Scripts & root access
Project scripts live in `tools/`. Anything that needs root goes in
`tools/setup.sh` — short, idempotent, commented, root-only work — and the owner
approves it once from Discord:
`/sudo action:approve project:garden-monitor script:tools/setup.sh`
Sessions have no general sudo; editing an approved script cancels its approval.

## References
- See `LOG.md` for the change journal (read this first)
- See `RULES.md` for constraints and coding standards
- See `PLAN.md` for the phased development plan
- See `SKILLS.md` for reusable prompt patterns
- See `LAUNCH.md` for launch.sh rules (if present)

## Notes
**Hardware target:** fastapi
