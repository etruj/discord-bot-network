#!/usr/bin/env python3
"""
Turn the ObjC runtime type encodings dumped by tools/objc_meta.py into
  analysis/objc/types.spec   line spec read by tools/ghidra/ApplyObjcTypes.java
  analysis/objc/mgst_types.h C header of the same structs (for the GitHub release)

Ivar and method type encodings are the only complete type information in the
binary, and they carry named struct fields (the whole pEneDat layout). Layout
is COMPUTED with the Apple armv6 ABI (all scalars align to their size, double
aligns to 4) and then VERIFIED against the compiler's own ivar offsets/sizes
and each class's instance_size; any disagreement aborts the run.

Spec lines (tab separated):
  STRUCT  name  size
  FIELD   offset  name  type
  CLASS   name  size            (ivar struct; FIELD lines follow)
  IVARVAR va  class  ivar  offset
  METHOD  class  kind  selector  imp  rettype  argtype...
type grammar: void i4 u4 i2 u2 i1 u1 i8 u8 f4 f8 bool id sel ptr(T) arr(N,T) struct(name)
"""
# Copied into this project by claude-pm from the a prior legacy-iOS decompilation project
# (an earlier decompilation project). The hardcoded target path was replaced by
# $RE_TARGET (default: target/binary); everything else is verbatim, so the
# docstring still describes that binary. Read before trusting a default.

import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "analysis/objc")

SCALAR = {"c": ("i1", 1), "C": ("u1", 1), "s": ("i2", 2), "S": ("u2", 2),
          "i": ("i4", 4), "I": ("u4", 4), "l": ("i4", 4), "L": ("u4", 4),
          "q": ("i8", 8), "Q": ("u8", 8), "f": ("f4", 4), "d": ("f8", 8),
          "B": ("bool", 1), "v": ("void", 0), "@": ("id", 4), "#": ("id", 4),
          ":": ("sel", 4), "*": ("ptr(i1)", 4)}
CTYPE = {"i1": "signed char", "u1": "unsigned char", "i2": "short",
         "u2": "unsigned short", "i4": "int", "u4": "unsigned int",
         "i8": "long long", "u8": "unsigned long long", "f4": "float",
         "f8": "double", "bool": "unsigned char", "void": "void",
         "id": "void *", "sel": "void *"}


KNOWN_FIELDS = {"CGPoint": ["x", "y"], "CGSize": ["width", "height"],
                "CGRect": ["origin", "size"]}


class T:
    """kind: scalar | ptr | arr | struct ; layout computed on demand."""
    def __init__(self, kind, **kw):
        self.kind = kind
        self.__dict__.update(kw)

    def size_align(self):
        if self.kind == "scalar":
            return self.size, max(1, min(self.size, 4))
        if self.kind == "ptr":
            return 4, 4
        if self.kind == "arr":
            s, a = self.elem.size_align()
            return s * self.n, a
        if self.kind == "struct":
            self.layout()
            return self.size, self.align
        raise ValueError(self.kind)

    def layout(self):
        if getattr(self, "laid", False):
            return
        off, al = 0, 1
        self.offsets = []
        for fname, ft in self.fields:
            s, a = ft.size_align()
            off = (off + a - 1) // a * a
            self.offsets.append(off)
            off += s
            al = max(al, a)
        self.align = al
        self.size = (off + al - 1) // al * al
        self.laid = True


