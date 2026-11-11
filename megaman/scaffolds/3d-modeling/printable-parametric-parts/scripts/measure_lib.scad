// ============================================================================
//  measure_lib.scad  —  colour-coded "please measure this" markers
//
//  Used when reverse-engineering an existing object: model what you can from
//  photos/traces, then render the model in neutral grey with each UNKNOWN
//  dimension drawn on it as a coloured bar/ring + a numbered tag. The user
//  replies "1. 26mm  2. remove  3. ..." against the numbers. See SKILL.md
//  ("Reverse-engineering an existing object").
//
//  `use <measure_lib.scad>` — no top-level geometry here. Render with
//  render_measure.sh, which passes the camera angle in as $cam_rx / $cam_rz so
//  the tags always face the camera (no hand-synced constants).
// ============================================================================

// Fixed palette, assigned in order: item 1 = red, 2 = blue, ... Keep the
// mapping stable across rounds so "the orange one" means the same thing.
PP_MARK_COLORS = [
    [0.90, 0.15, 0.15],   // 1 red
    [0.15, 0.40, 0.95],   // 2 blue
    [0.10, 0.75, 0.25],   // 3 green
    [1.00, 0.60, 0.00],   // 4 orange
    [0.85, 0.10, 0.80],   // 5 magenta
    [0.00, 0.70, 0.85],   // 6 cyan
    [0.95, 0.85, 0.05],   // 7 yellow
    [0.55, 0.30, 0.10],   // 8 brown
];
PP_MARK_GHOST = [0.72, 0.72, 0.74];   // the model itself: neutral grey
PP_MARK_D     = 1.6;                  // marker bar cross-section (mm)

// Camera defaults; render_measure.sh overrides these with -D.
$cam_rx = 62;
$cam_rz = 22;

function pp_mark_color(n) = PP_MARK_COLORS[(n - 1) % len(PP_MARK_COLORS)];

// The model under measurement, in grey so the markers carry all the colour.
module pp_mark_ghost() { color(PP_MARK_GHOST) children(); }

// Straight bar from p0 to p1 — an edge length, a thickness, a gap.
module pp_mark_bar(n, p0, p1, d = PP_MARK_D) {
    color(pp_mark_color(n)) hull() {
        translate(p0) sphere(d = d, $fn = 16);
        translate(p1) sphere(d = d, $fn = 16);
    }
}

// Ring of radius r in the local XY plane at `at` — a hole/boss/recess
// diameter. Rotate it with `rot` to lie on a non-XY face.
module pp_mark_ring(n, at, r, rot = [0, 0, 0], d = PP_MARK_D) {
    color(pp_mark_color(n)) translate(at) rotate(rot)
        rotate_extrude($fn = 64) translate([r, 0]) circle(d = d, $fn = 16);
}

// Arc/curve traced through a list of 3D points — a fillet, cove, or any
// profile whose shape (not just size) is in question.
module pp_mark_path(n, pts, d = PP_MARK_D) {
    color(pp_mark_color(n))
        for (i = [0 : len(pts) - 2]) hull() {
            translate(pts[i])     sphere(d = d, $fn = 12);
            translate(pts[i + 1]) sphere(d = d, $fn = 12);
        }
}

// Paint a whole region/feature: wrap the feature's own geometry (or a slab
// over the area) so the user can answer "is this a real feature or an
// artefact?" Slightly grown so it shows through the grey body.
module pp_mark_region(n, grow = 0.15) {
    color(pp_mark_color(n), 0.85) minkowski() {
        children();
        cube(2 * grow, center = true);
    }
}

// Billboarded label at p. Default text is the item number; pass `s` for a
// word ("TAB", "COVE") when marking scope rather than asking for a number.
module pp_mark_tag(n, p, s = undef, size = 7) {
    color(pp_mark_color(n)) translate(p)
        rotate([0, 0, $cam_rz]) rotate([$cam_rx, 0, 0])
            linear_extrude(0.8)
                text(is_undef(s) ? str(n) : s, size = size,
                     halign = "center", valign = "center",
                     font = "Liberation Sans:style=Bold");
}
