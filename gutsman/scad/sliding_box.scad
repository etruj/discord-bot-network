// sliding_box.scad — a sliding-lid compartment box with engraved lid.
//
// Working copy of an earlier sliding-box draft. This file is DRIVEN BY box_types/sliding.py: the wizard
// asks for *interior* dimensions and passes the outer box_width/box_length/
// box_height it computes from them via -D overrides. The parameters below are
// only the offline-editing defaults — the bot overrides all of them.

// --- EDITABLE PARAMETERS (OpenSCAD Customizer) ---

/* [Outer Box Dimensions] */
// Total outer width (X axis)
box_width = 60;   
// Total outer length (Y axis)
box_length = 100; 
// Total outer height (Z axis)
box_height = 40;  
// Corner radius for vertical outer edges
box_radius = 8; 

/* [Split Proportion] */
// Where the box design splits (0.75 = 3/4 of the total length)
split_ratio = 0.75; // [0.5:0.05:0.9]

/* [Custom Text Options] */
// Enable custom text engraving on the lid
enable_text = true;
// Text string to engrave (Max 20 characters recommended)
lid_text = "MY TEXT 2026";
// Font size for the text
text_size = 6;

/* [Internal Compartments] */
// Number of compartments along the width (X axis) — used when col_widths is empty
compartments_x = 2; // [1:10]
// Number of compartments along the length (Y axis) — used when row_lengths is empty
compartments_y = 3; // [1:10]
// Explicit usable compartment widths (X). Non-empty overrides compartments_x.
col_widths = [];
// Explicit usable compartment lengths (Y). Non-empty overrides compartments_y.
row_lengths = [];
// Explicit ragged pockets [x, y, w, l] (interior-local). Overrides everything.
pockets = [];
// Thickness of the internal divider walls
divider_thickness = 1.5;
// Chamfer size at the bottom of compartments for easy scooping
compartment_chamfer = 2.5; 
// Safety clearance between divider top and sliding lid
divider_clearance = 0.2;

/* [Wall Thicknesses] */
// Thickness of the outer side walls
wall_thickness = 3;  
// Thickness of the box floor
floor_thickness = 3; 
// Thickness of the inner sliding lid track
lid_thickness = 2.5; 

/* [Sliding Groove Parameters] */
// How deep the groove cuts into the side walls
groove_depth = 1.5;     
// Distance from the top edge of the box to the start of the groove
groove_margin_top = 2; 
// 3D printing tolerance/play for smooth sliding movement
tolerance = 0.3;        

/* [Display Options] */
// Choose what to render for viewing or STL export
render_part = "both"; // [both:Both (Side by Side), box:Box Only, lid:Lid Only, assembled:Assembled Preview]


// --- INTERNAL CALCULATION LOGIC ---
$fn = 60; 
z_groove = box_height - groove_margin_top - lid_thickness;
inner_radius = max(0, box_radius - wall_thickness);
split_y = box_length * split_ratio;

// Total inner cavity dimensions
inner_w = box_width - (wall_thickness * 2);
inner_l = box_length - (wall_thickness * 2);
// Useful divider height (stops just below the lid path)
inner_h = z_groove - floor_thickness - divider_clearance; 

// Compartment grid: driven by explicit col_widths/row_lengths when given
// (custom sizes from the wizard), otherwise an even split into compartments_x/y.
use_cells = len(col_widths) > 0 && len(row_lengths) > 0;
ncols = use_cells ? len(col_widths) : compartments_x;
nrows = use_cells ? len(row_lengths) : compartments_y;
spacing_x = (inner_w - (compartments_x - 1) * divider_thickness) / compartments_x;
spacing_y = (inner_l - (compartments_y - 1) * divider_thickness) / compartments_y;

// Sum of the first n entries of a vector (recursive — OpenSCAD has no cumsum).
function csum(v, n) = n <= 0 ? 0 : v[n - 1] + csum(v, n - 1);
function cell_w(i) = use_cells ? col_widths[i] : spacing_x;
function cell_l(j) = use_cells ? row_lengths[j] : spacing_y;
function pos_x(i) = wall_thickness + i * divider_thickness +
                   (use_cells ? csum(col_widths, i) : i * spacing_x);
function pos_y(j) = wall_thickness + j * divider_thickness +
                   (use_cells ? csum(row_lengths, j) : j * spacing_y);

// Geometric compensations for the lid's rear curves
groove_radius = box_radius > 0 ? (inner_radius + groove_depth) : 0;
lid_back_radius = box_radius > 0 ? max(0, groove_radius - tolerance) : 0;


// --- HELPER MODULES ---

// Generates a cube with rounded vertical edges (Z axis)
module rounded_cube(w, h_dim, h, r) {
    if (r > 0) {
        translate([r, r, 0])
        linear_extrude(height = h)
        offset(r = r)
        square([w - 2*r, h_dim - 2*r]);
    } else {
        cube([w, h_dim, h]);
    }
}

// Generates a square rounded ONLY on the two rear corners (Y=0)
module back_rounded_square(w, l, r) {
    if (r > 0 && l > r) {
        hull() {
            translate([r, r]) circle(r = r);
            translate([w - r, r]) circle(r = r);
            translate([0, l - 0.1]) square([w, 0.1]); // Front corners stay at 90°
        }
    } else {
        square([w, l]);
    }
}

