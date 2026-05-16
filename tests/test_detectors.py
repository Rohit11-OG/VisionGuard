"""Golden-input tests for VisionGuard detectors.

Each detector is regex-driven and brittle — these pin down the expected
output for known inputs so a regex tweak can't silently regress detection.
Run: python -m unittest discover -q
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bug_bodyguard as bg  # noqa: E402


def _check(name: str, returncode: int, output: str) -> bg.CheckResult:
    return bg.CheckResult(
        name=name, command=f"{name} ...", returncode=returncode,
        stdout=output, stderr="", duration_seconds=0.1,
    )


class TestRuntimeErrorParsers(unittest.TestCase):
    def setUp(self) -> None:
        self.det = bg.BugDetector()

    def test_attribute_error(self) -> None:
        out = (
            "Traceback (most recent call last):\n"
            '  File "src/camera.py", line 42, in process\n'
            "    frame.get_data()\n"
            "AttributeError: 'NoneType' object has no attribute 'get_data'\n"
        )
        issues = self.det._parse_attribute_errors("unittest", out)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].title, "AttributeError detected")
        self.assertEqual(issues[0].file_path, "src/camera.py")
        self.assertEqual(issues[0].line, 42)
        self.assertEqual(issues[0].severity, "medium")

    def test_import_error(self) -> None:
        out = (
            "Traceback (most recent call last):\n"
            '  File "src/main.py", line 3, in <module>\n'
            "    import torchvisionn\n"
            "ModuleNotFoundError: No module named 'torchvisionn'\n"
        )
        issues = self.det._parse_import_errors("unittest", out)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "high")
        self.assertIn("torchvisionn", issues[0].description)

    def test_zero_division(self) -> None:
        out = (
            "Traceback (most recent call last):\n"
            '  File "src/stats.py", line 9, in mean\n'
            "ZeroDivisionError: division by zero\n"
        )
        issues = self.det._parse_zero_div_errors("unittest", out)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].line, 9)

    def test_clean_output_yields_nothing(self) -> None:
        out = "ok\nall tests passed\n"
        self.assertEqual(self.det._parse_attribute_errors("unittest", out), [])
        self.assertEqual(self.det._parse_type_errors("unittest", out), [])


class TestCVErrorParsers(unittest.TestCase):
    def setUp(self) -> None:
        self.det = bg.BugDetector()

    def test_cuda_oom(self) -> None:
        out = (
            '  File "src/infer.py", line 88, in run\n'
            "RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB\n"
        )
        issues = self.det._parse_cuda_errors("unittest", out)
        self.assertEqual(len(issues), 1)
        self.assertIn("CUDA out of memory", issues[0].description)

    def test_shape_mismatch(self) -> None:
        out = (
            '  File "src/net.py", line 12, in forward\n'
            "RuntimeError: mat1 and mat2 shapes cannot be multiplied (4x5 and 6x7)\n"
        )
        issues = self.det._parse_cv_shape_errors("unittest", out)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "high")


class TestTracebackParser(unittest.TestCase):
    """Regression: a crash inside a check tool must NOT become stdlib 'bugs'."""

    def setUp(self) -> None:
        self.det = bg.BugDetector()

    def test_tool_internal_crash_yields_no_bugs(self) -> None:
        # All frames live in site-packages — semgrep crashing on itself.
        out = (
            "Traceback (most recent call last):\n"
            '  File "C:/Python311/Lib/site-packages/semgrep/cli.py", line 49, in wrapper\n'
            "    func()\n"
            '  File "C:/Python311/Lib/encodings/cp1252.py", line 19, in encode\n'
            "    return codecs.charmap_encode(input)\n"
            "UnicodeEncodeError: 'charmap' codec can't encode character\n"
        )
        self.assertEqual(self.det._parse_tracebacks("semgrep", out), [])

    def test_user_code_crash_is_reported(self) -> None:
        out = (
            "Traceback (most recent call last):\n"
            '  File "src/app.py", line 20, in main\n'
            "    boom()\n"
            '  File "src/app.py", line 5, in boom\n'
            "    raise RuntimeError('bad')\n"
            "RuntimeError: bad\n"
        )
        issues = self.det._parse_tracebacks("unittest", out)
        self.assertTrue(issues)
        self.assertEqual(issues[0].file_path, "src/app.py")

    def test_detect_classifies_tool_crash_as_low_severity(self) -> None:
        out = (
            "Traceback (most recent call last):\n"
            '  File "C:/Python311/Lib/site-packages/semgrep/cli.py", line 49, in wrapper\n'
            "    func()\n"
            "UnicodeEncodeError: 'charmap' codec can't encode character\n"
        )
        issues = bg.BugDetector().detect([_check("semgrep", 2, out)])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "low")
        self.assertIn("tool error", issues[0].title)


class TestLintParser(unittest.TestCase):
    def test_ruff_line_parsed(self) -> None:
        out = "src/foo.py:10:5: F401 `os` imported but unused\n"
        issues = bg.BugDetector()._parse_lint_lines("ruff", out)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].file_path, "src/foo.py")
        self.assertEqual(issues[0].line, 10)


class TestBaseline(unittest.TestCase):
    def test_fingerprint_is_line_independent(self) -> None:
        a = bg.Issue("id-a", "ruff", "F401 unused import", "desc", "low", 0.5,
                      file_path="src/foo.py", line=10)
        b = bg.Issue("id-b", "ruff", "F401 unused import", "desc2", "low", 0.5,
                      file_path="src/foo.py", line=99)
        self.assertEqual(bg.issue_fingerprint(a), bg.issue_fingerprint(b))

    def test_fingerprint_differs_by_file(self) -> None:
        a = bg.Issue("x", "ruff", "F401", "d", "low", 0.5, file_path="a.py")
        b = bg.Issue("y", "ruff", "F401", "d", "low", 0.5, file_path="b.py")
        self.assertNotEqual(bg.issue_fingerprint(a), bg.issue_fingerprint(b))

    def test_store_roundtrip(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / ".agent").mkdir()
            store = bg.BaselineStore(root)
            self.assertEqual(store.load(), set())
            issue = bg.Issue("x", "ruff", "F401", "d", "low", 0.5, file_path="a.py")
            store.save([issue])
            self.assertIn(bg.issue_fingerprint(issue), store.load())


if __name__ == "__main__":
    unittest.main()
