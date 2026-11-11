#!/usr/bin/env python3
"""Decode the STABS debug entries in the target binary.

The binary was shipped without stripping debug symbols. STABS carry original
source paths (N_SO), per-function addresses (N_FUN), line-number tables
(N_SLINE) and typed local/parameter/global declarations (N_LSYM/N_PSYM/
N_RSYM/N_GSYM). That is the difference between reading raw ARM and reading
the original program structure.
"""
# Copied into this project by claude-pm from the a prior legacy-iOS decompilation project
# (an earlier decompilation project). The hardcoded target path was replaced by
# $RE_TARGET (default: target/binary); everything else is verbatim, so the
# docstring still describes that binary. Read before trusting a default.

import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from macho import MachO  # noqa: E402

STAB = {
    0x20: "N_GSYM", 0x22: "N_FNAME", 0x24: "N_FUN", 0x26: "N_STSYM",
    0x28: "N_LCSYM", 0x2A: "N_MAIN", 0x2E: "N_BNSYM", 0x30: "N_PC",
    0x32: "N_AST", 0x3A: "N_OPT", 0x40: "N_RSYM", 0x44: "N_SLINE",
    0x46: "N_ENSYM", 0x48: "N_DSLINE", 0x50: "N_BSLINE", 0x52: "N_BROWS",
    0x54: "N_DEFD", 0x5C: "N_EHDECL", 0x60: "N_CATCH", 0x64: "N_SO",
    0x66: "N_OSO", 0x80: "N_LSYM", 0x82: "N_BINCL", 0x84: "N_SOL",
    0x86: "N_PSYM", 0x88: "N_EINCL", 0x8A: "N_ENTRY", 0x8E: "N_EXCL",
    0xA0: "N_LBRAC", 0xA2: "N_SCOPE", 0xA4: "N_EXCL2", 0xC0: "N_RBRAC",
    0xC2: "N_BCOMM", 0xC4: "N_ECOMM", 0xC8: "N_ECOML", 0xE0: "N_LENG",
}


def main():
    m = MachO(os.environ.get("RE_TARGET", "target/binary"))
    out = Path("analysis/symbols")
    out.mkdir(parents=True, exist_ok=True)

    entries = [(n, t, s, d, v) for n, t, s, d, v in m.symbols() if t & 0xE0]
    kinds = Counter(STAB.get(t, hex(t)) for _n, t, _s, _d, _v in entries)

    print(f"total stabs: {len(entries)}\n")
    for k, c in kinds.most_common():
        print(f"  {k:<10} {c}")

    # raw dump
    with (out / "stabs_raw.txt").open("w") as f:
        for n, t, s, d, v in entries:
            f.write(f"{STAB.get(t, hex(t)):<10} val={v:#010x} desc={d:<6} "
                    f"sect={s:<3} {n}\n")

    # source files
    sofiles, cur = [], ""
    for n, t, s, d, v in entries:
        if t == 0x64:  # N_SO
            if n:
                cur = n if n.startswith("/") or not cur else cur + n
                sofiles.append(n)
    srcs = sorted(set(x for x in sofiles if x))
    (out / "source_files.txt").write_text(
        f"# {len(srcs)} distinct N_SO source path components\n" +
        "\n".join(srcs) + "\n")

    # object files
    osos = sorted({n for n, t, _s, _d, _v in entries if t == 0x66 and n})
    (out / "object_files.txt").write_text(
        f"# {len(osos)} N_OSO object files\n" + "\n".join(osos) + "\n")

    # functions with addresses
    funs = [(v, n) for n, t, _s, _d, v in entries if t == 0x24 and n]
    (out / "stab_functions.txt").write_text(
        f"# {len(funs)} N_FUN entries\n" +
        "\n".join(f"{v:#010x} {n}" for v, n in sorted(funs)) + "\n")

    # typed declarations: N_LSYM/N_PSYM/N_GSYM/N_RSYM carry "name:typecode"
    typed = [(STAB.get(t, hex(t)), n, v)
             for n, t, _s, _d, v in entries if t in (0x80, 0x86, 0x20, 0x40)]
    (out / "stab_types.txt").write_text(
        f"# {len(typed)} typed declarations (name:stabs-type)\n" +
        "\n".join(f"{k:<8} {v:#010x} {n}" for k, n, v in typed) + "\n")

    print(f"\nsource paths : {len(srcs)}")
    print(f"object files : {len(osos)}")
    print(f"N_FUN        : {len(funs)}")
    print(f"typed decls  : {len(typed)}")

    if srcs:
        print("\nsample source paths:")
        for s in srcs[:40]:
            print("  " + s)
    if osos:
        print("\nsample object files:")
        for s in osos[:40]:
            print("  " + s)


if __name__ == "__main__":
    main()
