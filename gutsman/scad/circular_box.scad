// circular_box.scad — a round box with a press-fit lip lid or a screw-down lid.
//
// Written from scratch for BoxBot (both earlier drafts are rectangular, so
// unlike sliding_box.scad and project_box.scad there is no vendored ancestor
// here and no third-party licence to carry). DRIVEN BY box_types/circular.py:
// the wizard asks the *interior* diameter and depth and passes the derived
// outer_dia / body_height via -D overrides — the same inversion pattern as
// sliding_box.scad and project_box.scad.
//
// Two lid styles in v1:
//   "lip"   — a ring on the underside of the lid drops into the bore and grips
//             by friction (the classic round-box / pill-tin closure).
//   "screw" — a flat lid held by screw ears. The ears sit OUTSIDE the wall and
//             are trimmed back to the bore, so the interior stays a clean
//             cylinder at the diameter the user asked for; only the outer
//             envelope grows, and only at the ear positions.
//
// Deliberately deferred (see ROADMAP Phase 3 / LOG.md): threaded lids and a
// hinged round lid. Both are a different class of geometry — threads need a
// profile sweep, a round hinge needs a tangent knuckle — and neither is needed
// for a first useful round box, the same v1 reasoning that dropped
// project_box.scad's recessed rabbet.

/* [Outer Dimensions] */
outer_dia   = 86;   // outer diameter
body_height = 48;   // floor to top of wall (the lid sits above this)

/* [Wall / Floor / Lid] */
wall_thickness  = 2.4;
floor_thickness = 2.0;
lid_thickness   = 2.4;
tolerance       = 0.25; // fit gap on the lip, per side

/* [Lid] */
lid_style  = "lip"; // [lip:Press-fit lip, screw:Screw-down with ears]
lip_height = 6;     // how far the lip descends into the bore
lip_wall   = 1.6;   // thickness of the lip ring

/* [Screw ears — lid_style = "screw" only] */
screw_count     = 3;   // ears spaced evenly around the box
screw_post_dia  = 8;   // ear/boss outer diameter
hole_dia_lid    = 3.4; // clearance hole through the lid
hole_dia_thread = 2.5; // pilot hole in the ear (M3 self-tapping into PLA)
hole_deepness   = 10;  // pilot depth from the top of the ear
// Head style for the screw sitting in the lid: 0 hole only, 1 countersink, 2 pan head
hole_type    = 1;
cs_head_dia  = 5.5;
ns_head_dia  = 6.0;
ns_head_high = 2.5;

/* [Display Options] */
render_part = "both"; // [both:Both (Side by Side), box:Box Only, lid:Lid Only, assembled:Assembled Preview]

// --- INTERNAL CALCULATION LOGIC ---
$fn = 120;                 // smooth enough that the bore prints round
inner_dia = outer_dia - 2 * wall_thickness;
outer_r   = outer_dia / 2;

// How far each ear is pulled back into the wall so it fuses solidly instead of
// touching it along a single tangent line. 0.35 of the boss diameter leaves a
// generous weld while keeping the pilot hole clear of the bore — the ear's
// inner edge pokes slightly past the bore wall and is trimmed off by the same
// difference() that cuts the cavity, so the interior stays perfectly round.
EAR_MERGE = 0.35;
ear_r = outer_r + screw_post_dia / 2 - screw_post_dia * EAR_MERGE;

is_screw = (lid_style == "screw");
is_lip   = (lid_style == "lip");

// Side-by-side spacing for the "both" parts view.
PART_GAP = 12;


// --- HELPER MODULES ---

// Place children at each ear position, evenly spaced around the box.
module ear_positions() {
    for (i = [0 : screw_count - 1])
        rotate([0, 0, i * 360 / screw_count])
            translate([ear_r, 0, 0])
                children();
}

module counter_sink(head_dia, hole_dia, depth) {
    // Cone opening out to the head diameter at the top face, so a countersunk
    // screw finishes flush with the lid.
    cylinder(h = depth, d1 = hole_dia, d2 = head_dia);
}

