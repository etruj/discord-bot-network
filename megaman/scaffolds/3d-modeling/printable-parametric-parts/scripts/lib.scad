// ============================================================================
//  lib.scad  —  printable-parametric-parts reusable modules
//  Tested on a 0.4 mm-nozzle FDM workflow. `use <lib.scad>` (no top-level
//  geometry here). See example.scad for a runnable two-part dovetail demo.
// ============================================================================

// ---- recommended FDM constants (0.4 mm nozzle; tune per printer) ----
PP_FIT_CLEAR   = 0.10;  // per-side sliding-fit clearance (default; tune up if it binds)
PP_EDGE_FILLET = 0.30;  // exposed-edge fillet radius
PP_ENGRAVE     = 0.30;  // text/logo engrave depth
PP_EMBED       = 0.20;  // feature embed depth for manifold fusion
PP_SPHERE_FN   = 20;    // sphere facets for fillets (keep modest; minkowski is slow)

// 2D rounded rectangle, centered at origin.
module pp_rrect(w, h, r) {
    offset(r=r) square([w - 2*r, h - 2*r], center=true);
}

// All-edge-rounded extrusion of a 2D child profile, sitting z = 0..t.
// Rounds top, bottom, side, AND hole edges by `er`. Minkowski-based: slow, and
// can emit sliver triangles — always validate the exported mesh afterwards.
// Keep functional MATING surfaces out of this (round what you touch, not joints).
module pp_rounded_body(t, er = PP_EDGE_FILLET, sfn = PP_SPHERE_FN) {
    translate([0, 0, t/2]) minkowski() {
        linear_extrude(t - 2*er, center = true) offset(r = -er) children();
        sphere(r = er, $fn = sfn);
    }
}

// Dovetail prism swept along X. Narrow at z=0 (mouth) -> wide at z=depth.
// Use as a GROOVE cutter (difference from a body).
module pp_dovetail_x(x0, x1, yc, w_narrow, w_wide, depth) {
    polyhedron(
        points = [
            [x0, yc-w_narrow/2, 0],[x0, yc+w_narrow/2, 0],
            [x0, yc+w_wide/2, depth],[x0, yc-w_wide/2, depth],
            [x1, yc-w_narrow/2, 0],[x1, yc+w_narrow/2, 0],
            [x1, yc+w_wide/2, depth],[x1, yc-w_wide/2, depth]],
        faces = [[0,3,2,1],[4,5,6,7],[0,1,5,4],[1,2,6,5],[2,3,7,6],[3,0,4,7]],
        convexity = 6);
}

// Dovetail prism swept along Y. Narrow at z=0 (mouth) -> wide at z=depth.
module pp_dovetail_y(y0, y1, xc, w_narrow, w_wide, depth) {
    polyhedron(
        points = [
            [xc-w_narrow/2, y0, 0],[xc+w_narrow/2, y0, 0],
            [xc+w_wide/2, y0, depth],[xc-w_wide/2, y0, depth],
            [xc-w_narrow/2, y1, 0],[xc+w_narrow/2, y1, 0],
            [xc+w_wide/2, y1, depth],[xc-w_wide/2, y1, depth]],
        faces = [[0,3,2,1],[4,5,6,7],[0,1,5,4],[1,2,6,5],[2,3,7,6],[3,0,4,7]],
        convexity = 6);
}

// Mating dovetail TONGUE swept along Y, with lead-in ramps at both ends so it
// self-guides into the groove. Shrunk from the groove by 2*clear (per side) and
// embedded `embed` into the plate for a manifold union.
// CALL AS:  translate([0,0,plate_t]) pp_tongue_y(y0, y1, xc, gN, gW, gD);
// where gN/gW/gD are the matching GROOVE narrow/wide/depth.
module pp_tongue_y(y0, y1, xc, groove_narrow, groove_wide, groove_depth,
                   clear = PP_FIT_CLEAR, lead = 3, embed = PP_EMBED) {
    wn = groove_narrow - 2*clear;
    ww = groove_wide   - 2*clear;
    Dfull = groove_depth - 0.15 + embed;   // protrude (gD-0.15) above plate top
    Dtip  = embed;                          // flush tip
    translate([0, 0, -embed]) {
        pp_dovetail_y(y0 + lead, y1 - lead, xc, wn, ww, Dfull);          // straight
        hull() { pp_dovetail_y(y1-lead, y1-lead+0.01, xc, wn, ww, Dfull);
                 pp_dovetail_y(y1-0.01, y1,           xc, wn, ww, Dtip); } // top ramp
        hull() { pp_dovetail_y(y0+lead-0.01, y0+lead, xc, wn, ww, Dfull);
                 pp_dovetail_y(y0, y0+0.01,           xc, wn, ww, Dtip); } // bottom ramp
    }
}