// Generates a single compartment pocket with a 45° bottom scoop ramp
module chamfered_pocket(w, l, h, cb) {
    if (cb > 0 && w > 2*cb && l > 2*cb) {
        hull() {
            // Upper face of the pocket (full size)
            translate([0, 0, cb])
                cube([w, l, h - cb]);
            // Lower face of the pocket (shrunk to form the ramp)
            translate([cb, cb, 0])
                cube([w - 2*cb, l - 2*cb, 0.1]);
        }
    } else {
        cube([w, l, h]); // Fallback if chamfer is too large for the pocket size
    }
}

// Generates the full array of compartment pockets — explicit ragged `pockets`
// win, otherwise the col/row grid (or even split from compartments_x/y).
module grid_of_pockets() {
    if (len(pockets) > 0) {
        for (p = pockets)
            translate([wall_thickness + p[0], wall_thickness + p[1], floor_thickness])
                chamfered_pocket(p[2], p[3], inner_h, compartment_chamfer);
    } else {
        for (i = [0 : ncols - 1])
            for (j = [0 : nrows - 1])
                translate([pos_x(i), pos_y(j), floor_thickness])
                    chamfered_pocket(cell_w(i), cell_l(j), inner_h,
                                     compartment_chamfer);
    }
}


// --- MAIN MODEL GENERATION ---

module box() {
    difference() {
        // 1. Outer main rounded body (fully solid at first)
        rounded_cube(box_width, box_length, box_height, box_radius);
        
        // 2. Subtract the upper open area (above the dividers, where the lid slides)
        translate([wall_thickness, wall_thickness, floor_thickness + inner_h])
            rounded_cube(inner_w, inner_l, box_height + 1, inner_radius);
        
        // 3. Subtract the masked pockets (creates the clean compartments)
        intersection() {
            // Inner rounded cavity mask (keeps corners clean)
            translate([wall_thickness, wall_thickness, floor_thickness])
                rounded_cube(inner_w, inner_l, inner_h + 1, inner_radius);
            
            grid_of_pockets();
        }
        
        // 4. Sliding lid track grooves (runs along the inner sides and back wall)
        translate([wall_thickness - groove_depth, wall_thickness - groove_depth, z_groove - (tolerance / 2)])
            rounded_cube(
                box_width - (wall_thickness * 2) + (groove_depth * 2), 
                box_length * 2, 
                lid_thickness + tolerance,
                groove_radius
            );
            
        // 5. Asymmetric front wall cut at the 3/4 mark
        translate([-1, split_y, z_groove - (tolerance / 2)])
            cube([box_width + 2, box_length - split_y + 2, box_height - z_groove + 1]);
    }
}

module lid_in_place() {
    difference() {
        union() {
            // 1. Main lid plate body (slides between the inner walls, STOPS at the inner back wall)
            translate([wall_thickness + tolerance, wall_thickness + tolerance, z_groove])
                linear_extrude(height = box_height - z_groove)
                    back_rounded_square(
                        box_width - (wall_thickness * 2) - (tolerance * 2), 
                        split_y - wall_thickness, 
                        inner_radius - tolerance
                    );

            // 2. Side guide wings and REAR tongue that lock into the grooves
            translate([wall_thickness - groove_depth + tolerance, wall_thickness - groove_depth + tolerance, z_groove])
                linear_extrude(height = lid_thickness)
                    back_rounded_square(
                        box_width - (wall_thickness * 2) + (groove_depth * 2) - (tolerance * 2), 
                        split_y - (wall_thickness - groove_depth), 
                        lid_back_radius
                    );

            // 3. Thick front block (matches the outer box contour and height)
            intersection() {
                rounded_cube(box_width, box_length, box_height, box_radius);
                translate([-1, split_y + tolerance, z_groove])
                    cube([box_width + 2, box_length - split_y + 2, box_height - z_groove + 1]);
            }
        }
        
        // 4. Recessed ergonomic finger grip handle on top
        translate([box_width / 2, box_length - 15, box_height - 2.5])
            linear_extrude(height = 4)
                offset(r = 2)
                    square([18, 6], center = true);
                    
        // 5. Custom text engraving (Only applies if enable_text is true)
        if (enable_text) {
            // Centered on the visible sliding part
            text_y_center = (split_y + wall_thickness) / 2;

            // Clip the engraving to the sliding lid's own top face so an
            // over-long label can never spill onto the front grip block or off
            // the back edge — a geometric backstop for the wizard's char limit.
            intersection() {
                translate([box_width / 2, text_y_center, box_height - 1])
                    rotate([0, 0, 90]) // Rotated to run along the long side (Y axis)
                        linear_extrude(height = 2)
                            text(lid_text, size = text_size, font = "Liberation Sans:style=Bold", halign = "center", valign = "center");
                // Usable plate area: interior width × [back wall, split].
                translate([wall_thickness, wall_thickness, box_height - 2])
                    cube([box_width - 2 * wall_thickness, split_y - wall_thickness, 4]);
            }
        }
    }
}


// --- FINAL RENDER SELECTION ---

if (render_part == "box") {
    box();
} else if (render_part == "lid") {
    // For single print export: flipped 180° to lie completely flat on Z=0 without supports
    translate([0, 0, box_height]) rotate([180, 0, 0]) lid_in_place();
} else if (render_part == "assembled") {
    box();
    lid_in_place(); 
} else {
    // "both" - Side-by-side view: Box straight, Lid flat on the floor and rotated 180° on Z
    box();
    
    translate([box_width * 2 + 15, box_length, 0]) 
        rotate([0, 0, 180]) 
            lid_in_place();
}