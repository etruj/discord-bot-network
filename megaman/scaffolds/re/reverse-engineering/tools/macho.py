#!/usr/bin/env python3
"""Minimal 32-bit Mach-O reader for the the target binary armv6 executable.

Written because lief/macholib are not installed and the phase-1 script
hardcoded its segment table instead of parsing it. Everything here is
derived from the file itself so the VA->file-offset mapping is trustworthy.
"""
# Copied into this project by claude-pm from the a prior legacy-iOS decompilation project
# (an earlier decompilation project). The hardcoded target path was replaced by
# $RE_TARGET (default: target/binary); everything else is verbatim, so the
# docstring still describes that binary. Read before trusting a default.

import os
import struct
import sys
from pathlib import Path

LC_SEGMENT = 0x01
LC_SYMTAB = 0x02
LC_DYSYMTAB = 0x0B
LC_LOAD_DYLIB = 0x0C
LC_ID_DYLIB = 0x0D
LC_LOAD_DYLINKER = 0x0E
LC_ROUTINES = 0x11
LC_TWOLEVEL_HINTS = 0x16
LC_LOAD_WEAK_DYLIB = 0x18 | 0x80000000
LC_UUID = 0x1B
LC_CODE_SIGNATURE = 0x1D
LC_SEGMENT_SPLIT_INFO = 0x1E
LC_ENCRYPTION_INFO = 0x21
LC_DYLD_INFO = 0x22
LC_DYLD_INFO_ONLY = 0x22 | 0x80000000
LC_VERSION_MIN_IPHONEOS = 0x25

LC_NAMES = {
    LC_SEGMENT: "LC_SEGMENT", LC_SYMTAB: "LC_SYMTAB", LC_DYSYMTAB: "LC_DYSYMTAB",
    LC_LOAD_DYLIB: "LC_LOAD_DYLIB", LC_ID_DYLIB: "LC_ID_DYLIB",
    LC_LOAD_DYLINKER: "LC_LOAD_DYLINKER", LC_ROUTINES: "LC_ROUTINES",
    LC_TWOLEVEL_HINTS: "LC_TWOLEVEL_HINTS", LC_LOAD_WEAK_DYLIB: "LC_LOAD_WEAK_DYLIB",
    LC_UUID: "LC_UUID", LC_CODE_SIGNATURE: "LC_CODE_SIGNATURE",
    LC_SEGMENT_SPLIT_INFO: "LC_SEGMENT_SPLIT_INFO",
    LC_ENCRYPTION_INFO: "LC_ENCRYPTION_INFO", LC_DYLD_INFO: "LC_DYLD_INFO",
    LC_DYLD_INFO_ONLY: "LC_DYLD_INFO_ONLY",
    LC_VERSION_MIN_IPHONEOS: "LC_VERSION_MIN_IPHONEOS",
}

N_STAB, N_TYPE, N_SECT = 0xE0, 0x0E, 0x0E


