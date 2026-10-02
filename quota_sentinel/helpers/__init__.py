"""Installed helper resources, shared by source and wheel entry points."""
from pathlib import Path

def resource_path(name):
    root=Path(__file__).resolve().parent
    if not isinstance(name,str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("invalid helper resource")
    path=(root/name).resolve()
    try:path.relative_to(root)
    except ValueError:raise ValueError("helper resource escapes the installed package") from None
    if not path.is_file():raise ValueError("helper resource is unavailable")
    return path
