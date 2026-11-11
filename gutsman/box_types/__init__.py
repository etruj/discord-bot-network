"""Box type registry.

Gutsman registers one slash command per box type, and each command spawns that
type's *own* wizard. Box types are not variations on a shared questionnaire —
a rugged box and a gridfinity tray ask different things in a different order,
so each module owns its conversation outright.

To add a box type: write a module here and append it to TYPES. No change to
gutsman.py is needed.

A box type module must define:

    COMMAND      str   Discord command name. Lowercase, [a-z0-9_-], <=32 chars
                       (Discord rejects anything else).
    TITLE        str   Human name used in prose, e.g. "Rugged Box"
    DESCRIPTION  str   Shown in Discord's command picker (<=100 chars)
    Wizard       class Subclass of wizard_base.BaseWizard implementing
                       `start() -> str` and `async step(text) -> str |
                       (str, [image_paths])`.

Everything else — option schemas, the generator call, the renders — is the
module's own business. wizard_base has shared parsing/formatting helpers if a
wizard wants them, but nothing forces their use.
"""
from . import circular, project, rugged, sliding

TYPES = [rugged, sliding, project, circular]


def by_command(name: str):
    """Look up a box type by its slash command name."""
    for t in TYPES:
        if t.COMMAND == name:
            return t
    return None
