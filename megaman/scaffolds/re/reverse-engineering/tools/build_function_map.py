#!/usr/bin/env python3
"""Reconstruct the original source-tree layout from STABS.

the target binary shipped unstripped, in every version checked. N_SO entries give the
original .mm/.m paths and N_FUN entries give every function's name and
address, grouped by the file it was compiled from. The output is effectively
the original project skeleton, which is what we mirror on the Android side.

Default binary is work/extracted (v2.3.0, the confirmed-runnable port target
as of 2026-09-28) -> analysis/symbols/function_map.csv, which is what
tools/mgst_probe.py and the Ghidra project both use unless told otherwise.

    build_function_map.py                                   # v2.3.0 (default)
    build_function_map.py --exe <the target binary> --out <name.csv>    # any other version

Function ADDRESSES differ per version (segment layout, code size all differ -
see LOG 2026-09-27 AUDIT finding A), so a v2.1.0 or v1.0.0 map is NOT a
drop-in replacement for function_map.csv; it is a separate file for probing
that version's own NAND, e.g.:

    build_function_map.py --exe oracle/state/ipa-extract/Payload/the target binary.app/the target binary \\
        --out analysis/symbols/function_map_v210.csv
"""
# Copied into this project by claude-pm from the a prior legacy-iOS decompilation project
# (an earlier decompilation project). The hardcoded target path was replaced by
# $RE_TARGET (default: target/binary); everything else is verbatim, so the
# docstring still describes that binary. Read before trusting a default.

import re
import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from macho import MachO  # noqa: E402

# The original build machine's absolute source root, as recorded in STABS
# (/Users/<developer>/.../BUILD_ver<NNN>.../). Matched by pattern so no
# developer's name is written into this tool.
BUILD_ROOT = re.compile(r"^/Users/[^/]+/(?:[^/]+/)*?BUILD_ver[^/]*/")
DEFAULT_BIN = os.environ.get("RE_TARGET", "target/binary")


def build(binpath=DEFAULT_BIN):
    m = MachO(binpath)
    ents = [(n, t, v) for n, t, _s, _d, v in m.symbols() if t & 0xE0]
    per, cur = defaultdict(list), None
    for n, t, v in ents:
        if t == 0x64:               # N_SO
            cur = n if (n and not n.endswith("/")) else None
        elif t == 0x24 and n and cur:   # N_FUN
            per[cur].append((v, n.split(":")[0]))
    return m, per


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", default=DEFAULT_BIN,
                    help="the target binary binary to read (default: %s, v2.3.0)" % DEFAULT_BIN)
    ap.add_argument("--out", default="analysis/symbols/function_map.csv",
                    help="output CSV path (default: analysis/symbols/function_map.csv)")
    ap.add_argument("--no-md", action="store_true",
                    help="skip source_tree_map.md (only meaningful for the default "
                         "v2.3.0 output; a per-version CSV doesn't need its own copy)")
    a = ap.parse_args()

    m, per = build(a.exe)
    out_csv = Path(a.out)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    total = sum(len(v) for v in per.values())

    # Each version's STABS carry ITS OWN build machine's absolute path
    # (/Users/<developer>/.../BUILD_ver<NNN>.../). Strip whichever root this
    # binary actually has.
    roots = {BUILD_ROOT.match(f).group(0) for f in per if BUILD_ROOT.match(f)}
    if len(roots) == 1:
        prefix = roots.pop()
    elif per:
        prefix = os.path.commonprefix(list(per.keys()))
        if not prefix.endswith("/"):
            prefix = prefix.rsplit("/", 1)[0] + "/"
    else:
        prefix = ""

    if not a.no_md:
        lines = [
            "# Original Source Tree (recovered from STABS)",
            "",
            f"Read from `{a.exe}`. The executable is **unstripped**.",
            f"`{prefix}` is the original build root. Every function below is",
            "named and addressed in the shipped binary, grouped by the source",
            "file it was compiled from.",
            "",
            f"- source files: **{len(per)}**",
            f"- named functions: **{total}**",
            "",
        ]
        for f in sorted(per, key=lambda k: k.replace(prefix, "")):
            rel = f.replace(prefix, "")
            lines += [f"## `{rel}` — {len(per[f])} functions", ""]
            for v, n in sorted(set(per[f])):
                lines.append(f"- `{v:#010x}`  `{n}`")
            lines.append("")
        md_path = out_csv.parent / "source_tree_map.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")

    # flat CSV for tooling
    with out_csv.open("w", encoding="utf-8") as fh:
        fh.write("address,source_file,function\n")
        for f in sorted(per):
            for v, n in sorted(set(per[f])):
                fh.write(f'{v:#010x},"{f.replace(prefix, "")}","{n}"\n')

    print(f"wrote {out_csv}" + ("" if a.no_md else " and source_tree_map.md")
          + f" ({len(per)} files, {total} functions) from {a.exe}")


if __name__ == "__main__":
    main()
