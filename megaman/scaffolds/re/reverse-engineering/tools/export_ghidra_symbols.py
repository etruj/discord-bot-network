#!/usr/bin/env python3
"""Export the target binary symbols for Ghidra, with ARM/Thumb mode per function.

The binary is 1388 Thumb functions to 7 ARM. Ghidra will not infer that
reliably on its own, and getting it wrong means the decompiler reads Thumb
halfwords as ARM words and emits convincing nonsense. The Thumb flag lives in
the symbol's n_desc (N_ARM_THUMB_DEF = 0x0008), NOT in the address low bit —
n_value is already clean here, unlike the Objective-C method-list IMP fields.

Output: analysis/symbols/ghidra_symbols.tsv
    address <TAB> thumb(0|1) <TAB> source_file <TAB> name
"""
# Copied into this project by claude-pm from the a prior legacy-iOS decompilation project
# (an earlier decompilation project). The hardcoded target path was replaced by
# $RE_TARGET (default: target/binary); everything else is verbatim, so the
# docstring still describes that binary. Read before trusting a default.

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from macho import MachO  # noqa: E402

N_ARM_THUMB_DEF = 0x0008
# original build machine's source root in STABS (/Users/<developer>/.../BUILD_ver<NNN>.../)
BUILD_ROOT = re.compile(r"^/Users/[^/]+/(?:[^/]+/)*?BUILD_ver[^/]*/")
BIN = os.environ.get("RE_TARGET", "target/binary")


def main():
    m = MachO(BIN)
    text = m.section("__TEXT", "__text")
    tlo, thi = text[2], text[2] + text[3]

    # file attribution from STABS (N_SO groups the N_FUN entries that follow)
    file_of, cur = {}, None
    for name, ntype, _s, _d, nval in m.symbols():
        if not (ntype & 0xE0):
            continue
        if ntype == 0x64:                      # N_SO
            cur = name if (name and not name.endswith("/")) else None
        elif ntype == 0x24 and name and cur:   # N_FUN
            file_of.setdefault(nval & ~1, BUILD_ROOT.sub("", cur))

    rows, seen = [], set()
    for name, ntype, _ns, ndesc, nval in m.symbols():
        if ntype & 0xE0 or (ntype & 0x0E) != 0x0E or not nval:
            continue
        addr = nval & ~1
        if not (tlo <= addr < thi) or addr in seen:
            continue
        seen.add(addr)
        thumb = 1 if (ndesc & N_ARM_THUMB_DEF or nval & 1) else 0
        rows.append((addr, thumb, file_of.get(addr, ""), name))

    rows.sort()
    out = Path("analysis/symbols/ghidra_symbols.tsv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write("# address\tthumb\tsource_file\tname\n")
        for a, t, src, n in rows:
            f.write(f"{a:#x}\t{t}\t{src}\t{n}\n")

    nt = sum(r[1] for r in rows)
    print(f"wrote {out}: {len(rows)} symbols ({nt} thumb, {len(rows)-nt} arm)")
    print(f"with source attribution: {sum(1 for r in rows if r[2])}")
    arm = [r for r in rows if not r[1]]
    if arm:
        print("\nARM (non-Thumb) functions:")
        for a, t, src, n in arm:
            print(f"  {a:#010x}  {n}  [{src or '?'}]")


if __name__ == "__main__":
    main()
