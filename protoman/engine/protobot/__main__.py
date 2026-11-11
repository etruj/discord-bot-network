"""CLI for both bot options:

  python3 -m protobot sketch tracing.jpg -l 120 -w 40 -t 4
  python3 -m protobot photo  object.jpg -t 4            # size from the paper
  python3 -m protobot photo  object.jpg -l 101.6 -w 63.5 -t 50.8
  python3 -m protobot sketch tracing.jpg -l 90 -w 87 -t 4 --symmetry lr --straighten
  python3 -m protobot revise jobs/<job> "E3 up 2, C2 R5, H1 circle 6"
  python3 -m protobot revise jobs/<job> "E3 is 20mm"            # absolute target
  python3 -m protobot revise jobs/<job> "E3 is 20mm" --override # drop the blocker

Every job writes drawing.png/.pdf: a ballooned feature map, every edge
(E#), corner (C#) and hole (H#) with exact coordinates. Reply with `revise`.
"""
import argparse
import sys

from .pipeline import photo_to_part, revise_part, sketch_to_part
from .refine import ConflictError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="protobot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="kind", required=True)

    def common(p, size_required):
        p.add_argument("image")
        p.add_argument("-l", "--length", type=float, required=size_required,
                       help="X extent, mm")
        p.add_argument("-w", "--width", type=float, required=size_required,
                       help="Y extent, mm")
        p.add_argument("-t", "--thickness", type=float, required=True,
                       help="extrusion, mm")
        p.add_argument("-n", "--name", help="part name (default: image filename)")
        p.add_argument("-o", "--out", help="output folder (default: jobs/<time>_<name>)")
        p.add_argument("--no-holes", action="store_true", help="ignore interior holes")
        p.add_argument("--orient", choices=["auto", "as-drawn"], default="auto",
                       help="auto: turn 90° so the long sides match")
        p.add_argument("--smooth", type=float, default=2.0,
                       help="edge smoothing, 0 = off (default 2)")
        p.add_argument("--version", type=int, default=1, help="v# stamped underneath")
        p.add_argument("--no-preview", action="store_true")
        p.add_argument("--symmetry", choices=["lr", "tb", "both"],
                       help="shape mirrors left/right, top/bottom, or both")
        p.add_argument("--straighten", action="store_true",
                       help="true straight edges, clean corners, round holes")
        p.add_argument("--edits", help='label edits, e.g. "E3 straight, H1 circle 6"')

    s = sub.add_parser("sketch", help="a drawn tracing / silhouette")
    common(s, True)
    s.add_argument("--mode", choices=["auto", "filled", "outline"], default="auto",
                   help="filled-in shape or just an outline (default: guess)")
    s.add_argument("--invert", action="store_true",
                   help="drawing is lighter than its background")

    p = sub.add_parser("photo", help="a photo of the real object lying on paper")
    common(p, False)
    p.add_argument("--paper", choices=["letter", "a4", "none"], default="letter",
                   help="sheet under the object (for straightening + scale)")
    rv = sub.add_parser("revise", help="apply label edits to a finished job")
    rv.add_argument("job_dir")
    rv.add_argument("edits", nargs="?", default="",
                    help='e.g. "E3 up 2, E1 straight, C2 R5, C4 sharp, H1 circle 6, H2 remove"')
    rv.add_argument("--symmetry", choices=["lr", "tb", "both", "off"])
    rv.add_argument("--straighten", choices=["on", "off"])
    rv.add_argument("--no-preview", action="store_true")
    rv.add_argument("--override", nargs="*", metavar="LABEL",
                    help="on a conflict, drop the earlier requirement that blocks "
                         "it (all named in the error, or just these labels, e.g. "
                         "E5 width)")
    a = ap.parse_args(argv)

    try:
        if a.kind == "revise":
            sym = "keep" if a.symmetry is None else (None if a.symmetry == "off" else a.symmetry)
            st = None if a.straighten is None else a.straighten == "on"
            ov = False if a.override is None else (a.override or True)
            r = revise_part(a.job_dir, a.edits, symmetry=sym, straighten=st,
                            preview=not a.no_preview, override=ov)
            return _report(r)
        kw = dict(name=a.name, out_dir=a.out, holes=not a.no_holes, orient=a.orient,
                  smooth=a.smooth, version=a.version, preview=not a.no_preview,
                  symmetry=a.symmetry, straighten=a.straighten, edits=a.edits)
        if a.kind == "sketch":
            r = sketch_to_part(a.image, a.length, a.width, a.thickness,
                               mode=a.mode, invert=a.invert, **kw)
        else:
            r = photo_to_part(a.image, a.thickness, a.length, a.width,
                              paper=a.paper, **kw)
    except ConflictError as e:
        print(f"conflict: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return _report(r)


def _report(r) -> int:
    print(r.summary())
    print(f"\nscad    {r.scad}\nstl     {r.stl}\ntrace   {r.overlay}\ndrawing {r.drawing}"
          + (f"\npreview {r.preview}" if r.preview else ""))
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
