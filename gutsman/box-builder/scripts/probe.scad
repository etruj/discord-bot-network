/*
 * probe.scad — ask the rugged-box engine which optional features it will
 * actually build for a given set of options, WITHOUT rendering geometry.
 *
 * The engine gates the handle and label behind internal predicates
 * (_handle_enabled / _label_enabled) whose thresholds depend on several
 * derived values. Re-implementing that math in Python would silently drift
 * from the engine, so we evaluate it in OpenSCAD and echo the answers:
 *
 *   openscad -o /dev/null -D Width=160 ... probe.scad
 *   -> ECHO: "PROBE latch_count=2 handle=true label=false"
 *
 * Run with --export-format echo (or any -o) and parse the PROBE line.
 */
include <rugged-box-library.scad>;

Width = 180;
Length = 90;
Bottom_Height = 40;
Top_Height = 20;
Corner_Radius = 5;
Edge_Chamfer_Proportion = 0.4;
Lip_Seal_Type = "wedge";
Reinforced_Corners = false;
Latch_Type = "clip";
Latch_Count = 0;
Top_Grip = false;
Hinge_End_Stops = false;
Handle = false;
Label = false;
Label_Text = "Label";
Label_Text_Size = 10;
Wall_Thickness = 3.0;
Lip_Thickness = 3.0;
Rib_Width = 6;
Latch_Width = 28;
Latch_Screw_Separation = 20;
Third_Hinge_Width = 0;
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
// _box_part_setup() establishes $b_part and the remaining derived values the
// predicates read; "bottom" is the part that carries the handle and label.
rbox_for_bottom()
echo(str(
    "PROBE latch_count=", _compute_latch_count(),
    " handle=", _handle_enabled(),
    " label=", _label_enabled(),
    " outer_width=", $b_outer_width,
    " outer_length=", $b_outer_length
));
