# Scaffolds

Each folder is a **skill**: a `SKILL.md` playbook plus the scripts and reference files it needs. When `/new` creates a project of the matching type, MegaMan copies the skill into the project so the Claude Code session starts with it.

For what each skill contains, and the files every project gets regardless of type, see **[Project skills and rules](../docs/PROJECT_SKILLS.md)**.

| Folder | Project type | What it carries |
|---|---|---|
| `3d-modeling/printable-parametric-parts` | OpenSCAD | Parametric-part workflow, render and feature-map scripts (the labelled drawing from [Protoman](../../protoman/README.md)) |
| `gbdk/gameboy-dev` | Game Boy | GBDK build, ROM test and screenshot workflow |
| `kicad/pcb-design` | KiCad | ERC/DRC loop and board rendering |
| `re/reverse-engineering` | Reverse engineering | Evidence discipline, triage→static→dynamic workflow, small Mach-O/Ghidra helpers |
| `ctf/challenge-authoring` | CTF authoring | Challenge layout, solve-script and flag-hygiene rules |

To add your own: create `scaffolds/<group>/<name>/SKILL.md`, then register it in the `_ASSETS`-based map in `claude_pm.py` next to the others. One slot is already wired: a Game Boy CTF ROM uses `scaffolds/gbdk/gbc-ctf/` if you create it (none ships here), and the general `gbdk/gameboy-dev` skill otherwise.

Some tools these skills call (KiCad, GBDK, Ghidra, OpenSCAD) are installed separately; see the `install-*.sh` scripts in the parent folder. Reverse engineering: only analyse software you own or are authorised to analyse.
