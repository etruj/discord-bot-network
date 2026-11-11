# tools/

`setup.sh` is this project's root script: put anything needing root in it
(installing a provider, mounting an image, attaching a debugger), keep it
idempotent, and get it approved once with
`/sudo action:approve project:<name> script:tools/setup.sh`. Editing it
cancels the approval.

The rest came from the an earlier decompilation project. They are small, dependency-free parsers for **32-bit Mach-O**
(iOS armv6/armv7) that REA doesn't replace — REA gives you inventory and
decompilation; these give you the exact facts a legacy iOS binary hides in its
symbol table.

| file | what it gives you |
|---|---|
| `macho.py` | load commands, segments/sections, VA ↔ file offset. Import `MachO` from the others. |
| `dump_symbols.py` | the real `LC_SYMTAB`: name → address for every function |
| `dump_stabs.py` | STABS debug records: original source files, per-function addresses, line tables |
| `build_function_map.py` | the original source-tree layout reconstructed from STABS |
| `objc_meta.py` | Objective-C 2.0 classes, ivars (with offsets), method lists |
| `objc_types.py` | runtime type encodings → C structs, verified against the compiler's own ivar offsets |
| `export_ghidra_symbols.py` | symbols **with ARM/Thumb mode per function** — seed this before auto-analysis |

Point them at the artifact with `RE_TARGET`:

```bash
export RE_TARGET=target/<the binary>
python3 tools/dump_stabs.py
python3 tools/export_ghidra_symbols.py
```

They are otherwise **verbatim**: docstrings, output paths (`analysis/symbols`,
`analysis/objc`) and some assumptions still describe that project's binary.
Read the file before trusting a default, and fix what doesn't fit here rather
than working around it.
