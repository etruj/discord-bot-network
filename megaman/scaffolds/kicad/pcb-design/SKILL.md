---
name: pcb-design
description: >-
  Rules and workflow for PCB design with KiCad 9: schematic capture, ERC,
  layout, DRC, and headless validation/rendering via kicad-cli. Use whenever
  the work involves PCBs, schematics, footprints, gerbers, or circuit design.
  The validation-gate and fab-rules here prevent boards that look right but
  fail fabrication or don't work electrically.
---

# PCB Design (KiCad 9)

## Golden rules

1. **ERC + DRC clean is the test gate.** Run `./scripts/pcb_check.sh` after
   every meaningful change — it runs ERC on the schematic, DRC on the board,
   and renders a PNG. Never report progress with open ERC/DRC violations;
   fix or explicitly waive each one (waivers documented in LOG.md).
2. **Schematic before layout, always.** Finish and ERC-clean the schematic,
   assign every footprint, THEN start layout. Editing the schematic mid-layout
   means re-importing the netlist (`Update PCB from Schematic`) — doable but
   every round trip risks orphaned traces.
3. **KiCad files are plain-text s-expressions** (`.kicad_sch`, `.kicad_pcb`).
   You can read them to inspect state and make small edits directly, but for
   generation prefer the tools: schematic work in the s-expr format follows
   consistent patterns (copy an existing symbol stanza), board scripting goes
   through Python (`import pcbnew`). Always run the check script after a
   direct file edit — a malformed s-expr fails loudly, a wrong one silently.
4. **Design against the fab's real rules from day one** (defaults below are
   JLCPCB 2-layer — adjust if CLAUDE.md names a different fab). Set them in
   Board Setup → Design Rules so DRC enforces them; don't design at tighter
   tolerances than the fab can hold.
5. **Every IC power pin gets a 100 nF decoupling cap** placed <2 mm away in
   layout, plus one bulk cap (≥10 µF) per rail per board region. This is the
   #1 "compiles but doesn't work" class of PCB bug.
6. **Ground pour on both layers, stitched.** Copper pour connected to GND on
   front and back, stitching vias every ~10 mm and around connectors/ICs.
   Never route signals that break the pour into long slivers.
7. **Version stamp the silkscreen** — board name + `vN` + date on the
   silkscreen every revision, same spirit as the OpenSCAD `v#` stamp.

## Fab defaults (JLCPCB 2-layer, 1.6 mm FR-4)

| Rule | Minimum | Use instead |
|---|---|---|
| Trace width (signal) | 0.127 mm | **0.25 mm** |
| Trace width (power) | — | **0.5–1.0 mm** (1 A ≈ 0.3 mm on outer layer, derate ×2) |
| Clearance | 0.127 mm | **0.2 mm** |
| Via | 0.45/0.3 mm | **0.6/0.3 mm** |
| Trace to board edge | 0.2 mm | **0.3 mm** |
| Silkscreen text height | 0.8 mm | **1.0 mm+** |

Board outline on `Edge.Cuts`, closed contour, corners radiused ≥1 mm.
Mounting holes: M3 (3.2 mm hole, 6 mm keepout) unless the enclosure says
otherwise — coordinate with any OpenSCAD enclosure project.

## Workflow

1. **Parts + datasheets first.** List every component with exact MPN,
   package, and one-line role in LOG.md before drawing anything. Check
   JLCPCB/LCSC stock for assembly builds.
2. **Schematic** (`.kicad_sch`): hierarchical sheets only past ~1 page.
   Net labels over long wires. Power symbols (+3V3, GND) not drawn wires.
   Every net that leaves the board gets a testpoint or header access.
3. **ERC clean** — `kicad-cli sch erc` via the check script. Common fixes:
   PWR_FLAG on power inputs, explicit no-connect flags on unused pins.
4. **Footprints**: assign in the schematic. Verify any hand-made footprint
   against the datasheet land pattern at 1:1 scale before layout.
5. **Layout**: placement first (connectors on edges, decoupling at pins,
   crystal <5 mm from IC with guard ring), then route: power/critical nets
   by hand first, signals after; 45° corners; no acid traps.
6. **DRC clean**, then **render and inspect**: the check script produces
   `build/board.png` — the bot's `/render <project>` posts it to Discord.
7. **Gerbers**: `kicad-cli pcb export gerbers` + `drill` into `build/gerbers/`,
   zip that folder for upload. Re-run DRC immediately before every gerber
   export — gerbers from a dirty board are the expensive mistake.

## Headless commands (kicad-cli 9)

```bash
kicad-cli sch erc --severity-error --exit-code-violations -o build/erc.rpt X.kicad_sch
kicad-cli pcb drc --severity-error --exit-code-violations -o build/drc.rpt X.kicad_pcb
kicad-cli pcb render -o build/board.png X.kicad_pcb          # raytraced top view
kicad-cli pcb render --side bottom -o build/board-bottom.png X.kicad_pcb
kicad-cli sch export pdf -o build/schematic.pdf X.kicad_sch  # for review
kicad-cli pcb export gerbers -o build/gerbers/ X.kicad_pcb
kicad-cli pcb export drill  -o build/gerbers/ X.kicad_pcb
```

## Scripting

- **Board manipulation:** `python3 -c "import pcbnew; b = pcbnew.LoadBoard('X.kicad_pcb'); ..."`
  — move footprints, add zones, query nets. Save with `pcbnew.SaveBoard`.
- **Code-first netlists:** `skidl` (installed) defines circuits in Python and
  exports a KiCad netlist — useful for repetitive/parametric circuits
  (resistor ladders, LED matrices). The schematic still needs manual layout
  in KiCad afterward for human readability.

## Common traps

- Footprint pin 1 ≠ symbol pin 1 (check twice on hand-made parts).
- Connector footprints mirrored on the bottom side (USB, headers).
- Antenna/crystal areas need pour keepouts, not more copper.
- Silkscreen over pads gets clipped at fab — DRC won't always flag it.
- A net named `GND` and a net named `GNDD` both exist and aren't connected —
  check the ERC "net has no driver" style warnings carefully.
