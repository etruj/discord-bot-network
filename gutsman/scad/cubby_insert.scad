// cubby_insert.scad — a parametric divided-tray insert.
//
// Original to BoxBot (no third-party attribution). Two ways to describe the
// compartments:
//   • a regular grid via col_widths[] (X) and row_lengths[] (Y), or
//   • an explicit `pockets` list of [x, y, w, l] cells (interior-local, with
//     0,0 at the inside-wall corner) — which lets cubby.py lay out ragged /
//     nested arrangements (odd counts, varying sizes) that a col×row grid can't.
// When `pockets` is non-empty it wins, and the caller passes the tray's outer
// inner_w/inner_l directly; otherwise the outer size is derived from the grid.
//
// Driven entirely by -D overrides from cubby.py — nothing here knows which box
// it goes into.

/* [Grid (used when pockets is empty)] */
col_widths = [60];
row_lengths = [40, 20];
divider_thickness = 1.5;

/* [Explicit pockets (override)] */
// Each entry is [x, y, w, l] in interior-local mm (0,0 = inside-wall corner).
pockets = [];
// Tray outer size, required in pockets mode.
inner_w = 0;
inner_l = 0;

/* [Tray] */
insert_height = 41.5;
corner_radius = 4.85;
chamfer = 2.5;
wall_thickness = 2;
floor_thickness = 1.5;

// --- internal ---
$fn = 48;

use_pockets = len(pockets) > 0;

// Sum of the first n entries of a vector (recursive — OpenSCAD has no cumsum).
function csum(v, n) = n <= 0 ? 0 : v[n - 1] + csum(v, n - 1);

grid_span_w = csum(col_widths, len(col_widths))
              + (len(col_widths) - 1) * divider_thickness;
grid_span_l = csum(row_lengths, len(row_lengths))
              + (len(row_lengths) - 1) * divider_thickness;

outer_w = use_pockets ? inner_w : grid_span_w + 2 * wall_thickness;
outer_l = use_pockets ? inner_l : grid_span_l + 2 * wall_thickness;
span_w = outer_w - 2 * wall_thickness;
span_l = outer_l - 2 * wall_thickness;
pocket_h = insert_height - floor_thickness;      // open through the top
inner_radius = max(0, corner_radius - wall_thickness);

// Lower-left corner of grid cell (i, j), inside the tray walls.
function pos_x(i) = csum(col_widths, i) + i * divider_thickness;
function pos_y(j) = csum(row_lengths, j) + j * divider_thickness;

// A box with rounded vertical (Z) edges.
module rounded_box(w, l, h, r) {
    if (r > 0) {
        translate([r, r, 0])
            linear_extrude(height = h)
                offset(r = r)
                    square([w - 2 * r, l - 2 * r]);
    } else {
        cube([w, l, h]);
    }
}

// One compartment pocket with a 45-degree scoop ramp at the bottom.
module chamfered_pocket(w, l, h, cb) {
    if (cb > 0 && w > 2 * cb && l > 2 * cb) {
        hull() {
            translate([0, 0, cb]) cube([w, l, h - cb]);
            translate([cb, cb, 0]) cube([w - 2 * cb, l - 2 * cb, 0.1]);
        }
    } else {
        cube([w, l, h]);   // fallback when the chamfer won't fit the cell
    }
}

// Every compartment pocket, positioned inside the tray walls.
module all_pockets() {
    if (use_pockets) {
        for (p = pockets)
            translate([wall_thickness + p[0], wall_thickness + p[1], floor_thickness])
                chamfered_pocket(p[2], p[3], pocket_h + 1, chamfer);
    } else {
        for (i = [0 : len(col_widths) - 1])
            for (j = [0 : len(row_lengths) - 1])
                translate([wall_thickness + pos_x(i), wall_thickness + pos_y(j),
                           floor_thickness])
                    chamfered_pocket(col_widths[i], row_lengths[j],
                                     pocket_h + 1, chamfer);
    }
}

module cubby_insert() {
    difference() {
        rounded_box(outer_w, outer_l, insert_height, corner_radius);
        // Mask the pockets to the rounded inner cavity so corners stay clean
        // and the outer wall is never breached.
        intersection() {
            translate([wall_thickness, wall_thickness, floor_thickness])
                rounded_box(span_w, span_l, pocket_h + 1, inner_radius);
            all_pockets();
        }
    }
}

cubby_insert();
