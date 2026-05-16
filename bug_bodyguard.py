"""Backward-compatible shim.

VisionGuard is now the :mod:`visionguard` package; the implementation moved
to :mod:`visionguard.core`. This module re-exports it so existing imports
(``import bug_bodyguard``) and ``python bug_bodyguard.py`` keep working.
"""
from visionguard.core import *  # noqa: F401,F403
from visionguard.core import main

if __name__ == "__main__":
    raise SystemExit(main())
