"""VisionGuard — deep static + runtime bug detection for CV projects.

The implementation lives in :mod:`visionguard.core`; everything public is
re-exported here so ``import visionguard`` and ``from visionguard import X``
both work.
"""
from visionguard.core import *  # noqa: F401,F403
from visionguard.core import main  # noqa: F401

__version__ = "1.2.0"