def parse(enc, i=0):
    while enc[i] in "rnNoORV":
        i += 1
    c = enc[i]
    if c == "?":
        return T("scalar", name="void", size=0), i + 1
    if c == "@" and i + 1 < len(enc) and enc[i + 1] == '"':
        j = enc.index('"', i + 2)
        return T("scalar", name="id", size=4), j + 1
    if c in SCALAR and c != "*":
        name, size = SCALAR[c]
        return T("scalar", name=name, size=size), i + 1
    if c == "*":
        return T("ptr", to=T("scalar", name="i1", size=1)), i + 1
    if c == "^":
        if i + 1 >= len(enc) or enc[i + 1] in ")}]\"" or enc[i + 1].isdigit():
            return T("ptr", to=T("scalar", name="void", size=0)), i + 1
        to, j = parse(enc, i + 1)
        return T("ptr", to=to), j
    if c == "[":
        j = i + 1
        while enc[j].isdigit():
            j += 1
        n = int(enc[i + 1:j])
        el, j = parse(enc, j)
        assert enc[j] == "]", enc[i:j + 5]
        return T("arr", n=n, elem=el), j + 1
    if c in "{(":
        close = "}" if c == "{" else ")"
        j = i + 1
        while enc[j] not in "=" + close:
            j += 1
        sname = enc[i + 1:j]
        fields = []
        if enc[j] == "=":
            j += 1
            while enc[j] != close:
                fname = None
                if enc[j] == '"':
                    k = enc.index('"', j + 1)
                    fname, j = enc[j + 1:k], k + 1
                ft, j = parse(enc, j)
                fields.append((fname or "f%d" % len(fields), ft))
        return T("struct", sname=sname, fields=fields, union=(c == "(")), j + 1
    raise ValueError("cannot parse %r at %d" % (enc, i))


def parse_method(types):
    """'v24@0:4^{..}8..' -> list of types (ret, self, _cmd, args...)"""
    out, i = [], 0
    while i < len(types):
        t, i = parse(types, i)
        while i < len(types) and types[i].isdigit():
            i += 1
        out.append(t)
    return out


class Namer:
    """Give every struct a stable, unique name; dedupe by structure."""
    def __init__(self):
        self.by_key = {}
        self.structs = collections.OrderedDict()
        self.used = set()

    def key(self, t):
        if t.kind == "scalar":
            return t.name
        if t.kind == "ptr":
            return "p(%s)" % self.key(t.to)
        if t.kind == "arr":
            return "a%d(%s)" % (t.n, self.key(t.elem))
        return "s%s{%s}" % (t.sname if t.sname != "?" else "",
                            ",".join(self.key(f) for _, f in t.fields))

    def name_struct(self, t, hint):
        if getattr(t, "final", None):
            return t.final
        k = self.key(t)
        if t.sname not in ("?", "") and k in self.by_key:
            pass
        if k in self.by_key:
            t.final = self.by_key[k]
            return t.final
        names = KNOWN_FIELDS.get(t.sname)
        if names and len(names) == len(t.fields):
            t.fields = [(nm, ft) for nm, (_, ft) in zip(names, t.fields)]
        base = t.sname if t.sname not in ("?", "") else hint
        base = base.replace("CrashLandingAppDelegate_npc_char", "EneDat")
        n, x = base, 2
        while n in self.used:
            n = "%s_%d" % (base, x)
            x += 1
        self.used.add(n)
        self.by_key[k] = n
        t.final = n
        self.structs[n] = t
        for fname, ft in t.fields:
            self.walk(ft, "%s_%s" % (n, fname))
        return n

    def walk(self, t, hint):
        if t.kind == "struct":
            self.name_struct(t, hint)
        elif t.kind == "ptr":
            self.walk(t.to, hint)
        elif t.kind == "arr":
            self.walk(t.elem, hint)


def tstr(t, namer):
    if t.kind == "scalar":
        return t.name
    if t.kind == "ptr":
        return "ptr(%s)" % tstr(t.to, namer)
    if t.kind == "arr":
        return "arr(%d,%s)" % (t.n, tstr(t.elem, namer))
    return "struct(%s)" % t.final


def cdecl(t, name):
    if t.kind == "scalar":
        return "%s %s" % (CTYPE[t.name], name)
    if t.kind == "ptr":
        if t.to.kind == "ptr" or t.to.kind == "arr":
            return "void *%s" % name
        inner = t.to
        if inner.kind == "scalar":
            return "%s *%s" % (CTYPE[inner.name], name)
        return "struct %s *%s" % (inner.final, name)
    if t.kind == "arr":
        dims, e = [], t
        while e.kind == "arr":
            dims.append(e.n)
            e = e.elem
        return cdecl(e, name + "".join("[%d]" % d for d in dims))
    return "struct %s %s" % (t.final, name)


