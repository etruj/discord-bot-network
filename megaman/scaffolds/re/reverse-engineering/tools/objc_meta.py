#!/usr/bin/env python3
"""Dump ObjC 2.0 class metadata (classes, ivars, methods) from the target binary.

    tools/objc_meta.py [--exe PATH] [--out analysis/objc]

Writes classes.tsv, ivars.tsv, methods.tsv. Ghidra's own ObjC handling does
not produce ivar structs, so tools/ghidra/ApplyObjcTypes.java reads these to
build one struct per class and type every method's `self`.
"""
# Copied into this project by claude-pm from the a prior legacy-iOS decompilation project
# (an earlier decompilation project). The hardcoded target path was replaced by
# $RE_TARGET (default: target/binary); everything else is verbatim, so the
# docstring still describes that binary. Read before trusting a default.

import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import macho  # noqa: E402

DEF_EXE = os.environ.get("RE_TARGET", "target/binary")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=DEF_EXE)
    ap.add_argument("--out", default=os.path.join(ROOT, "analysis/objc"))
    a = ap.parse_args()
    m = macho.MachO(a.exe)
    os.makedirs(a.out, exist_ok=True)

    def u32(va):
        o = m.off(va)
        return struct.unpack_from("<I", m.data, o)[0]

    cl = m.section("__DATA", "__objc_classlist")
    n = cl[3] // 4
    classes = [u32(cl[2] + 4 * i) for i in range(n)]

    # superclass pointers to other images are dyld-bound (0 in the file);
    # recover the symbol from the bind opcodes' fallout: the __objc_data
    # class_t.superclass slot is bound to _OBJC_CLASS_$_X. Use the nl symbol
    # table via the indirect symbols is overkill; instead map by address of
    # each in-image class_t and treat 0 as external, named via ro.name walk
    # of the metaclass chain as printed by the linker (see ext_super below).
    by_addr = {}
    rows = []
    for c in classes:
        ro = u32(c + 16) & ~3
        name = m.cstr(u32(ro + 16))
        by_addr[c] = name
        rows.append((c, ro, name))

    ext = {}
    # Bound (external) superclasses: read the dyld bind info for the slots.
    ext = bound_superclasses(m)

    with open(os.path.join(a.out, "classes.tsv"), "w") as fc, \
         open(os.path.join(a.out, "ivars.tsv"), "w") as fi, \
         open(os.path.join(a.out, "methods.tsv"), "w") as fm:
        fc.write("class\tsuper\tclass_va\tro_va\tinstance_start\tinstance_size\n")
        fi.write("class\tname\ttype\toffset\tsize\talign\toffset_var\n")
        fm.write("class\tkind\tselector\ttypes\timp\n")
        for c, ro, name in rows:
            sup = u32(c + 4)
            if sup in by_addr:
                sname = by_addr[sup]
            else:
                sname = ext.get(c + 4, "?")
            fc.write("%s\t%s\t0x%x\t0x%x\t%d\t%d\n" % (
                name, sname, c, ro, u32(ro + 4), u32(ro + 8)))
            ivl = u32(ro + 28)
            if ivl:
                cnt = u32(ivl + 4)
                for i in range(cnt):
                    e = ivl + 8 + i * 20
                    off = u32(u32(e))
                    fi.write("%s\t%s\t%s\t%d\t%d\t%d\t0x%x\n" % (
                        name, m.cstr(u32(e + 4)), m.cstr(u32(e + 8)),
                        off, u32(e + 16), u32(e + 12), u32(e)))
            for kind, meth_ro in (("-", ro), ("+", u32(c) & ~0)):
                pass
            write_methods(m, fm, name, "-", u32(ro + 20), u32)
            meta = u32(c)  # isa -> metaclass (in-image)
            if meta:
                mro = u32(meta + 16) & ~3
                write_methods(m, fm, name, "+", u32(mro + 20), u32)
        # categories (__objc_catlist): category_t {name, cls, instanceMethods,
        # classMethods, protocols, instanceProperties}. Their methods are the
        # class's methods at runtime (e.g. Texture2D(Text) initWithString:...,
        # Texture2D(Drawing) drawInRect:..., getImgW/getImgH).
        ncat = 0
        try:
            cat = m.section("__DATA", "__objc_catlist")
        except Exception:
            cat = None
        if cat:
            for i in range(cat[3] // 4):
                c = u32(cat[2] + 4 * i)
                cname = m.cstr(u32(c))
                cls = u32(c + 4)
                base = by_addr.get(cls) or ext.get(c + 4, "?")
                write_methods(m, fm, base, "-", u32(c + 8), u32)
                write_methods(m, fm, base, "+", u32(c + 12), u32)
                print("category %s(%s)" % (base, cname))
                ncat += 1
    print("classes:", len(rows), "categories:", ncat, "->", a.out)


def write_methods(m, fm, cname, kind, ml, u32):
    if not ml:
        return
    cnt = u32(ml + 4)
    for i in range(cnt):
        e = ml + 8 + i * 12
        fm.write("%s\t%s\t%s\t%s\t0x%x\n" % (
            cname, kind, m.cstr(u32(e)), m.cstr(u32(e + 4)), u32(e + 8)))


def bound_superclasses(m):
    """Return {slot_va: symbol} for dyld-bound pointers via LC_DYLD_INFO."""
    out = {}
    for c in m.cmds:
        if c[0] not in (0x22, 0x80000022):
            continue
        # cmd, cmdsize, rebase_off, rebase_size, bind_off, bind_size, ...
        vals = struct.unpack_from("<IIIIIIIIII", m.data, c[1])
        bind_off, bind_size = vals[4], vals[5]
        data = m.data[bind_off:bind_off + bind_size]
        segs = [s for s in m.segments]
        sym, seg, off, i = "", 0, 0, 0

        def uleb():
            nonlocal i
            r, sh = 0, 0
            while True:
                b = data[i]
                i += 1
                r |= (b & 0x7f) << sh
                sh += 7
                if not b & 0x80:
                    return r

        while i < len(data):
            b = data[i]
            i += 1
            op, imm = b & 0xf0, b & 0x0f
            if op == 0x00:
                pass
            elif op == 0x10 or op == 0x20:
                pass
            elif op == 0x30:
                uleb()
            elif op == 0x40:
                e = data.index(b"\0", i)
                sym = data[i:e].decode()
                i = e + 1
            elif op == 0x50:
                pass
            elif op == 0x60:
                uleb()
            elif op == 0x70:
                seg = imm
                off = uleb()
            elif op == 0x80:
                off += uleb()
            elif op in (0x90, 0xa0, 0xb0, 0xc0):
                if op == 0x90:
                    n = 1
                    adv = 0
                elif op == 0xa0:
                    n = 1
                    adv = uleb()
                elif op == 0xb0:
                    n = 1
                    adv = imm * 4
                else:
                    n = uleb()
                    adv = uleb()
                for _ in range(n):
                    out[segs[seg][1] + off] = sym.replace("_OBJC_CLASS_$_", "")
                    off += 4 + adv if op != 0x90 else 4
                    if op == 0xa0 or op == 0xb0:
                        off += 0
                if op in (0xa0, 0xb0):
                    pass
            else:
                pass
    return out


if __name__ == "__main__":
    main()
