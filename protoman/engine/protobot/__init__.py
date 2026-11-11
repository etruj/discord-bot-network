"""protobot — turn an image of a shape + a few measurements into a
print-ready extruded part (OpenSCAD source + binary STL).

Two options (kept as separate tracers so they don't affect each other):
  sketch_to_part  a drawn tracing / silhouette
  photo_to_part   a photo of the real object lying on paper
"""
from .pipeline import (Result, history, photo_to_part, revert_part, revise_part,
                       sketch_to_part, undo_part)
from .refine import ConflictError
from . import ui  # noqa: E402  (bot menus)

__all__ = ["sketch_to_part", "photo_to_part", "revise_part", "revert_part", "undo_part",
           "history", "Result", "ConflictError"]