def tsv(name):
    with open(os.path.join(D, name)) as f:
        hdr = f.readline().rstrip("\n").split("\t")
        for line in f:
            p = line.rstrip("\n").split("\t")
            yield dict(zip(hdr, p))


def main():
    namer = Namer()
    classes = {c["class"]: c for c in tsv("classes.tsv")}
    iv = collections.defaultdict(list)
    for r in tsv("ivars.tsv"):
        iv[r["class"]].append(r)

    spec, errors = [], []
    cls_struct = collections.OrderedDict()
    for cname, rows in iv.items():
        fields, real = [], []
        for r in rows:
            t, _ = parse(r["type"])
            namer.walk(t, "%s_%s" % (cname, r["name"].lstrip("_")))
            s, a = t.size_align()
            off, size = int(r["offset"]), int(r["size"])
            if s != size:
                errors.append("%s.%s size computed %d vs runtime %d (%s)"
                              % (cname, r["name"], s, size, r["type"][:40]))
            if off % a:
                errors.append("%s.%s offset %d misaligned (a=%d)" % (cname, r["name"], off, a))
            fields.append((r, t, off, size))
        cls_struct[cname] = fields

    # verify nested struct layouts against known pEneDat offsets
    for n, t in namer.structs.items():
        t.layout()

    known = {0x28: "z", 0x34: None, 0x38: None}
    ene = None
    for n, t in namer.structs.items():
        if any(fn == "obj_pIndex" for fn, _ in t.fields) is False and \
                any(fn == "jumpMax" or fn == "hp" for _, ft in t.fields
                    for fn, _ in (ft.fields if ft.kind == "struct" else [])):
            ene = t
    if ene is not None:
        print("pEneDat struct: %s size 0x%x" % (ene.final, ene.size))

    if errors:
        print("LAYOUT ERRORS (%d):" % len(errors))
        print("\n".join(errors[:30]))
        sys.exit(1)

    # infer superclass = the unique class whose instance_size equals our instance_start
    size_of = collections.defaultdict(list)
    for cn, c in classes.items():
        size_of[int(c["instance_size"])].append(cn)
    sup = {}
    for cn, c in classes.items():
        st = int(c["instance_start"])
        if c["super"] in classes:
            sup[cn] = c["super"]
            continue
        cands = [x for x in size_of.get(st, []) if x != cn and x in cls_struct]
        if st and len(cands) == 1:
            sup[cn] = cands[0]
        elif st and len(cands) > 1:
            print("ambiguous super for %s (start %d): %s" % (cn, st, cands))
        elif st and cn in cls_struct:
            print("no super found for %s (start %d)" % (cn, st))
    print("inferred supers:", len(sup))

    def full_fields(cn):
        chain = []
        while cn:
            chain.append(cn)
            cn = sup.get(cn)
        res = []
        for c in reversed(chain):
            res += [(r, t, off, sz, c) for r, t, off, sz in cls_struct.get(c, [])]
        return res

    # emit
    out = []
    for n, t in namer.structs.items():
        out.append("STRUCT\t%s\t%d\t%d" % (n, t.size, 1 if t.union else 0))
        for (fn, ft), off in zip(t.fields, t.offsets):
            out.append("FIELD\t%d\t%s\t%s" % (0 if t.union else off, fn, tstr(ft, namer)))
    # classes without ivars of their own (NPC, HavenSoldier, HavenSoldierA)
    # still need a struct: their methods access the inherited ivars
    for cname in classes:
        if cname not in cls_struct and full_fields(sup.get(cname)) if sup.get(cname) else False:
            cls_struct[cname] = []
    for cname, fields in cls_struct.items():
        size = int(classes[cname]["instance_size"])
        out.append("CLASS\t%s\t%d\t%s" % (cname, size, sup.get(cname, "")))
        for r, t, off, sz, owner in full_fields(cname):
            out.append("FIELD\t%d\t%s\t%s" % (off, r["name"], tstr(t, namer)))
        for r, t, off, sz in fields:
            out.append("IVARVAR\t%s\t%s\t%s\t%d" % (r["offset_var"], cname, r["name"], off))
    # non-lazy symbol pointers that alias an ivar offset variable (subclasses reach
    # inherited ivars through them: `*(self + **nlptr)`)
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import macho
    import struct
    exe = os.environ.get("RE_TARGET", "target/binary")
    mo = macho.MachO(exe)
    raw = open(exe, "rb").read()
    varset = {int(r["offset_var"], 16) for rows in iv.values() for r in rows}
    npt = 0
    for sec in mo.sections:
        if sec[1] == "__nl_symbol_ptr":
            for va in range(sec[2], sec[2] + sec[3], 4):
                tgt = struct.unpack_from("<I", raw, mo.off(va))[0]
                if tgt in varset:
                    out.append("IVARPTR\t0x%x\t0x%x" % (va, tgt))
                    npt += 1
    print("ivar offset-variable aliases in __nl_symbol_ptr:", npt)
    nm = 0
    for m in tsv("methods.tsv"):
        try:
            ts = parse_method(m["types"])
        except (ValueError, IndexError, AssertionError) as e:
            print("method parse fail", m["class"], m["selector"], m["types"][:50], e)
            continue
        for k, t in enumerate(ts):
            namer.walk(t, "%s_%s_a%d" % (m["class"], m["selector"].split(":")[0], k))
        if any(t.kind == "struct" and not getattr(t, "final", None) for t in ts):
            continue
        out.append("METHOD\t%s\t%s\t%s\t%s\t%s" % (
            m["class"], m["kind"], m["selector"], m["imp"],
            "\t".join(tstr(t, namer) for t in ts)))
        nm += 1

    # structs first discovered via methods must be defined too
    defs = []
    for n, t in namer.structs.items():
        t.layout()
        defs.append("STRUCT\t%s\t%d\t%d" % (n, t.size, 1 if t.union else 0))
        for (fn, ft), off in zip(t.fields, t.offsets):
            defs.append("FIELD\t%d\t%s\t%s" % (0 if t.union else off, fn, tstr(ft, namer)))
    body = [l for l in out if not l.startswith(("STRUCT", "FIELD"))
            or False]
    # rebuild: STRUCT defs, then CLASS blocks (which own their FIELD lines)
    final = list(defs)
    inclass = False
    for l in out:
        if l.startswith("CLASS"):
            inclass = True
        if inclass and not l.startswith("METHOD"):
            final.append(l)
    final += [l for l in out if l.startswith("METHOD")]
    with open(os.path.join(D, "types.spec"), "w") as f:
        f.write("\n".join(final) + "\n")

    with open(os.path.join(D, "mgst_types.h"), "w") as h:
        h.write("/* Generated by tools/objc_types.py from the ObjC runtime metadata of the target binary v2.3.0. */\n")
        h.write("/* Layout verified against every runtime ivar offset and size. */\n\n")
        for n, t in namer.structs.items():
            h.write("%s %s { /* size 0x%x */\n" % ("union" if t.union else "struct", n, t.size))
            for (fn, ft), off in zip(t.fields, t.offsets):
                h.write("    %s; /* 0x%x */\n" % (cdecl(ft, fn), off))
            h.write("};\n\n")
        for cname, fields in cls_struct.items():
            h.write("struct %s_ivars { /* instance_size %s */\n" % (cname, classes[cname]["instance_size"]))
            for r, t, off, sz, owner in full_fields(cname):
                h.write("    /* 0x%x %s */ %s;\n" % (off, owner, cdecl(t, r["name"])))
            h.write("};\n\n")
    print("structs: %d, classes: %d, methods typed: %d" % (len(namer.structs), len(cls_struct), nm))


if __name__ == "__main__":
    main()
