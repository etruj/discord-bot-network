#!/usr/bin/env bash
# Optional installs for Game Boy dev — the only bits that need sudo.
# Everything required is already installed (gbdk-2020 in ~/gbdk, PyBoy via pip).
#
# SameBoy: the most accurate GB/GBC emulator, packaged in Fedora. Use it to
# play-test ROMs on the laptop with real hardware timing. Not needed for the
# bot's /screenshot command (that uses PyBoy headlessly).
set -e
sudo dnf install -y sameboy
echo "Done. Run ROMs with: sameboy path/to/game.gb"
