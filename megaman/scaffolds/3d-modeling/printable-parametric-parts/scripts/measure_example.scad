// ============================================================================
//  measure_example.scad  —  a measurement-request render, end to end
//
//  Stand-in for a part reverse-engineered from photos: a block with a hole
//  and a raised lip. Four things are still guesses, so each gets a colour + a
//  number; the reply that ships the PNG carries the matching numbered list.
//
//  Render:  ./render_measure.sh measure_example.scad measure_request.png
// ============================================================================
use <measure_lib.scad>

L = 60;      // [M] long edge, from the 1:1 trace
D = 30;      // [P] depth — photogrammetry, needs calipers
T = 18;      // [P] height — photogrammetry, needs calipers
HOLE_D = 8;  // [P] hole diameter
LIP_H = 3;   // [?] lip height — or is it a moulding artefact?

$fa = 2; $fs = 0.4;

module part() {
    difference() {
        union() {
            cube([L, D, T]);
            translate([0, D - 4, T - 0.01]) cube([L, 4, LIP_H]);
        }
        translate([L/2, D/2, -1]) cylinder(d = HOLE_D, h = T + 2);
    }
}

pp_mark_ghost() part();

// 1 — depth (front edge to back edge)
pp_mark_bar(1, [L + 4, 0, 0], [L + 4, D, 0]);
pp_mark_tag(1, [L + 11, D/2, 0]);

// 2 — overall height, marked at both ends so it reads from either side
for (x = [-4, L + 4]) pp_mark_bar(2, [x, -4, 0], [x, -4, T]);
pp_mark_tag(2, [-11, -4, T/2]);

// 3 — hole diameter: ring sitting just proud of the top face
pp_mark_ring(3, [L/2, D/2, T + 0.5], HOLE_D/2);
pp_mark_tag(3, [L/2 + 14, D/2, T + 5]);

// 4 — the lip: real feature or artefact? Paint the whole thing.
pp_mark_region(4) translate([0, D - 4, T]) cube([L, 4, LIP_H]);
pp_mark_tag(4, [L/2, D + 6, T + 8]);
