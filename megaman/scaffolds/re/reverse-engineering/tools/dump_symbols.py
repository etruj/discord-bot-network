#!/usr/bin/env python3
"""Dump the real LC_SYMTAB from the target binary.

Phase 1 recovered 182 C++ symbols by grepping strings. The binary actually
ships a full symbol table; this reads it properly so we get name -> address
for every function, which is what makes the disassembly tractable.
"""
# Copied into this project by claude-pm from the a prior legacy-iOS decompilation project
# (an earlier decompilation project). The hardcoded target path was replaced by
# $RE_TARGET (default: target/binary); everything else is verbatim, so the
# docstring still describes that binary. Read before trusting a default.

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from macho import MachO  # noqa: E402

N_STAB = 0xE0
N_TYPE = 0x0E
N_EXT = 0x01
TYPE_NAMES = {0x0: "UNDF", 0x2: "ABS", 0xE: "SECT", 0xC: "PBUD", 0xA: "INDR"}


def demangle_itanium(sym):
    """Crude _ZN<len>Name<len>Name...E splitter. Good enough to get
    Class::method out of the mangled C++ names in this binary."""
    s = sym[1:] if sym.startswith("__Z") else sym
    if not s.startswith("_Z"):
        return None
    i = 2
    nested = s[i] == "N"
    if nested:
        i += 1
    parts = []
    while i < len(s) and s[i].isdigit():
        n = 0
        while i < len(s) and s[i].isdigit():
            n = n * 10 + int(s[i])
            i += 1
        parts.append(s[i:i + n])
        i += n
    if not parts:
        return None
    return "::".join(parts)


def main():
    m = MachO(os.environ.get("RE_TARGET", "target/binary"))
    out = Path("analysis/symbols")
    out.mkdir(parents=True, exist_ok=True)

    allsyms, funcs, stabs, undef = [], [], [], []
    for name, ntype, nsect, ndesc, nval in m.symbols():
        if ntype & N_STAB:
            stabs.append((name, ntype, nsect, ndesc, nval))
            continue
        t = ntype & N_TYPE
        ext = bool(ntype & N_EXT)
        allsyms.append((name, t, nsect, nval, ext))
        if t == 0x0:
            undef.append(name)
        elif t == 0xE and nval:
            funcs.append((nval, name, nsect, ext))

    # __text is section index 1 in this binary; keep only code symbols.
    text = m.section("__TEXT", "__text")
    tlo, thi = text[2], text[2] + text[3]
    code = sorted({(a & ~1, n) for a, n, s, e in funcs if tlo <= (a & ~1) < thi})

    with (out / "symtab_full.txt").open("w") as f:
        f.write(f"# {len(allsyms)} non-stab symbols, {len(stabs)} stabs, "
                f"{len(undef)} undefined\n")
        for name, t, nsect, nval, ext in sorted(allsyms, key=lambda x: x[3]):
            f.write(f"{nval:#010x} {TYPE_NAMES.get(t, t):<5} sect={nsect:<3} "
                    f"{'ext' if ext else 'loc'} {name}\n")

    with (out / "code_symbols.txt").open("w") as f:
        f.write(f"# {len(code)} symbols resolving into __TEXT,__text "
                f"({tlo:#x}-{thi:#x})\n")
        for a, n in code:
            f.write(f"{a:#010x} {n}\n")

    # C++ symbols, demangled where we can
    cpp = [(a, n) for a, n in code if n.startswith("__Z")]
    with (out / "cpp_symbols_demangled.txt").open("w") as f:
        f.write(f"# {len(cpp)} mangled C++ code symbols\n")
        for a, n in cpp:
            f.write(f"{a:#010x} {demangle_itanium(n) or '?'}\t{n}\n")

    with (out / "undefined_imports.txt").open("w") as f:
        f.write(f"# {len(set(undef))} undefined (imported) symbols\n")
        for n in sorted(set(undef)):
            f.write(n + "\n")

    print(f"non-stab symbols : {len(allsyms)}")
    print(f"stab (debug)     : {len(stabs)}")
    print(f"undefined imports: {len(set(undef))}")
    print(f"__text code syms : {len(code)}")
    print(f"  mangled C++    : {len(cpp)}")
    print(f"  other (C/ObjC) : {len(code) - len(cpp)}")

    # which C++ classes are present, by symbol count
    classes = Counter()
    for a, n in cpp:
        d = demangle_itanium(n)
        if d and "::" in d:
            classes[d.split("::")[0]] += 1
    print("\ntop C++ classes by symbol count:")
    for c, k in classes.most_common(30):
        print(f"  {k:4d}  {c}")


if __name__ == "__main__":
    main()
