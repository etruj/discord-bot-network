# License for `project_box.scad`

`project_box.scad` is **not** under this repository's MIT license.

It is adapted from **"Easy Project Box" by pbtec** (pb-tec.ch) and is licensed
**Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
(CC BY-NC-SA 4.0)**.

- Full legal text: <https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode>
- Summary: <https://creativecommons.org/licenses/by-nc-sa/4.0/>

What that means here:

- **Attribution.** Keep the credit to pbtec and the notice at the top of the file.
- **NonCommercial.** Do not use the file, or the box models Gutsman's `/project`
  command generates from it, for commercial purposes (for example selling the
  files or the printed boxes).
- **ShareAlike.** If you change the file and share it, share it under the same
  license.

**Changes from the original:** the lid sits flat on the walls and screw posts
(no recessed rabbet), mid-side screw posts are added automatically on long
sides, PCB standoffs and the manual cut hooks were removed, and the outer
dimensions are driven by `box_types/project.py`.

The material is provided as-is, without warranties, as stated in the license.

**Need a commercially usable tree?** Delete `scad/project_box.scad`, this file
and `box_types/project.py`. Gutsman's other box types don't depend on them.
