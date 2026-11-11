#!/usr/bin/env bash
# KiCad for the PCB-design wizard stack — the only sudo step.
# Installs KiCad 9 (includes kicad-cli, symbol + footprint libraries).
# Deliberately skips kicad-packages3d (~5 GB of 3D viewer models — not needed
# for schematic/PCB work or headless renders; install later if you want the
# 3D viewer to show component bodies).
set -e
sudo dnf install -y kicad
echo
kicad-cli version
echo "Done — /render and PCB project builds are now live."
