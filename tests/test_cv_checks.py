"""Tests for the CV AST static checks (CVASTVisitor)."""
import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bug_bodyguard as bg  # noqa: E402


def analyze(src: str) -> list[bg.Issue]:
    visitor = bg.CVASTVisitor(pathlib.Path("sample.py"), pathlib.Path("."))
    visitor.visit(ast.parse(src))
    return visitor.issues


def titles(src: str) -> list[str]:
    return [i.title for i in analyze(src)]


class TestResourceLeaks(unittest.TestCase):
    def test_videocapture_not_released(self) -> None:
        src = (
            "import cv2\n"
            "def grab():\n"
            "    cap = cv2.VideoCapture(0)\n"
            "    return cap.read()\n"
        )
        self.assertTrue(any("never released" in t for t in titles(src)))

    def test_videocapture_released_is_clean(self) -> None:
        src = (
            "import cv2\n"
            "def grab():\n"
            "    cap = cv2.VideoCapture(0)\n"
            "    if cap.isOpened():\n"
            "        cap.read()\n"
            "    cap.release()\n"
        )
        self.assertFalse(any("never released" in t for t in titles(src)))

    def test_realsense_pipeline_not_stopped(self) -> None:
        src = (
            "import pyrealsense2 as rs\n"
            "def stream():\n"
            "    pipeline = rs.pipeline()\n"
            "    pipeline.start()\n"
            "    return pipeline\n"
        )
        self.assertTrue(any("never stopped" in t for t in titles(src)))

    def test_realsense_pipeline_stopped_is_clean(self) -> None:
        src = (
            "import pyrealsense2 as rs\n"
            "def stream():\n"
            "    pipeline = rs.pipeline()\n"
            "    pipeline.start()\n"
            "    pipeline.stop()\n"
        )
        self.assertFalse(any("never stopped" in t for t in titles(src)))

    def test_thread_start_does_not_false_trigger_pipeline_leak(self) -> None:
        src = (
            "import threading\n"
            "def go():\n"
            "    t = threading.Thread(target=work)\n"
            "    t.start()\n"
        )
        self.assertFalse(any("never stopped" in t for t in titles(src)))


class TestDiscardedTransform(unittest.TestCase):
    def test_discarded_to_device(self) -> None:
        src = "def f(x):\n    x.to('cuda')\n    return x\n"
        self.assertTrue(any("result discarded" in t for t in titles(src)))

    def test_assigned_transform_is_clean(self) -> None:
        src = "def f(x):\n    x = x.to('cuda')\n    return x\n"
        self.assertFalse(any("result discarded" in t for t in titles(src)))


class TestExistingChecks(unittest.TestCase):
    def test_torch_load_without_map_location(self) -> None:
        src = "import torch\nm = torch.load('w.pt')\n"
        self.assertTrue(any("map_location" in t for t in titles(src)))


if __name__ == "__main__":
    unittest.main()