// Engrave text into a +Z face at height z_face (difference this from the part).
// mirror_x=true for faces READ FROM THE BACK (bed/outer face) so text reads true.
// IMPORTANT: place x,y on SOLID material — verify it isn't inside a window/cutout.
module pp_engrave(txt, x, y, z_face, size = 3.2, depth = PP_ENGRAVE, mirror_x = false) {
    translate([x, y, z_face - depth])
        linear_extrude(depth + 0.01)
            scale([mirror_x ? -1 : 1, 1, 1])
                text(txt, size = size, halign = "center", valign = "center");
}

// Deboss an imported SVG (logo) into a face. For the bottom/outer face use
// z_face=0 and mirror_x=true. diameter scales the SVG bounding box to d mm.
// CALL: difference(){ part(); pp_deboss_svg("logo.svg", 0,0, 45, z_face=0, mirror_x=true); }
module pp_deboss_svg(path, x, y, d, z_face = 0, depth = PP_ENGRAVE, mirror_x = false) {
    translate([x, y, z_face - 0.001])
        linear_extrude(depth + 0.002)
            scale([mirror_x ? -1 : 1, 1, 1])
                resize([d, d]) import(path, center = true);
}

// ============================================================================
//  Cantilever leaf-spring clip  (page-clip / snap-tongue family)
//  A U-slot frees a tongue hinged along its top edge. Lessons baked in:
//   - ROUNDED bottom corners via explicit arc polygon (NOT stamped circles or
//     offset(+r)offset(-r), which collapse thin slot walls and corrupt the mesh).
//   - SQUARE, full-thickness hinge: the rounded rect is extended above the hinge
//     and the straight walls run tall, so the hinge edge stays flush & strong.
//   - Slot cut with TALL inner+outer walls then an OVERLAPPING opener above the
//     hinge — never a frame "cap" meeting the opener at the same line (coincident
//     faces -> CGAL "not closed mesh", even when trimesh reports watertight).
// ----------------------------------------------------------------------------

// 2D outline of the tongue: rounded bottom corners, square top at y=ytop.
// hw=half width, ybot=free-end y, ytop=hinge y, cc=corner radius, grow=offset.
module pp_clip_shape(hw, ybot, ytop, cc, grow = 0) {
    HW = hw + grow; CC = cc + grow; YB = ybot - grow;
    bl = [-HW+CC, YB+CC]; br = [HW-CC, YB+CC];
    polygon(concat(
        [[-HW, ytop]],
        [for (a=[180:10:270]) [bl[0]+CC*cos(a), bl[1]+CC*sin(a)]],
        [for (a=[270:10:360]) [br[0]+CC*cos(a), br[1]+CC*sin(a)]],
        [[HW, ytop]]));
}

// U-slot cutter (2D). difference() this (linear_extrude'd through the plate) to
// free the tongue. sw = slot width. Walls run tall to ytop+30 and an opener
// clears everything above ytop, so the tongue connects ONLY along the hinge.
module pp_clip_uslot2d(hw, ybot, ytop, cc, sw) {
    yhi = ytop + 30;
    difference() {
        pp_clip_shape(hw, ybot, yhi, cc, sw);   // outer wall (tall)
        pp_clip_shape(hw, ybot, yhi, cc, 0);    // inner wall (tall)
        translate([-(hw+sw+5), ytop]) square([2*(hw+sw+5), 60]); // overlapping opener
    }
}

// Wedge taper for the tongue: full plate thickness `t` at the hinge tapering to
// `tip_t` at the free end, so the leaf flexes easily and slips past pages.
// difference() this from the part. Clipped to the tongue (grown 0.3 into the
// empty slot) so the slope reaches every edge with no coincident faces.
module pp_clip_taper(hw, ybot, ytop, cc, t, tip_t) {
    HW = hw + 4;
    intersection() {
        hull() {
            translate([-HW, ybot-0.5, tip_t]) cube([2*HW, 1, t + 2 - tip_t]);
            translate([-HW, ytop-0.5, t])     cube([2*HW, 1, 2]);
        }
        translate([0,0,0.001]) linear_extrude(t + 2)
            pp_clip_shape(hw, ybot, ytop, cc, 0.3);
    }
}

// Cut a 2D profile (text, SVG, shape) CLEAN THROUGH a part of thickness `t`.
// For multi-colour prints: the part behind shows through the cut, so a logo
// reads in the second material. Read from +Z (visible) face -> mirror_x=false.
module pp_cut_through(t, mirror_x = false) {
    translate([0, 0, -1]) linear_extrude(t + 2)
        scale([mirror_x ? -1 : 1, 1, 1]) children();
}