module pan_head_recess(head_dia, head_high) {
    cylinder(h = head_high + 0.5, d = head_dia);
}


// --- MAIN MODEL GENERATION ---

module body() {
    difference() {
        union() {
            cylinder(h = body_height, d = outer_dia);
            if (is_screw)
                ear_positions()
                    cylinder(h = body_height, d = screw_post_dia);
        }
        // The cavity. Cut after the ears are unioned on, which is what trims
        // any ear material that strayed inside the bore.
        translate([0, 0, floor_thickness])
            cylinder(h = body_height + 1, d = inner_dia);
        // Pilot holes, drilled down from the top of each ear.
        if (is_screw)
            ear_positions()
                translate([0, 0, body_height - hole_deepness])
                    cylinder(h = hole_deepness + 1, d = hole_dia_thread);
    }
}

// The lid disc spans z = [0, lid_thickness]. For the lip style the ring hangs
// BELOW z=0, so the assembled view can simply drop it at z = body_height and
// the lip lands inside the bore. The print views lift it back onto the bed.
module lid() {
    difference() {
        union() {
            cylinder(h = lid_thickness, d = outer_dia);
            if (is_screw)
                ear_positions()
                    cylinder(h = lid_thickness, d = screw_post_dia);
            if (is_lip)
                translate([0, 0, -lip_height])
                    difference() {
                        cylinder(h = lip_height + 0.01,
                                 d = inner_dia - 2 * tolerance);
                        translate([0, 0, -0.5])
                            cylinder(h = lip_height + 1.5,
                                     d = inner_dia - 2 * tolerance - 2 * lip_wall);
                    }
        }
        if (is_screw)
            ear_positions() {
                translate([0, 0, -0.5])
                    cylinder(h = lid_thickness + 1, d = hole_dia_lid);
                if (hole_type == 1)
                    translate([0, 0, lid_thickness - hole_deepness / 4])
                        counter_sink(cs_head_dia, hole_dia_lid,
                                     hole_deepness / 4 + 0.5);
                if (hole_type == 2)
                    translate([0, 0, lid_thickness - ns_head_high])
                        pan_head_recess(ns_head_dia, ns_head_high);
            }
    }
}

// The lid as it should sit on the print bed.
//
// A lip lid has to be FLIPPED: lid() models the lip hanging below the disc (so
// the assembled view can just drop it into the bore), but printing it that way
// puts the thin ring on the bed and asks the disc to bridge the whole bore in
// mid-air. Turned over, the solid disc is the first layer and the ring grows
// upward off it — no supports.
//
// A screw lid is left alone: it is already flat on the bed, and flipping it
// would point the countersinks at the bed, turning each one into an overhang
// instead of a hole that simply widens as it rises.
module lid_printed() {
    if (is_lip)
        translate([0, 0, lid_thickness]) rotate([180, 0, 0]) lid();
    else
        lid();
}

// Preview colours, matching render.COLORS ("body" / "lid"). A closed round box
// is an otherwise featureless cylinder — with a flush lid there is no visible
// seam the way there is on the rectangular types — so the preview has to
// colour the two parts or it tells the user nothing. Ignored by STL export.
BODY_COLOR = "#e07a2f";
LID_COLOR  = "#7ab8f0";


// --- FINAL RENDER SELECTION ---

if (render_part == "box") {
    body();
} else if (render_part == "lid") {
    lid_printed();
} else if (render_part == "assembled") {
    // Closed, as it ends up on the desk — lid the right way up, lip in the bore.
    color(BODY_COLOR) body();
    color(LID_COLOR) translate([0, 0, body_height]) lid();
} else {
    // "both" — side-by-side, each part as it goes on the bed.
    color(BODY_COLOR) body();
    color(LID_COLOR) translate([outer_dia + PART_GAP, 0, 0]) lid_printed();
}
