"""workspace.py — locate the workspace root directory.

The workspace root is the parent of ``colcon_ws``.  We walk up from any
caller's ``__file__`` (or, as a convenience, from *this* file) until we find
a directory named ``colcon_ws`` and return its parent.

Usage::

    from common.workspace import WORKSPACE_ROOT, TMP_DIR
"""

from pathlib import Path


def get_workspace_root() -> Path:
    """Return the workspace root (parent of ``colcon_ws``).

    Walks up from this file's location, which works both from the source
    tree and the colcon install tree.
    """
    p = Path(__file__).resolve()
    for ancestor in p.parents:
        if ancestor.name == 'colcon_ws':
            return ancestor.parent
    # Fallback — should never happen in normal usage.
    return p.parents[4]


WORKSPACE_ROOT: Path = get_workspace_root()
"""Absolute path to the workspace root (parent of ``colcon_ws``)."""

TMP_DIR: Path = WORKSPACE_ROOT / 'tmp'
"""Shared temporary directory for exported files, logs, etc."""
TMP_DIR.mkdir(parents=True, exist_ok=True)