class MachO:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        b = self.data
        magic, self.cputype, self.cpusubtype, self.filetype, self.ncmds, \
            self.sizeofcmds, self.flags = struct.unpack_from("<7I", b, 0)
        if magic != 0xFEEDFACE:
            raise SystemExit(f"not a 32-bit little-endian Mach-O: {magic:#x}")
        self.cmds = []
        self.segments = []   # (name, vmaddr, vmsize, fileoff, filesize, maxprot, initprot)
        self.sections = []   # (segname, sectname, addr, size, offset, flags)
        self.dylibs = []
        self.encryption = None
        self.symtab = None
        self._parse_cmds()

    def _parse_cmds(self):
        b = self.data
        off = 28
        for _ in range(self.ncmds):
            cmd, cmdsize = struct.unpack_from("<II", b, off)
            self.cmds.append((cmd, cmdsize, off))
            if cmd == LC_SEGMENT:
                name = b[off + 8:off + 24].rstrip(b"\0").decode()
                vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, _fl = \
                    struct.unpack_from("<8I", b, off + 24)
                self.segments.append((name, vmaddr, vmsize, fileoff, filesize,
                                      maxprot, initprot))
                so = off + 56
                for _s in range(nsects):
                    sect = b[so:so + 16].rstrip(b"\0").decode()
                    seg = b[so + 16:so + 32].rstrip(b"\0").decode()
                    addr, size, soff, _align, _reloff, _nreloc, sflags = \
                        struct.unpack_from("<7I", b, so + 32)
                    self.sections.append((seg, sect, addr, size, soff, sflags))
                    so += 68
            elif cmd in (LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_ID_DYLIB):
                noff, = struct.unpack_from("<I", b, off + 8)
                s = b[off + noff:off + cmdsize]
                self.dylibs.append(s.split(b"\0")[0].decode())
            elif cmd == LC_ENCRYPTION_INFO:
                cryptoff, cryptsize, cryptid = struct.unpack_from("<3I", b, off + 8)
                self.encryption = (cryptoff, cryptsize, cryptid)
            elif cmd == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = struct.unpack_from("<4I", b, off + 8)
                self.symtab = (symoff, nsyms, stroff, strsize)
            off += cmdsize

    # --- address helpers -------------------------------------------------
    def off(self, va):
        """Virtual address -> file offset, or None if unmapped."""
        for name, vmaddr, vmsize, fileoff, filesize, _mp, _ip in self.segments:
            if name == "__PAGEZERO":
                continue
            if vmaddr <= va < vmaddr + vmsize:
                delta = va - vmaddr
                if delta < filesize:
                    return fileoff + delta
                return None   # in __bss/__common, no file backing
        return None

    def cstr(self, va):
        o = self.off(va)
        if o is None:
            return ""
        e = self.data.find(b"\0", o)
        return self.data[o:e].decode("utf-8", "replace")

    def section(self, seg, sect):
        for s in self.sections:
            if s[0] == seg and s[1] == sect:
                return s
        return None

    def section_bytes(self, seg, sect):
        s = self.section(seg, sect)
        if not s:
            return b""
        return self.data[s[4]:s[4] + s[3]]

    # --- symbols ---------------------------------------------------------
    def symbols(self):
        """Yield (name, n_type, n_sect, n_desc, n_value) from LC_SYMTAB."""
        if not self.symtab:
            return
        symoff, nsyms, stroff, _strsize = self.symtab
        b = self.data
        for i in range(nsyms):
            o = symoff + i * 12
            strx, ntype, nsect, ndesc, nvalue = struct.unpack_from("<IBBhI", b, o)
            e = b.find(b"\0", stroff + strx)
            yield (b[stroff + strx:e].decode("utf-8", "replace"),
                   ntype, nsect, ndesc, nvalue)


def main():
    m = MachO(sys.argv[1] if len(sys.argv) > 1 else
              os.environ.get("RE_TARGET", "target/binary"))
    print(f"cputype={m.cputype} cpusubtype={m.cpusubtype} "
          f"filetype={m.filetype} ncmds={m.ncmds} flags={m.flags:#x}")
    print("\n--- LOAD COMMANDS ---")
    for cmd, size, off in m.cmds:
        print(f"  {LC_NAMES.get(cmd, hex(cmd)):<26} size={size}")
    print("\n--- SEGMENTS / SECTIONS ---")
    for name, vmaddr, vmsize, fileoff, filesize, mp, ip in m.segments:
        print(f"  {name:<12} vm={vmaddr:#010x}+{vmsize:#x}  "
              f"file={fileoff:#x}+{filesize:#x}  prot={ip}/{mp}")
        for seg, sect, addr, size, soff, fl in m.sections:
            if seg == name:
                print(f"      {sect:<20} addr={addr:#010x} size={size:#8x} "
                      f"off={soff:#8x} flags={fl:#x}")
    print("\n--- ENCRYPTION ---")
    if m.encryption:
        co, cs, cid = m.encryption
        print(f"  LC_ENCRYPTION_INFO cryptoff={co:#x} cryptsize={cs:#x} cryptid={cid}")
        print("  => " + ("ENCRYPTED (FairPlay) - code is unreadable"
                         if cid else "cryptid=0 - NOT encrypted, code is readable"))
    else:
        print("  no LC_ENCRYPTION_INFO -> not FairPlay protected")
    print("\n--- LINKED DYLIBS/FRAMEWORKS ---")
    for d in m.dylibs:
        print("  " + d)
    if m.symtab:
        print(f"\n--- SYMTAB --- nsyms={m.symtab[1]}")


if __name__ == "__main__":
    main()
