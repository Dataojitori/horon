"""Horon Hook Gateway Runner.

Ensures that the horon project root is in sys.path regardless of whether
the CWD is .agents, horon root, or an arbitrary directory.
"""
import sys
from pathlib import Path

_current = Path(__file__).resolve().parent
# Locate the horon project root containing 'backend'
for candidate in [_current, _current.parent, Path.cwd(), Path.cwd().parent]:
    if (candidate / "backend" / "hook_gateway.py").is_file():
        project_root = candidate
        break
else:
    project_root = _current

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.hook_gateway import main

if __name__ == "__main__":
    main()
