/*
 * boxgen.scad — thin driveable wrapper around smkent/monoscad's rugged-box
 * engine (CC BY-SA 4.0). Every knob is a top-level variable so the Python
 * wizard can override it with `openscad -D name=value`. This exposes options
 * the stock rugged-box.scad customizer hides — notably latch_count.
 *
 * The engine library is resolved via OPENSCADPATH (set by the wizard to the
 * vendored tools/monoscad/rugged-box directory).
 */
include <rugged-box-library.scad>;

/* [Part] */
Part = "bottom";  // bottom | top | latch | stacking_latch | handle | label | assembled_open | assembled_closed

/* [Dimensions] */
Width = 180;          // interior side-to-side (mm)
Length = 90;          // interior front-to-back (mm)
Bottom_Height = 40;   // interior bottom height (mm)
Top_Height = 20;      // interior top height (mm)
Corner_Radius = 5;
Edge_Chamfer_Proportion = 0.4;

/* [Features] */
Lip_Seal_Type = "wedge";     // none | wedge | square | filament-1.75mm
Reinforced_Corners = false;
Latch_Type = "clip";          // clip | draw
Latch_Count = 0;              // 0 = auto, else 1 or 2   (== "number of clasps")
Top_Grip = false;
Hinge_End_Stops = false;
Handle = false;
Label = false;
Label_Text = "Label";
Label_Text_Size = 10;

/* [Advanced] */
Wall_Thickness = 3.0;
Lip_Thickness = 3.0;
Rib_Width = 6;
Latch_Width = 28;
Latch_Screw_Separation = 20;
Third_Hinge_Width = 0;        // add a center hinge if interior width >= this (0 = off)
Stacking_Separation = 0;
Size_Tolerance = 0.20;

rbox(
    Width, Length, Bottom_Height, Top_Height,
    corner_radius=Corner_Radius,
    edge_chamfer_proportion=Edge_Chamfer_Proportion,
    lip_seal_type=Lip_Seal_Type,
    reinforced_corners=Reinforced_Corners,
    latch_type=Latch_Type,
    latch_count=Latch_Count,
    top_grip=Top_Grip,
    hinge_end_stops=Hinge_End_Stops,
    handle=Handle,
    label=Label,
    label_text=Label_Text,
    label_text_size=Label_Text_Size
)
rbox_size_adjustments(
    wall_thickness=Wall_Thickness,
    lip_thickness=Lip_Thickness,
    rib_width=Rib_Width,
    latch_width=Latch_Width,
    latch_screw_separation=Latch_Screw_Separation,
    third_hinge_width=Third_Hinge_Width,
    stacking_separation=Stacking_Separation,
    size_tolerance=Size_Tolerance
)
rbox_part(Part);
