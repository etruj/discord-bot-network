# Rules: garden-monitor

## Change Log — HARD RULE, never skip
- `LOG.md` in the project root is the change journal. **After completing every
  prompt that changes anything, append an entry before finishing your reply.**
- Newest entries at the top. Each entry has:
  - Timestamp (YYYY-MM-DD HH:MM local) and a short title
  - A high-level summary of what changed and why (the intent, not a diff)
  - The list of files created/edited/deleted
- This is a more verbose git history: commits capture the *what*, LOG.md
  captures the *why* and the session-to-session context. Read LOG.md first
  when starting a session to get oriented.
- Prompts that change nothing (questions, discussion) need no entry.

## Sandbox
- Strictly confined to `/tmp/mm-projects-esfy8o1g/garden-monitor`
- No access to parent directories or sibling projects
- No network calls unless explicitly instructed

## Git
- Never commit directly to `main` — always use feature branches
- Commit messages follow: `type: short description` (e.g. `feat: add login endpoint`)
- Keep commits small and focused on one change

## Code Quality
- Follow PEP 8
- Use type hints on all function signatures
- Docstrings on public functions

## Testing
- Use `pytest` for all tests
- Tests live in `tests/`
- Cover all new code

## Project-Specific Constraints
- -
