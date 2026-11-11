---
name: ctf-challenge-authoring
description: >-
  Workflow for building a CTF challenge other people will solve — designing the
  intended path, implementing it, writing the reference solver, calibrating
  difficulty, and packaging it for deployment. Use this skill when the project
  is authoring/designing a challenge (not solving one): web, pwn, crypto, re,
  forensics or misc, including hardware/ROM challenges. It covers flag
  hygiene, unintended-solution hunting, hint ladders, and the artifacts an
  event needs from you.
---

# Building a CTF challenge

The job is not "make something hard". It is **to design an experience with one
intended path, prove that path works, and know what else the challenge
accidentally allows.**

## 1. Write the solution first

Before any challenge code exists, write down:

- **The intended path**, step by step, as a solver would discover it. If you
  can't write it, there isn't one yet.
- **The one insight** the challenge is about. A challenge with three unrelated
  insights is three challenges stacked badly.
- **What the solver sees first** — the artifact, the prompt, the hook that
  makes step 1 obvious enough to start.

The reference solver (`solve/solve.py`) is part of the deliverable and must run
end-to-end against the deployed challenge, from a clean environment, in CI if
there is one. A challenge whose solver you can't re-run is unverified.

## 2. Flag hygiene

- One flag, in the event's exact format (`flag{...}` unless told otherwise).
  Check the format with the event before you build around it.
- The flag never appears in the artifact as plaintext when the challenge is
  "find the flag" — `strings`/`xxd`/`grep -r` must not hand it over.
- The flag is a constant in **one** place, injected at build/deploy time
  (env var or build arg), never pasted into several files.
- Unguessable: no `flag{try_harder}` patterns a solver can derive from the
  title.

## 3. Hunt unintended solutions before anyone else does

Spend real time attacking your own challenge the way a player will:

- What does the artifact leak that it didn't mean to? Build paths, symbols,
  debug strings, `.git`, source maps, backup files, container layers.
- Can the flag be reached by skipping the insight — brute force, a race, a
  library bug, an environment variable, a path traversal to the flag file?
- For remote challenges: what else is reachable on that host/container? Can
  one player break the challenge for everyone?
- Write every unintended path you find into `notes.md` with whether you fixed
  it or accepted it. Accepted ones are design decisions, not oversights —
  record *why*.

## 4. Difficulty calibration

Rate it by the **number of insights**, not by how long it took you to build:

| tier | shape |
|---|---|
| easy | one standard technique, applied directly; nothing to discover |
| medium | one real insight + careful execution |
| hard | two chained insights, or one insight plus a demanding implementation |
| brutal | research-grade; expect single-digit solves — say so up front |

Write a **hint ladder** (3 hints: nudge → direction → near-spoiler). Hints are
part of the design, not an apology for it.

## 5. Build, deploy, hand off

- `build/` produces the player-facing artifact **deterministically** from
  source. Ship the smallest artifact that still contains the puzzle.
- Strip what gives it away: symbols, debug info, build paths, comments,
  history. Then re-run your own `strings`/`grep` sweep and look at the output.
- For hosted challenges: a container that starts clean, runs unprivileged,
  resets per connection where it matters, and has resource limits.
- Hand-off package: artifact, `README` (name, category, difficulty, author),
  flag, reference solver, hint ladder, deploy instructions, known unintended
  paths.

## 6. Game Boy / ROM challenges

A Game Boy challenge ROM is a GBDK project — pick "Game development" with
`ctf` in the answer, and it gets the Game Boy skill (plus your own GBC-CTF
playbook, if you keep one in `scaffolds/gbdk/gbc-ctf/`). The rules above
still apply: the flag must survive a `strings`/`xxd` sweep of the ROM.

## 7. Playtest with someone who doesn't know the answer

The last gate before shipping. You cannot measure the difficulty of a puzzle
whose solution you already know. If no playtester is available, say that in
the hand-off instead of guessing a tier.
