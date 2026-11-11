// project_box.scad — a screw-close project/electronics box.
//
// SPDX-License-Identifier: CC-BY-NC-SA-4.0
//
// LICENSE NOTICE: this file is NOT covered by the repository's MIT license.
// It is adapted from "Easy Project Box" by pbtec (pb-tec.ch), licensed
// Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International:
// https://creativecommons.org/licenses/by-nc-sa/4.0/
//   - Attribution: keep this notice and the credit to pbtec.
//   - NonCommercial: no commercial use of this file or of boxes made with it.
//   - ShareAlike: changes to this file must be shared under the same license.
// Changes from the original are described below. See project_box.LICENSE.md.
//
// Working copy adapted from the original pbtec draft projectboxv.scad (not
// shipped here). This file is DRIVEN BY
// box_types/project.py: the wizard asks *interior* dimensions and passes the
// outer box_width/box_length/box_height it derives via -D overrides, the same
// inversion pattern as sliding_box.scad.
//
// Simplified from the draft: the lid sits flat on top of the walls and posts
// (no recessed rabbet), held down by 4 corner screw posts (plus 2 more per
// long side once that side exceeds MID_SCREW_SPAN, decided automatically —
// see box_types/project.py). This drops a whole class of geometry (the
// draft's lid-cut ledge) for a simpler, more robust model; PCB standoffs and
// the draft's manual cut hooks are out of scope for v1.

/* [Outer Box Dimensions] */
box_width     = 106;   // outer width (X)
box_length    = 146;   // outer length (Y)
box_height    = 43;    // body height, floor to top of walls (lid sits above this)
corner_radius = 5;     // outer corner radius

/* [Wall / Lid] */
wall_thickness = 3;    // side wall AND floor thickness
lid_thickness  = 3;
tolerance      = 0.2;  // lid inset from the outer edge, per side

/* [Screw bosses] */
hole_dia_lid    = 3.4; // clearance hole through the lid for the screw shaft
hole_dia_thread = 2.5; // pilot hole in the boss (self-tapping into PLA, M3)
hole_deepness   = 12;  // pilot hole depth into the boss from its top
screw_post_dia  = 8;   // boss outer diameter
hole_pos        = 7;   // boss centre, inset from each outer wall face
add_x_mid       = false; // extra mid-span bosses along the length-direction walls
add_y_mid       = false; // extra mid-span bosses along the width-direction walls
// Head style for the screw sitting in the lid: 0 hole only, 1 countersink, 2 pan/round head
hole_type    = 1;
cs_head_dia  = 5.5;   // M3 countersink head diameter
ns_head_dia  = 6.0;   // M3 pan head diameter
ns_head_high = 2.5;   // pan head height

/* [Display Options] */
render_part = "both"; // [both:Both (Side by Side), box:Box Only, lid:Lid Only, assembled:Assembled Preview]

// --- INTERNAL CALCULATION LOGIC ---
$fn = 60;
inner_radius = max(0, corner_radius - wall_thickness);
inner_w = box_width - 2 * wall_thickness;
inner_l = box_length - 2 * wall_thickness;

// Rounding must be >= wall_thickness for the corner-post offset math below to
// stay valid (a smaller rounding than the wall is clamped, matching the
// draft's own guard against rendering issues at that combination).
effective_radius = max(corner_radius, wall_thickness);
hole_pos_corner = (corner_radius <= wall_thickness) ? hole_pos :
    (sqrt(2) * effective_radius - effective_radius) / sqrt(2) + hole_pos / sqrt(2);

// Corner boss centres, plus optional mid-span bosses on the long walls.
corner_positions = [
    for (x = [hole_pos_corner, box_width - hole_pos_corner])
    for (y = [hole_pos_corner, box_length - hole_pos_corner])
    [x, y]
];
mid_positions = concat(
    add_x_mid ? [[box_width / 2, hole_pos], [box_width / 2, box_length - hole_pos]] : [],
    add_y_mid ? [[hole_pos, box_length / 2], [box_width - hole_pos, box_length / 2]] : []
);
post_positions = concat(corner_positions, mid_positions);


// --- HELPER MODULES ---

// A rectangle with rounded vertical edges, extruded from z=0 to z=h. Corner at
// the origin (footprint spans [0,w] x [0,d]) — same convention as sliding_box.scad.
module rounded_cube(w, d, h, r) {
    if (r > 0) {
        translate([r, r, 0])
            linear_extrude(height = h)
                offset(r = r)
                    square([w - 2 * r, d - 2 * r]);
    } else {
        cube([w, d, h]);
    }
}

module counter_sink(head_dia, hole_dia, depth) {
    // Cone tapering from the shaft hole up to the head diameter at the top
    // surface, so a countersunk screw sits flush.
    cylinder(h = depth, d1 = hole_dia, d2 = head_dia);
}

module pan_head_recess(head_dia, head_high) {
    cylinder(h = head_high + 0.5, d = head_dia);
}


// --- MAIN MODEL GENERATION ---

module box() {
    difference() {
        union() {
            difference() {
                rounded_cube(box_width, box_length, box_height, corner_radius);
                // Open-top interior cavity.
                translate([wall_thickness, wall_thickness, wall_thickness])
                    rounded_cube(inner_w, inner_l, box_height + 1, inner_radius);
            }
            // Screw posts, flush with the top of the walls so the lid rests on
            // both together.
            for (p = post_positions)
                translate([p[0], p[1], 0])
                    cylinder(h = box_height, d = screw_post_dia);
        }
        // Pilot holes, drilled down from the top of each post.
        for (p = post_positions)
            translate([p[0], p[1], box_height - hole_deepness])
                cylinder(h = hole_deepness + 1, d = hole_dia_thread);
    }
}

module lid() {
    lw = box_width - 2 * tolerance;
    ll = box_length - 2 * tolerance;
    lr = max(0, corner_radius - tolerance);
    difference() {
        translate([tolerance, tolerance, 0])
            rounded_cube(lw, ll, lid_thickness, lr);
        for (p = post_positions) {
            translate([p[0], p[1], -0.5])
                cylinder(h = lid_thickness + 1, d = hole_dia_lid);
            if (hole_type == 1)
                translate([p[0], p[1], lid_thickness - hole_deepness / 4])
                    counter_sink(cs_head_dia, hole_dia_lid, hole_deepness / 4 + 0.5);
            if (hole_type == 2)
                translate([p[0], p[1], lid_thickness - ns_head_high])
                    pan_head_recess(ns_head_dia, ns_head_high);
        }
    }
}


// --- FINAL RENDER SELECTION ---

if (render_part == "box") {
    box();
} else if (render_part == "lid") {
    lid();
} else if (render_part == "assembled") {
    box();
    translate([0, 0, box_height]) lid();
} else {
    // "both" — side-by-side view for the parts render.
    box();
    translate([box_width + 15, 0, 0]) lid();
}
