Deterministic build of the player-facing artifact.

The flag is injected here (env var / build arg), never committed into source.
After every build, sweep the artifact (`strings`, `xxd`, `grep -r`) and look
at the output before shipping.
