"""Tests for config validation, SARIF output, and the check-result cache."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bug_bodyguard as bg  # noqa: E402


def _valid_cfg() -> dict:
    return {
        "mode": "safe_pr",
        "checks": {"commands": [{"name": "compileall", "command": "python -m compileall ."}]},
    }


class TestConfigValidation(unittest.TestCase):
    def test_valid_config_passes(self) -> None:
        bg.validate_config(_valid_cfg())  # must not raise

    def test_bad_mode_rejected(self) -> None:
        cfg = _valid_cfg()
        cfg["mode"] = "turbo"
        with self.assertRaises(bg.ConfigError):
            bg.validate_config(cfg)

    def test_empty_commands_rejected(self) -> None:
        cfg = _valid_cfg()
        cfg["checks"]["commands"] = []
        with self.assertRaises(bg.ConfigError):
            bg.validate_config(cfg)

    def test_duplicate_check_name_rejected(self) -> None:
        cfg = _valid_cfg()
        cfg["checks"]["commands"].append({"name": "compileall", "command": "x"})
        with self.assertRaises(bg.ConfigError):
            bg.validate_config(cfg)

    def test_bad_timeout_rejected(self) -> None:
        cfg = _valid_cfg()
        cfg["checks"]["timeout_seconds"] = -5
        with self.assertRaises(bg.ConfigError):
            bg.validate_config(cfg)

    def test_bad_confidence_rejected(self) -> None:
        cfg = _valid_cfg()
        cfg["notifications"] = {"min_confidence": 1.7}
        with self.assertRaises(bg.ConfigError):
            bg.validate_config(cfg)


class TestSarif(unittest.TestCase):
    def test_sarif_shape(self) -> None:
        issues = [
            bg.Issue("a", "ruff", "Unused import", "os unused", "low", 0.6,
                     file_path="src/x.py", line=3),
            bg.Issue("b", "unittest", "Crash", "boom", "high", 0.9,
                     file_path="src/y.py", line=10),
        ]
        doc = bg.build_sarif(issues)
        self.assertEqual(doc["version"], "2.1.0")
        results = doc["runs"][0]["results"]
        self.assertEqual(len(results), 2)
        levels = {r["level"] for r in results}
        self.assertEqual(levels, {"note", "error"})
        loc = results[0]["locations"][0]["physicalLocation"]
        self.assertEqual(loc["artifactLocation"]["uri"], "src/x.py")
        self.assertEqual(loc["region"]["startLine"], 3)

    def test_sarif_is_json_serializable(self) -> None:
        json.dumps(bg.build_sarif([]))  # must not raise


class TestProjectFingerprint(unittest.TestCase):
    def test_fingerprint_changes_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = {"watch": {"ignore": []}, "checks": {}}
            runner = bg.CheckRunner(root, cfg)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            fp1 = runner._project_fingerprint()
            (root / "a.py").write_text("x = 2\ny = 3\n", encoding="utf-8")
            fp2 = runner._project_fingerprint()
            self.assertNotEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()
