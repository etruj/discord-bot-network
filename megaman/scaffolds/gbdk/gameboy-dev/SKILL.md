---
name: gameboy-dev
description: >-
  Rules and workflow for Game Boy development with GBDK-2020: C code compiled
  with lcc to .gb ROMs. Use whenever the work involves GBDK, Game Boy, .gb
  ROMs, sprites/tiles/VRAM, banking, or emulator testing. The hardware-budget
  and asset-pipeline rules here prevent code that compiles but glitches or
  crashes on hardware.
---

# Game Boy Development (GBDK-2020)

## Golden rules

1. **`make` must always produce a working ROM.** The build gate is the test
   gate. Never leave the tree in a state where `make` fails or the ROM crashes
   at boot. Run `make` after every change; `make size` after adding assets.
2. **Budget the hardware, in writing.** DMG limits are hard walls:
   - 40 sprites total, **10 per scanline** (11th+ silently vanish)
   - 8 KB VRAM: 256 tiles background + 256 shared window/sprite region
   - 8 KB WRAM total (globals + stack), 127 bytes HRAM
   - 32 KB ROM without banking
   When a feature spends against a budget, note it in a comment at the spend
   site (`// sprites: 12/40 used`).
3. **No floats, no malloc.** Fixed point 8.8 (`int16_t`, high byte = whole
   part) for smooth movement. All memory static.
4. **Graphics go through `png2asset`.** Source PNGs live in `assets/`,
   converted at build time — never hand-write tile bytes except for tiny
   placeholder tiles. Keep the PNG the source of truth.
5. **Everything hangs off `vsync()`.** One game-loop iteration per frame.
   Never busy-wait on timers; never touch VRAM outside vblank unless the
   screen is off (`DISPLAY_OFF`).
6. **Bank before you're forced to.** The moment ROM crosses ~24 KB, plan the
   MBC layout (MBC5 default: `-Wl-yt0x1B`) and mark data with
   `BANKREF`/`SWITCH_ROM` — retrofitting banking late is misery.
7. **Test on DMG rules even for Color.** 4-shade palette, DMG timing. If it
   plays well monochrome, CGB palettes are free polish.

## Workflow

- Build: `make` → `build/game.gb`. Size/bank report: `make size`.
- Run in an emulator with an accuracy reputation (SameBoy > mGBA > others);
  the bot's `/screenshot` command can capture a frame remotely once an
  emulator is installed.
- Map file (`build/*.map`) is the truth for what's eating ROM/WRAM.

## Common patterns

- **Input:** read `joypad()` once per frame into a variable; compare against
  the previous frame's value for press/release edges.
- **Random:** seed `initrand()` with DIV register on first button press
  (`initrand(DIV_REG)`), never a constant.
- **Sprite 0 trick, window HUD, scanline effects:** use `STAT`/`LYC`
  interrupts via `add_LCD()`; keep ISRs tiny (HRAM budget).
- **Music/SFX:** hUGEDriver integrates cleanly with GBDK-2020; call its tick
  from the vblank handler.
