// ============================================================================
//  example.scad  —  minimal two-part dovetail sandwich using lib.scad
//  Demonstrates: rounded body w/ window, side dovetail groove+tongue with
//  lead-ins, validated clearance, and a discreet engraved version mark.
//  Render: openscad --export-format binstl -o front.stl  (wrap front_part())
// ============================================================================
use <lib.scad>
$fn = 48;

// ---- parameters ----
W = 50; H = 70;                 // outer footprint
FT = 1.4; BT = 0.8;             // front / back thickness
WIN_W = 30; WIN_H = 46;         // window opening

// ---- VALIDATED JOINT — do not change once a fit is confirmed ----
G_NARROW = 2.6; G_WIDE = 4.0; G_DEPTH = 0.7;   // dovetail groove
// clearance lives in lib (PP_FIT_CLEAR = 0.10)
XC = W/2 - 3;                   // rail centerline (left/right)

VER = "v1";

module front_part() {
    difference() {
        pp_rounded_body(FT)
            difference() { pp_rrect(W, H, 3); pp_rrect(WIN_W, WIN_H, 2); }
        pp_dovetail_y(-H/2-1, H/2+1,  XC, G_NARROW, G_WIDE, G_DEPTH);
        pp_dovetail_y(-H/2-1, H/2+1, -XC, G_NARROW, G_WIDE, G_DEPTH);
    }
}

module back_part() {
    difference() {
        union() {
            pp_rounded_body(BT) pp_rrect(W, H, 3);
            translate([0,0,BT]) pp_tongue_y(-H/2, H/2,  XC, G_NARROW, G_WIDE, G_DEPTH);
            translate([0,0,BT]) pp_tongue_y(-H/2, H/2, -XC, G_NARROW, G_WIDE, G_DEPTH);
        }
        // discreet version on the inner (mating) face — hidden in use, on solid material
        pp_engrave(VER, 0, -H/2 + 6, BT);
    }
}

// layout for preview
translate([0,  H/2 + 5, 0]) front_part();
translate([0, -H/2 - 5, 0]) back_part();
