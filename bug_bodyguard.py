#!/usr/bin/env python3
"""Bug Bodyguard: safe, drop-in bug hunting agent for Python repositories."""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import os
import pathlib
import queue
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import traceback
import uuid
from typing import Any, Iterable


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "safe_pr",
    "watch": {
        "paths": ["."],
        "ignore": [
            ".git", ".agent", "venv", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache",
            "datasets", "checkpoints", "weights", "runs", "outputs", "logs",
            "vendor", "stl_models", "models", "dataset",
        ],
        "debounce_seconds": 2,
        "poll_seconds": 2,
        "run_initial_scan": True,
    },
    "checks": {
        "commands": [
            {"name": "compileall", "command": "python -m compileall -q ."},
            {
                "name": "pytest",
                "command": "python -m pytest -q --tb=short",
                "optional": True,
                "requires": ["pytest"],
            },
            {"name": "unittest", "command": "python -m unittest discover -q"},
            {"name": "ruff", "command": "python -m ruff check .", "optional": True},
            {"name": "basedpyright", "command": "basedpyright", "optional": True},
        ],
        "timeout_seconds": 120,
        "run_impacted_tests": True,
        "impacted_test_limit": 20,
        "exclude_paths": ["bug_bodyguard.py", ".agent"],
        "basedpyright": {
            "level": "warning",
            "fail_on_warnings": False,
        },
    },
    "fixes": {
        "prefer_libcst": True,
        "ruff_diff_proposals": True,
        "ruff_diff_extra_args": [],
        "ruff_diff_max_proposals": 40,
    },
    "llm": {"provider": "none", "model": "", "api_key_env": "OPENAI_API_KEY"},
    "reporting": {"path": ".agent/reports", "verbosity": "normal"},
    "notifications": {
        "enabled": True,
        "console": True,
        "summary_file": ".agent/last_notification.txt",
        "min_confidence": 0.75,
    },
    "auto_apply": {
        "enabled": False,
        "min_confidence": 0.8,
        "max_patches_per_scan": 1,
        "require_all_checks_pass_after_apply": True,
        "preview_only": False,
    },
    "telemetry": {
        "enabled": True,
        "events_path": ".agent/telemetry/events.jsonl",
    },
}


@dataclasses.dataclass
class CheckResult:
    name: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


@dataclasses.dataclass
class Issue:
    issue_id: str
    source_check: str
    title: str
    description: str
    severity: str
    confidence: float
    file_path: str | None = None
    line: int | None = None
    evidence: str = ""


@dataclasses.dataclass
class PatchProposal:
    proposal_id: str
    issue_id: str
    file_path: str
    summary: str
    confidence: float
    patch: str
    validated: bool = False
    validation_note: str = "Not validated yet."
    auto_applied: bool = False
    apply_note: str = "Not applied."
    proposal_source: str = "heuristic"


def utc_ts() -> str:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).isoformat()


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def load_config(path: pathlib.Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "agent.yml is not valid JSON and PyYAML is unavailable. "
                "Install pyyaml or keep agent.yml in JSON syntax."
            ) from exc
        cfg = yaml.safe_load(raw) or {}
    return merge_defaults(DEFAULT_CONFIG, cfg)


def merge_defaults(defaults: Any, override: Any) -> Any:
    if isinstance(defaults, dict) and isinstance(override, dict):
        merged = dict(defaults)
        for key, value in override.items():
            merged[key] = merge_defaults(merged.get(key), value)
        return merged
    return override if override is not None else defaults


def ensure_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def to_root_relative(path: pathlib.Path, root: pathlib.Path) -> str:
    normalized = path.resolve() if path.is_absolute() else (root / path).resolve()
    return normalized.relative_to(root.resolve()).as_posix()


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def subprocess_text(data: str | bytes | None) -> str:
    """Normalize subprocess stdout/stderr to str (TimeoutExpired may surface bytes in stubs)."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def is_ignored(path: pathlib.Path, ignore_tokens: Iterable[str]) -> bool:
    text = path.as_posix()
    for token in ignore_tokens:
        if not token:
            continue
        token = token.replace("\\", "/")
        if token in text:
            return True
    return False


def collect_python_files(root: pathlib.Path, ignore_tokens: list[str]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for current_root, dirs, names in os.walk(root):
        current = pathlib.Path(current_root)
        dirs[:] = [d for d in dirs if not is_ignored(current / d, ignore_tokens)]
        for name in names:
            candidate = current / name
            if candidate.suffix == ".py" and not is_ignored(candidate, ignore_tokens):
                files.append(candidate)
    return sorted(files)


class RepoIndexer:
    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        self.cfg = cfg
        self.agent_root = root / ".agent"
        self.index_path = self.agent_root / "index" / "index.json"
        self.memory_path = self.agent_root / "memory" / "memory.json"

    def rebuild(self) -> tuple[int, int]:
        ignore_tokens = self.cfg["watch"]["ignore"]
        py_files = collect_python_files(self.root, ignore_tokens)
        index_rows = []
        symbol_rows = {}

        for file_path in py_files:
            rel = file_path.relative_to(self.root).as_posix()
            text = file_path.read_text(encoding="utf-8", errors="replace")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            lines = text.splitlines()
            defs = [line.strip() for line in lines if line.strip().startswith("def ")]
            classes = [line.strip() for line in lines if line.strip().startswith("class ")]

            index_rows.append(
                {
                    "path": rel,
                    "sha256": digest,
                    "line_count": len(lines),
                    "defs": len(defs),
                    "classes": len(classes),
                    "updated_at": utc_ts(),
                }
            )
            symbol_rows[rel] = {
                "functions": defs[:40],
                "classes": classes[:40],
                "summary": summarize_file(lines, defs, classes),
            }

        ensure_dir(self.index_path.parent)
        ensure_dir(self.memory_path.parent)
        self.index_path.write_text(json.dumps(index_rows, indent=2), encoding="utf-8")
        self.memory_path.write_text(json.dumps(symbol_rows, indent=2), encoding="utf-8")
        return len(py_files), len(symbol_rows)


def summarize_file(lines: list[str], defs: list[str], classes: list[str]) -> str:
    parts = [
        f"lines={len(lines)}",
        f"functions={len(defs)}",
        f"classes={len(classes)}",
    ]
    return ", ".join(parts)


class CheckRunner:
    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        self.cfg = cfg

    def run_all(self, changed_files: list[pathlib.Path] | None = None) -> list[CheckResult]:
        changed_files = changed_files or []
        timeout = int(self.cfg["checks"].get("timeout_seconds", 120))
        impacted_tests = infer_impacted_test_modules(
            self.root,
            changed_files,
            int(self.cfg["checks"].get("impacted_test_limit", 20)),
        )
        results: list[CheckResult] = []
        for item in self.cfg["checks"]["commands"]:
            name = item["name"]
            command = self._render_command(name, item["command"], changed_files, impacted_tests)
            command = self._append_tool_excludes(name, command)
            command = self._append_basedpyright_switches(name, command)
            if self._should_skip_optional_check(item, command):
                results.append(
                    CheckResult(
                        name=name,
                        command=command,
                        returncode=0,
                        stdout=f"Skipped optional check '{name}' (dependency not available).",
                        stderr="",
                        duration_seconds=0.0,
                    )
                )
                continue
            start = time.perf_counter()
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(self.root),
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
                result = CheckResult(
                    name=name,
                    command=command,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    duration_seconds=time.perf_counter() - start,
                )
            except subprocess.TimeoutExpired as exc:
                result = CheckResult(
                    name=name,
                    command=command,
                    returncode=124,
                    stdout=subprocess_text(exc.stdout),
                    stderr=subprocess_text(exc.stderr) + f"\nTimed out after {timeout}s",
                    duration_seconds=time.perf_counter() - start,
                )
            results.append(result)
        return results

    def _render_command(
        self,
        name: str,
        command: str,
        changed_files: list[pathlib.Path],
        impacted_tests: list[str],
    ) -> str:
        changed_rel = [to_root_relative(p, self.root) for p in changed_files if p.exists()]
        quoted = " ".join(shlex.quote(p) for p in changed_rel)
        impacted_quoted = " ".join(shlex.quote(m) for m in impacted_tests)
        rendered = (
            command.replace("{changed_files}", quoted).replace("{project_root}", shlex.quote(str(self.root))).strip()
        )
        rendered = rendered.replace("{impacted_tests}", impacted_quoted)

        if (
            name == "unittest"
            and bool(self.cfg["checks"].get("run_impacted_tests", True))
            and impacted_tests
            and "{impacted_tests}" not in command
        ):
            return f"python -m unittest -q {impacted_quoted}"
        return rendered

    def _append_tool_excludes(self, name: str, command: str) -> str:
        paths = list(self.cfg["checks"].get("exclude_paths") or [])
        if not paths:
            return command
        flags = " ".join(f"--exclude {shlex.quote(p)}" for p in paths)
        if name == "ruff":
            return f"{command.rstrip()} {flags}".strip()
        if name == "semgrep" or ("semgrep" in command.lower()):
            cmd_strip = command.rstrip()
            low = cmd_strip.lower()
            if low.startswith("cmd ") and cmd_strip.endswith('"') and cmd_strip.count('"') >= 2:
                return cmd_strip[:-1].rstrip() + " " + flags + '"'
            return f"{cmd_strip} {flags}".strip()
        if name == "bandit":
            joined = ",".join(str(p) for p in paths)
            return f"{command.rstrip()} -x {shlex.quote(joined)}".strip()
        return command

    def _append_basedpyright_switches(self, name: str, command: str) -> str:
        if name != "basedpyright":
            return command
        bp_cfg = self.cfg.get("checks", {}).get("basedpyright") or {}
        extras: list[str] = []
        level = str(bp_cfg.get("level", "warning")).lower()
        if level in ("error", "warning"):
            extras.append(f"--level {level}")
        if bool(bp_cfg.get("fail_on_warnings", False)):
            extras.append("--warnings")
        if not extras:
            return command
        return f"{command.rstrip()} {' '.join(extras)}".strip()

    def _should_skip_optional_check(self, item: dict[str, Any], command: str) -> bool:
        if not bool(item.get("optional", False)):
            return False
        requires = item.get("requires")
        if isinstance(requires, list) and requires:
            for req in requires:
                if not command_exists(str(req)) and not module_exists(str(req)):
                    return True
            return False
        return not command_dependencies_available(command)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def command_dependencies_available(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.strip().split()
    if not tokens:
        return False
    exe = tokens[0]
    if exe in {"python", "py"} and len(tokens) >= 3 and tokens[1] == "-m":
        return module_exists(tokens[2])
    return command_exists(exe)


def infer_impacted_test_modules(root: pathlib.Path, changed_files: list[pathlib.Path], limit: int) -> list[str]:
    modules: set[str] = set()
    for path in changed_files:
        if not path.exists():
            continue
        try:
            rel = to_root_relative(path, root)
        except Exception:
            continue
        rel_path = pathlib.Path(rel)
        if rel_path.suffix != ".py":
            continue
        name = rel_path.name
        parts = rel_path.parts
        if "tests" in parts or name.startswith("test_") or name.endswith("_test.py"):
            modules.add(path_to_module(rel_path))
            continue
        stem = rel_path.stem
        candidates = [
            root / "tests" / f"test_{stem}.py",
            root / "tests" / f"{stem}_test.py",
            rel_path.parent / f"test_{stem}.py",
            rel_path.parent / f"{stem}_test.py",
        ]
        for candidate in candidates:
            candidate_abs = candidate if candidate.is_absolute() else root / candidate
            if candidate_abs.exists():
                modules.add(path_to_module(candidate_abs.relative_to(root)))
    return sorted(m for m in modules if m)[:limit]


def path_to_module(path: pathlib.Path) -> str:
    no_suffix = path.with_suffix("")
    safe_parts = [part.replace("-", "_") for part in no_suffix.parts]
    return ".".join(safe_parts)


def ruff_exclude_argv(cfg: dict[str, Any]) -> list[str]:
    argv: list[str] = []
    for p in cfg.get("checks", {}).get("exclude_paths") or []:
        argv.extend(["--exclude", str(p)])
    return argv


def classify_issue_bucket(file_path: str | None, root: pathlib.Path, cfg: dict[str, Any]) -> str:
    if not file_path:
        return "unknown"
    normalized = file_path.replace("\\", "/")
    agent_markers = ("bug_bodyguard.py", "install_bodyguard_deps.ps1", "run_bodyguard.ps1")
    for marker in agent_markers:
        if normalized.endswith(marker) or f"/{marker}" in normalized or normalized == marker:
            return "agent_dropin"
    for ex in cfg.get("checks", {}).get("exclude_paths") or []:
        exn = str(ex).replace("\\", "/").strip("/")
        if exn and exn in normalized:
            return "tooling_or_cache"
    if ".agent/" in normalized or normalized.startswith(".agent/"):
        return "tooling_or_cache"
    if ".venv/" in normalized or "/venv/" in normalized or "site-packages" in normalized:
        return "dependencies"
    try:
        pathlib.Path(file_path).resolve().relative_to(root.resolve())
    except Exception:
        return "unknown"
    return "app_code"


def split_unified_diff_chunks(blob: str) -> list[tuple[str, str]]:
    """Split combined unified diff into (relative_path, chunk) pairs."""
    blob = blob.strip()
    if not blob or not re.search(r"^--- [ab][/\\]", blob, flags=re.M):
        return []
    parts = re.split(r"(?=^--- [ab][/\\])", blob, flags=re.M)
    out: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if not part.startswith("--- "):
            continue
        rel = ""
        for line in part.splitlines()[:10]:
            m = re.match(r"^\+\+\+ b[/\\](.+)$", line)
            if m:
                rel = m.group(1).strip().split("\t")[0].strip()
                break
            m = re.match(r"^--- a[/\\](.+)$", line)
            if m:
                rel = m.group(1).strip().split("\t")[0].strip()
        if not rel:
            continue
        rel = rel.replace("\\", "/").lstrip("./")
        out.append((rel, part if part.endswith("\n") else part + "\n"))
    return out


def merge_heuristic_and_ruff_proposals(
    heuristic: list[PatchProposal], ruff: list[PatchProposal]
) -> list[PatchProposal]:
    """Keep heuristic proposals first; add Ruff diff previews only for files not already covered."""
    covered: set[str] = set()
    for p in heuristic:
        covered.add(pathlib.Path(p.file_path).as_posix().replace("\\", "/").lstrip("./"))
    out = list(heuristic)
    for p in ruff:
        key = pathlib.Path(p.file_path).as_posix().replace("\\", "/").lstrip("./")
        if key not in covered:
            out.append(p)
            covered.add(key)
    return out


class RuffDiffProposer:
    """Build reviewable patches from `ruff check --fix --diff` (does not write files)."""

    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        self.cfg = cfg

    def build_proposals(self) -> list[PatchProposal]:
        if not self.cfg.get("fixes", {}).get("ruff_diff_proposals", True):
            return []
        if not module_exists("ruff"):
            return []
        timeout = int(self.cfg["checks"].get("timeout_seconds", 120))
        fixes = self.cfg.get("fixes", {})
        cmd_parts: list[str] = [sys.executable, "-m", "ruff", "check", ".", "--fix", "--diff"]
        cmd_parts.extend(ruff_exclude_argv(self.cfg))
        for extra in fixes.get("ruff_diff_extra_args") or []:
            cmd_parts.append(str(extra))
        command = shlex.join(cmd_parts)
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.root),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return []
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if not re.search(r"^--- [ab][/\\]", combined, flags=re.M):
            return []
        proposals: list[PatchProposal] = []
        max_props = int(fixes.get("ruff_diff_max_proposals", 40))
        for rel, chunk in split_unified_diff_chunks(combined):
            if not rel.endswith(".py"):
                continue
            prop_id = f"patch-ruff-{short_hash(chunk)}"
            issue_id = f"ruff-diff-{short_hash(rel + chunk)}"
            proposals.append(
                PatchProposal(
                    proposal_id=prop_id,
                    issue_id=issue_id,
                    file_path=rel,
                    summary="Ruff autofix preview (`ruff check --fix --diff`). Review before apply.",
                    confidence=0.62,
                    patch=chunk,
                    proposal_source="ruff-diff",
                )
            )
            if len(proposals) >= max_props:
                break
        return proposals


class BugDetector:
    LINT_RE = re.compile(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+)?:?\s*(?P<msg>.+)$", re.M)
    SYNTAX_RE = re.compile(
        r'File "(?P<file>.+?)", line (?P<line>\d+)\n(?P<code>.*)\n\s*\^\nSyntaxError:\s*(?P<msg>.+)',
        re.S,
    )
    NAME_ERROR_MSG_RE = re.compile(r"NameError:\s*(?P<msg>.+)")
    TRACEBACK_FILE_RE = re.compile(r'File "(?P<file>.+?)", line (?P<line>\d+), in (?P<func>[^\n]+)')
    ASSERTION_RE = re.compile(r"AssertionError:\s*(?P<msg>.+)")

    # Runtime error patterns
    ATTRIBUTE_ERROR_RE = re.compile(r"AttributeError:\s*(?P<msg>.+)")
    TYPE_ERROR_RE = re.compile(r"TypeError:\s*(?P<msg>.+)")
    IMPORT_ERROR_RE = re.compile(r"(?:ImportError|ModuleNotFoundError):\s*(?P<msg>.+)")
    VALUE_ERROR_RE = re.compile(r"ValueError:\s*(?P<msg>.+)")
    INDEX_ERROR_RE = re.compile(r"(?:IndexError|KeyError):\s*(?P<msg>.+)")
    RUNTIME_ERROR_RE = re.compile(r"RuntimeError:\s*(?P<msg>.+)")

    # CV-specific patterns
    CV2_ERROR_RE = re.compile(r"cv2\.error[:\s]+(?P<msg>[^\n]+)")
    PIL_ERROR_RE = re.compile(r"(?:PIL\.\w*Error|UnidentifiedImageError):\s*(?P<msg>.+)")
    SHAPE_MISMATCH_RE = re.compile(
        r"(?P<msg>"
        r"(?:Expected \d+[D-]?\s*(?:tensor|input)|"
        r"operands could not be broadcast|"
        r"size mismatch|"
        r"shape \([^)]+\) is invalid|"
        r"mat1 and mat2 shapes cannot be multiplied|"
        r"Sizes of tensors must match|"
        r"Input type .* and weight type .* should be the same)"
        r"[^\n]*)"
    )
    CUDA_ERROR_RE = re.compile(
        r"(?P<msg>"
        r"(?:CUDA out of memory|"
        r"Expected all tensors to be on the same device|"
        r"device-side assert triggered|"
        r"CUDA error:)"
        r"[^\n]*)"
    )
    DTYPE_ERROR_RE = re.compile(
        r"(?P<msg>"
        r"(?:expected scalar type|"
        r"RuntimeError: expected .* but found|"
        r"Input type .* and bias type)"
        r"[^\n]*)"
    )
    # RealSense-specific errors
    RS_TIMEOUT_RE = re.compile(
        r"(?P<msg>"
        r"(?:Frame didn't arrive within|"
        r"RuntimeError: Frame didn't|"
        r"rs2_wait_for_frame|"
        r"Device or resource busy)"
        r"[^\n]*)"
    )
    RS_PIPELINE_RE = re.compile(
        r"(?P<msg>"
        r"(?:RuntimeError: No device connected|"
        r"failed to set power state|"
        r"rs2::error.*pipeline|"
        r"Cannot open RealSense)"
        r"[^\n]*)"
    )
    # YOLO/ultralytics-specific errors
    YOLO_ERROR_RE = re.compile(
        r"(?P<msg>"
        r"(?:ultralytics|YOLO).*(?:Error|error|failed)[^\n]*|"
        r"Model.*not found[^\n]*|"
        r"NMS time limit.*exceeded[^\n]*)"
    )
    # Threading errors
    THREAD_ERROR_RE = re.compile(
        r"(?P<msg>"
        r"(?:RuntimeError: CUDA error.*in another thread|"
        r"cannot schedule new futures after|"
        r"RuntimeError: main thread is not in main loop)"
        r"[^\n]*)"
    )
    # General runtime errors
    ZERO_DIV_RE = re.compile(r"ZeroDivisionError:\s*(?P<msg>[^\n]+)")
    FILE_NOT_FOUND_RE = re.compile(
        r"(?:FileNotFoundError|OSError|IOError):\s*(?P<msg>(?:\[Errno \d+\]\s*)?[^\n]+)"
    )
    RECURSION_ERROR_RE = re.compile(r"RecursionError:\s*(?P<msg>[^\n]+)")
    MEMORY_ERROR_RE = re.compile(r"(?:MemoryError|OutOfMemoryError):\s*(?P<msg>[^\n]*)")
    TORCH_CUDA_MAP_RE = re.compile(
        r"(?P<msg>"
        r"(?:Attempting to deserialize object on a CUDA device|"
        r"Expected all tensors to be on the same device|"
        r"Cannot copy out of meta tensor)"
        r"[^\n]*)"
    )

    def detect(self, checks: list[CheckResult]) -> list[Issue]:
        issues: list[Issue] = []
        for result in checks:
            if result.returncode == 0:
                continue
            output = result.combined_output
            issues.extend(self._parse_syntax_errors(result.name, output))
            issues.extend(self._parse_name_errors(result.name, output))
            issues.extend(self._parse_attribute_errors(result.name, output))
            issues.extend(self._parse_type_errors(result.name, output))
            issues.extend(self._parse_import_errors(result.name, output))
            issues.extend(self._parse_value_errors(result.name, output))
            issues.extend(self._parse_index_errors(result.name, output))
            issues.extend(self._parse_cv_shape_errors(result.name, output))
            issues.extend(self._parse_cuda_errors(result.name, output))
            issues.extend(self._parse_dtype_errors(result.name, output))
            issues.extend(self._parse_cv2_errors(result.name, output))
            issues.extend(self._parse_pil_errors(result.name, output))
            issues.extend(self._parse_realsense_errors(result.name, output))
            issues.extend(self._parse_yolo_errors(result.name, output))
            issues.extend(self._parse_thread_errors(result.name, output))
            issues.extend(self._parse_zero_div_errors(result.name, output))
            issues.extend(self._parse_file_not_found_errors(result.name, output))
            issues.extend(self._parse_recursion_errors(result.name, output))
            issues.extend(self._parse_memory_errors(result.name, output))
            issues.extend(self._parse_torch_cuda_map_errors(result.name, output))
            issues.extend(self._parse_lint_lines(result.name, output))
            issues.extend(self._parse_tracebacks(result.name, output))
            if not issues_for_check(issues, result.name):
                issues.append(
                    Issue(
                        issue_id=f"{result.name}-{short_hash(output[:300] or result.name)}",
                        source_check=result.name,
                        title=f"{result.name} failed",
                        description=f"Check command failed with return code {result.returncode}.",
                        severity="high",
                        confidence=0.8,
                        evidence=trim_text(output, 1200),
                    )
                )
        unique: dict[str, Issue] = {}
        for issue in issues:
            unique[issue.issue_id] = issue
        ranked = sorted(unique.values(), key=lambda i: (severity_rank(i.severity), -i.confidence))
        return ranked

    def _extract_frame(self, output: str, match_start: int) -> tuple[str | None, int | None]:
        prefix = output[:match_start]
        frames = list(self.TRACEBACK_FILE_RE.finditer(prefix))
        if not frames:
            return None, None
        last = frames[-1]
        return sanitize_path(last.group("file")), int(last.group("line"))

    def _parse_attribute_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.ATTRIBUTE_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            if not file_path:
                continue
            issue_id = f"attrerror-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="AttributeError detected",
                description=message,
                severity="medium",
                confidence=0.78,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_type_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.TYPE_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            if not file_path:
                continue
            issue_id = f"typeerror-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="TypeError detected",
                description=message,
                severity="medium",
                confidence=0.78,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_import_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.IMPORT_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"importerror-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="ImportError detected",
                description=message,
                severity="high",
                confidence=0.85,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_value_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.VALUE_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            if not file_path:
                continue
            issue_id = f"valueerror-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="ValueError detected",
                description=message,
                severity="medium",
                confidence=0.75,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_index_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.INDEX_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            if not file_path:
                continue
            issue_id = f"indexerror-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="IndexError/KeyError detected",
                description=message,
                severity="medium",
                confidence=0.75,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_cv_shape_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.SHAPE_MISMATCH_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"cvshape-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="CV shape/dimension mismatch",
                description=message,
                severity="high",
                confidence=0.88,
                file_path=file_path,
                line=line,
                evidence=trim_text(output[max(0, match.start()-200):match.end()+200], 600),
            ))
        return found

    def _parse_cuda_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.CUDA_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"cuda-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="CUDA error detected",
                description=message,
                severity="high",
                confidence=0.92,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 400),
            ))
        return found

    def _parse_dtype_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.DTYPE_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"dtype-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="Tensor dtype mismatch",
                description=message,
                severity="high",
                confidence=0.87,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 400),
            ))
        return found

    def _parse_cv2_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.CV2_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"cv2err-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="cv2 error detected",
                description=message,
                severity="high",
                confidence=0.85,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 400),
            ))
        return found

    def _parse_pil_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.PIL_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"pilerr-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="PIL image error detected",
                description=message,
                severity="medium",
                confidence=0.82,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 400),
            ))
        return found

    def _parse_syntax_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.SYNTAX_RE.finditer(output):
            file_path = sanitize_path(match.group("file"))
            line = int(match.group("line"))
            message = match.group("msg").strip()
            code = match.group("code").strip()
            issue_id = f"syntax-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(
                Issue(
                    issue_id=issue_id,
                    source_check=check_name,
                    title="SyntaxError detected",
                    description=message,
                    severity="high",
                    confidence=0.95,
                    file_path=file_path,
                    line=line,
                    evidence=code,
                )
            )
        return found

    def _parse_name_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.NAME_ERROR_MSG_RE.finditer(output):
            message = match.group("msg").strip()
            prefix = output[: match.start()]
            frames = list(self.TRACEBACK_FILE_RE.finditer(prefix))
            if not frames:
                continue
            last_frame = frames[-1]
            file_path = sanitize_path(last_frame.group("file"))
            line = int(last_frame.group("line"))
            code = ""
            issue_id = f"nameerror-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(
                Issue(
                    issue_id=issue_id,
                    source_check=check_name,
                    title="NameError detected",
                    description=message,
                    severity="medium",
                    confidence=0.75,
                    file_path=file_path,
                    line=line,
                    evidence=code,
                )
            )
        return found

    def _parse_realsense_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for pattern, title in (
            (self.RS_TIMEOUT_RE, "RealSense frame timeout"),
            (self.RS_PIPELINE_RE, "RealSense pipeline/device error"),
        ):
            for match in pattern.finditer(output):
                message = match.group("msg").strip()
                file_path, line = self._extract_frame(output, match.start())
                issue_id = f"rserr-{short_hash(f'{file_path}:{line}:{message}')}"
                found.append(Issue(
                    issue_id=issue_id,
                    source_check=check_name,
                    title=title,
                    description=message,
                    severity="high",
                    confidence=0.88,
                    file_path=file_path,
                    line=line,
                    evidence=trim_text(message, 400),
                ))
        return found

    def _parse_yolo_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.YOLO_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"yoloerr-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="YOLO/ultralytics error",
                description=message,
                severity="high",
                confidence=0.85,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 400),
            ))
        return found

    def _parse_thread_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.THREAD_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"threaderr-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="Threading error detected",
                description=message,
                severity="high",
                confidence=0.86,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 400),
            ))
        return found

    def _parse_zero_div_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.ZERO_DIV_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            if not file_path:
                continue
            issue_id = f"zerodiv-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="ZeroDivisionError detected",
                description=message,
                severity="high",
                confidence=0.90,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_file_not_found_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.FILE_NOT_FOUND_RE.finditer(output):
            message = match.group("msg").strip()
            # Skip OS-level noise that's not about model/data files
            if not any(kw in message.lower() for kw in ("pt", "pth", "onnx", "yaml", "cfg", "weight", "model", "data", "path", "no such")):
                continue
            file_path, line = self._extract_frame(output, match.start())
            if not file_path:
                continue
            issue_id = f"filenotfound-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="FileNotFoundError — missing model/data file",
                description=message,
                severity="high",
                confidence=0.84,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_recursion_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.RECURSION_ERROR_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"recursionerr-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="RecursionError — infinite recursion or deep call stack",
                description=message,
                severity="high",
                confidence=0.88,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_memory_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.MEMORY_ERROR_RE.finditer(output):
            message = match.group("msg").strip() or "Process ran out of system memory."
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"memerr-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="MemoryError — out of RAM",
                description=message,
                severity="high",
                confidence=0.86,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_torch_cuda_map_errors(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.TORCH_CUDA_MAP_RE.finditer(output):
            message = match.group("msg").strip()
            file_path, line = self._extract_frame(output, match.start())
            issue_id = f"torchmap-{short_hash(f'{file_path}:{line}:{message}')}"
            found.append(Issue(
                issue_id=issue_id,
                source_check=check_name,
                title="torch.load() device mismatch — CUDA weights loaded on CPU",
                description=message,
                severity="high",
                confidence=0.90,
                file_path=file_path,
                line=line,
                evidence=trim_text(message, 300),
            ))
        return found

    def _parse_lint_lines(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        for match in self.LINT_RE.finditer(output):
            file_path = sanitize_path(match.group("file"))
            line = int(match.group("line"))
            message = match.group("msg").strip()
            issue_id = f"lint-{short_hash(f'{file_path}:{line}:{message}')}"
            severity = "high" if "error" in message.lower() else "medium"
            found.append(
                Issue(
                    issue_id=issue_id,
                    source_check=check_name,
                    title="Lint/type issue",
                    description=message,
                    severity=severity,
                    confidence=0.7,
                    file_path=file_path,
                    line=line,
                    evidence=trim_text(message, 300),
                )
            )
        return found[:150]

    # Paths that indicate non-user code — skip these frames in traceback analysis
    _SKIP_PATH_TOKENS = (
        "site-packages", "dist-packages", "lib/python", "lib\\python",
        "venv", ".venv", "<frozen", "<string>",
    )

    def _is_user_frame(self, file_path: str) -> bool:
        for token in self._SKIP_PATH_TOKENS:
            if token in file_path:
                return False
        return True

    def _parse_tracebacks(self, check_name: str, output: str) -> list[Issue]:
        found: list[Issue] = []
        tb_matches = list(self.TRACEBACK_FILE_RE.finditer(output))
        if not tb_matches:
            return found

        # Determine the root exception message (last error line in output)
        exc_msg = self._extract_exception_msg(output)

        # Collect all unique user-code frames
        seen: set[str] = set()
        user_frames = []
        for m in tb_matches:
            fp = sanitize_path(m.group("file"))
            ln = int(m.group("line"))
            fn = m.group("func").strip()
            if self._is_user_frame(fp):
                key = f"{fp}:{ln}"
                if key not in seen:
                    seen.add(key)
                    user_frames.append((fp, ln, fn))

        if not user_frames:
            # Fall back to last frame regardless of origin
            last = tb_matches[-1]
            user_frames = [(sanitize_path(last.group("file")), int(last.group("line")), last.group("func").strip())]

        # Innermost (last) user frame = highest confidence — where the crash happened
        inner_fp, inner_ln, inner_fn = user_frames[-1]
        issue_id = f"traceback-{short_hash(f'{inner_fp}:{inner_ln}:{exc_msg}')}"
        found.append(
            Issue(
                issue_id=issue_id,
                source_check=check_name,
                title=f"Runtime crash in {inner_fn}()",
                description=exc_msg,
                severity="high",
                confidence=0.88,
                file_path=inner_fp,
                line=inner_ln,
                evidence=trim_text(output, 800),
            )
        )

        # Emit lower-confidence issues for every other user frame in the call chain
        for fp, ln, fn in user_frames[:-1]:
            frame_id = f"traceback-frame-{short_hash(f'{fp}:{ln}:{exc_msg}')}"
            found.append(
                Issue(
                    issue_id=frame_id,
                    source_check=check_name,
                    title=f"Call chain frame in {fn}()",
                    description=f"Part of traceback leading to: {exc_msg}",
                    severity="medium",
                    confidence=0.65,
                    file_path=fp,
                    line=ln,
                    evidence="",
                )
            )
        return found

    def _extract_exception_msg(self, output: str) -> str:
        """Extract the final exception type + message from traceback output."""
        # Try specific error types first (order matters — most specific first)
        for pattern in (
            self.SHAPE_MISMATCH_RE,
            self.CUDA_ERROR_RE,
            self.DTYPE_ERROR_RE,
            self.CV2_ERROR_RE,
            self.PIL_ERROR_RE,
            self.ATTRIBUTE_ERROR_RE,
            self.TYPE_ERROR_RE,
            self.VALUE_ERROR_RE,
            self.INDEX_ERROR_RE,
            self.IMPORT_ERROR_RE,
            self.NAME_ERROR_MSG_RE,
            self.ASSERTION_RE,
            self.RUNTIME_ERROR_RE,
        ):
            m = pattern.search(output)
            if m:
                return m.group("msg").strip()
        # Generic last-line fallback: last non-empty line of output
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line and not line.startswith("File ") and not line.startswith("Traceback"):
                return trim_text(line, 200)
        return "Unhandled exception in tests/runtime."


def _merge_issues(primary: list[Issue], extra: list[Issue]) -> list[Issue]:
    """Merge two issue lists, deduplicating by issue_id, preserving severity order."""
    unique: dict[str, Issue] = {i.issue_id: i for i in primary}
    for issue in extra:
        unique.setdefault(issue.issue_id, issue)
    return sorted(unique.values(), key=lambda i: (severity_rank(i.severity), -i.confidence))


def issues_for_check(issues: list[Issue], check_name: str) -> bool:
    for issue in issues:
        if issue.source_check == check_name:
            return True
    return False


def sanitize_path(value: str) -> str:
    return value.strip().replace("\\", "/")


def severity_rank(severity: str) -> int:
    ranks = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return ranks.get(severity, 99)


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def root_cause_hypothesis(issue: Issue) -> str:
    text = issue.description.lower()
    title = issue.title.lower()
    if "expected ':'" in text:
        return "Malformed Python block header missing trailing colon."
    if "name" in text and "not defined" in text:
        return "Variable/function typo or missing import before use."
    if "no module named" in text:
        return "Package not installed or wrong virtual environment active."
    if "assertionerror" in text:
        return "Behavior does not match test expectation; logic regression likely."
    if "cuda" in title or "cuda" in text:
        if "out of memory" in text:
            return "GPU OOM: reduce batch size, call torch.cuda.empty_cache(), or use gradient checkpointing."
        if "same device" in text:
            return "Device mismatch: move all tensors to same device with .to(device)."
        return "CUDA error: check device placement and tensor dtypes."
    if "shape" in title or "broadcast" in text or "size mismatch" in text or "shapes cannot be multiplied" in text:
        return "Shape mismatch: check HWC vs CHW order, batch dim, and tensor reshape before ops."
    if "dtype" in title or "scalar type" in text or "expected" in text and "found" in text:
        return "dtype mismatch: cast tensor with .float(), .half(), or .double() to match expected type."
    if "cv2" in title:
        return "cv2 error: check image path exists, array is uint8 HWC, and color space (BGR vs RGB)."
    if "pil" in title or "unidentified" in text:
        return "PIL error: verify image file is valid and not corrupted; check path and format."
    if "attributeerror" in title:
        return "Wrong attribute or method name; check model layer names and object type."
    if "typeerror" in title:
        return "Wrong argument type or count; check function signature and tensor types."
    if "indexerror" in title or "keyerror" in title:
        return "Out-of-bounds access; check array/list length or dict key existence before access."
    if "valueerror" in title:
        return "Invalid value passed to function; check array shape, range, or channel count."
    if "importerror" in title:
        return "Broken or circular import; check module name spelling and package installation."
    if "realsense frame timeout" in title:
        return "Camera disconnected or not streaming. Add timeout_ms to wait_for_frames() and check frame.is_valid() before use."
    if "realsense pipeline" in title:
        return "RealSense device not connected or already in use by another process. Check USB connection and release pipeline properly."
    if "wait_for_frames" in text and "timeout" in text:
        return "Add timeout_ms=5000 to wait_for_frames() and guard with is_valid() to handle disconnects."
    if "is_valid" in text or "get_data" in text:
        return "Always call frame.is_valid() before frame.get_data() — RealSense returns invalid frames on timeout."
    if "yolo" in title or "ultralytics" in title:
        return "Check model path exists, YOLO version matches ultralytics version, and results.boxes is not None before access."
    if "queue.get" in title or "queue.empty" in title or "queue.get" in text or "queue.empty" in text:
        return "Wrap queue.get(timeout=...) in try/except queue.Empty to handle timeout without crash."
    if "thread" in title or "thread" in text:
        return "Access shared state only inside 'with self._lock:'. Use queue for cross-thread data — never share raw attributes."
    if "realsense" in title or "rs2" in text:
        return "Check device connection, release pipeline on exit, and call frame.is_valid() before accessing frame data."
    if "depth" in text and "zero" in text:
        return "Guard depth division: 'if depth > 0: ...' — RealSense returns 0 for invalid/out-of-range pixels."
    if "numpy" in text and "detach" in text:
        return "Use tensor.detach().cpu().numpy() to safely convert — bare .numpy() fails on GPU tensors or tensors with grad."
    if "bgr" in text or "color channel" in text:
        return "cv2.imread returns BGR. plt.imshow and PIL expect RGB. Use cv2.cvtColor(img, cv2.COLOR_BGR2RGB) before display."
    if "zerodivision" in title or "zero division" in title:
        return "Divide-by-zero: check denominator > 0 before dividing. RealSense depth=0 for invalid pixels."
    if "filenotfound" in title or "missing model" in title or "no such file" in text:
        return "File not found: verify path exists before loading. Use pathlib.Path(path).exists() or try/except FileNotFoundError."
    if "recursion" in title:
        return "Infinite recursion: check base case in recursive function or model __call__ override calling itself."
    if "memory" in title and "out of" in title:
        return "Out of RAM: reduce batch size, load data in smaller chunks, or use float16 to halve memory footprint."
    if "torch.load" in title or "map_location" in title or "deserialize" in text:
        return "torch.load() without map_location crashes when CUDA weights loaded on CPU. Add map_location='cpu'."
    if "hardcoded" in title and "cuda" in title:
        return "Use device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') and .to(device). Never hardcode .cuda() or .to('cuda')."
    if "videocapture" in title or "isoopened" in text or "cap.read" in text:
        return "cv2.VideoCapture silently fails on wrong index. Check cap.isOpened() before cap.read() and release cap in finally block."
    if "imwrite" in title:
        return "cv2.imwrite() silently returns False on failure. Check: ok = cv2.imwrite(path, img); assert ok."
    if "daemon" in title or "thread" in title and "blocks" in title:
        return "Camera/worker threads must be daemon=True so they die when main thread exits. Or call .join(timeout=...) on shutdown."
    return "Likely check failure caused by recent code change near referenced location."


class CVASTVisitor(ast.NodeVisitor):
    """Walk one Python file and emit CV-specific issues found via AST analysis."""

    _MODEL_NAME_RE = re.compile(r"model|net|network|classifier|detector|backbone|encoder|decoder", re.I)

    def __init__(self, file_path: pathlib.Path, root: pathlib.Path) -> None:
        self.file_path = file_path
        self.root = root
        self.issues: list[Issue] = []
        self._imread_vars: dict[str, int] = {}      # var -> line of cv2.imread
        self._bgr_vars: set[str] = set()             # vars that hold BGR images
        self._rgb_converted: set[str] = set()        # vars converted via cvtColor
        self._none_checked: set[str] = set()         # vars that had None check
        self._imread_reported: set[str] = set()      # suppress duplicate None-check reports
        self._rs_frame_vars: dict[str, int] = {}     # vars from RealSense frame calls
        self._rs_valid_checked: set[str] = set()     # RS vars that had .is_valid() check
        self._rs_reported: set[str] = set()          # suppress duplicate RS reports
        self._try_handler_stack: list[set[str]] = [] # stack of except handler names per try block
        # VideoCapture tracking
        self._videocap_vars: dict[str, int] = {}     # cap = cv2.VideoCapture(...)
        self._cap_opened_checked: set[str] = set()   # cap.isOpened() seen
        self._cap_reported: set[str] = set()         # suppress dup reports
        # torch.load tracking
        self._no_grad_ctx_depth: int = 0             # nesting depth of torch.no_grad() contexts
        self._eval_called_vars: set[str] = set()     # model vars that had .eval() called

    def _rel(self) -> str:
        try:
            return self.file_path.relative_to(self.root).as_posix()
        except Exception:
            return str(self.file_path)

    def _issue(
        self,
        *,
        issue_id: str,
        title: str,
        desc: str,
        line: int,
        severity: str = "medium",
        confidence: float = 0.75,
    ) -> None:
        self.issues.append(
            Issue(
                issue_id=issue_id,
                source_check="cv_static",
                title=title,
                description=desc,
                severity=severity,
                confidence=confidence,
                file_path=self._rel(),
                line=line,
            )
        )

    @staticmethod
    def _name(node: ast.expr) -> str | None:
        return node.id if isinstance(node, ast.Name) else None

    @staticmethod
    def _is_cv2_call(node: ast.expr, method: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "cv2"
        )

    @staticmethod
    def _chained_attrs(node: ast.expr) -> list[str]:
        """Return method chain as list, e.g. x.detach().cpu().numpy() -> ['detach','cpu','numpy']."""
        chain: list[str] = []
        current = node
        while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
            chain.append(current.func.attr)
            current = current.func.value
        return list(reversed(chain))

    # ------------------------------------------------------------------ visitors

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if isinstance(test, ast.Compare):
            name = self._name(test.left)
            if name and any(isinstance(op, (ast.Is, ast.IsNot)) for op in test.ops):
                self._none_checked.add(name)
        # Also handle: if var: (truthy check)
        name = self._name(test)
        if name and name in self._imread_vars:
            self._none_checked.add(name)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        test = node.test
        if isinstance(test, ast.Compare):
            name = self._name(test.left)
            if name:
                self._none_checked.add(name)
        self.generic_visit(node)

    def _check_imread_none(self, node: ast.Call) -> None:
        """Flag cv2.imread result used directly without None check."""
        if self._is_cv2_call(node, "imread"):
            return
        for arg in node.args:
            name = self._name(arg)
            if (
                name
                and name in self._imread_vars
                and name not in self._none_checked
                and name not in self._imread_reported
                and node.lineno > self._imread_vars[name]
            ):
                issue_id = f"cvstatic-imread-nocheck-{short_hash(self._rel() + name)}"
                self._issue(
                    issue_id=issue_id,
                    title="cv2.imread result used without None check",
                    desc=(
                        f"'{name}' from cv2.imread() at line {self._imread_vars[name]} "
                        f"used at line {node.lineno} without checking for None. "
                        "imread() returns None if file not found or unreadable."
                    ),
                    line=self._imread_vars[name],
                    severity="medium",
                    confidence=0.80,
                )
                self._imread_reported.add(name)

    def _check_numpy_no_detach(self, node: ast.Call) -> None:
        """Flag tensor.numpy() called without .detach() or .cpu() in chain."""
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "numpy"):
            return
        chain = self._chained_attrs(node)
        if "detach" in chain or "cpu" in chain:
            return
        issue_id = f"cvstatic-numpy-nodetach-{short_hash(self._rel() + str(node.lineno))}"
        self._issue(
            issue_id=issue_id,
            title=".numpy() called without .detach()/.cpu()",
            desc=(
                f"Line {node.lineno}: .numpy() on a tensor that may have gradients or be on GPU. "
                "Use .detach().cpu().numpy() to safely convert."
            ),
            line=node.lineno,
            severity="medium",
            confidence=0.75,
        )

    def _check_plt_imshow_bgr(self, node: ast.Call) -> None:
        """Flag plt.imshow(bgr_var) where var came from cv2.imread without cvtColor."""
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "imshow"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "plt"
        ):
            return
        if not node.args:
            return
        arg_name = self._name(node.args[0])
        if arg_name and arg_name in self._bgr_vars and arg_name not in self._rgb_converted:
            issue_id = f"cvstatic-bgr-plt-{short_hash(self._rel() + arg_name + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title="BGR image passed to plt.imshow — colors will be wrong",
                desc=(
                    f"'{arg_name}' from cv2.imread() is BGR but plt.imshow expects RGB. "
                    f"Line {node.lineno}: add cv2.cvtColor({arg_name}, cv2.COLOR_BGR2RGB) before display."
                ),
                line=node.lineno,
                severity="medium",
                confidence=0.85,
            )

    def _check_cv2_resize_dsize(self, node: ast.Call) -> None:
        """Flag cv2.resize(img, img.shape[:2]) — shape slice is (H,W) but dsize needs (W,H)."""
        if not self._is_cv2_call(node, "resize"):
            return
        if len(node.args) < 2:
            return
        dsize = node.args[1]
        # Detect subscript of .shape attribute: img.shape[:2] or img.shape[1::-1]
        if isinstance(dsize, ast.Subscript) and isinstance(dsize.value, ast.Attribute):
            if dsize.value.attr == "shape":
                issue_id = f"cvstatic-resize-dsize-{short_hash(self._rel() + str(node.lineno))}"
                self._issue(
                    issue_id=issue_id,
                    title="cv2.resize dsize likely has wrong (H,W) order",
                    desc=(
                        f"Line {node.lineno}: cv2.resize dsize must be (width, height) "
                        "but .shape gives (height, width, channels). "
                        "Use (img.shape[1], img.shape[0]) or img.shape[:2][::-1]."
                    ),
                    line=node.lineno,
                    severity="medium",
                    confidence=0.85,
                )

    def _check_model_no_eval(self, node: ast.Call) -> None:
        """Flag model(x) called in a function where model.eval() was never seen."""
        # We detect model(x) calls where the callable looks like a model variable
        func = node.func
        if not isinstance(func, ast.Name):
            return
        if not self._MODEL_NAME_RE.search(func.id):
            return
        # Check if .eval() was called anywhere in the same visitor context
        # We use a simple heuristic: flag if model name never had .eval() in file
        # This is caught at module-level; in a class context it may have false positives
        issue_id = f"cvstatic-model-noeval-{short_hash(self._rel() + func.id + str(node.lineno))}"
        self._issue(
            issue_id=issue_id,
            title=f"Model '{func.id}' called — verify eval() and no_grad() used for inference",
            desc=(
                f"Line {node.lineno}: '{func.id}(...)' detected. "
                "During inference call model.eval() and wrap in torch.no_grad() "
                "to disable dropout/batchnorm training mode and avoid gradient memory waste."
            ),
            line=node.lineno,
            severity="low",
            confidence=0.60,
        )

    # ── RealSense checks ──────────────────────────────────────────────────────

    _RS_FRAME_METHODS = {"get_color_frame", "get_depth_frame", "get_infrared_frame",
                         "wait_for_frames", "poll_for_frames"}
    _RS_VALID_CHECKS = {"is_valid", "get_data", "as_frame"}

    def visit_Assign(self, node: ast.Assign) -> None:
        val = node.value
        # Track cv2.imread assignments
        if self._is_cv2_call(val, "imread"):
            for target in node.targets:
                name = self._name(target)
                if name:
                    self._imread_vars[name] = node.lineno
                    self._bgr_vars.add(name)
        # Track cvtColor
        if self._is_cv2_call(val, "cvtColor"):
            for target in node.targets:
                name = self._name(target)
                if name:
                    self._rgb_converted.add(name)
            if isinstance(val, ast.Call) and val.args:
                src = self._name(val.args[0])
                if src:
                    self._rgb_converted.add(src)
        # Track RealSense frame assignments
        if (isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr in self._RS_FRAME_METHODS):
            for target in node.targets:
                name = self._name(target)
                if name:
                    self._rs_frame_vars[name] = node.lineno
        # Track cv2.VideoCapture assignments
        if (isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr == "VideoCapture"
                and isinstance(val.func.value, ast.Name)
                and val.func.value.id == "cv2"):
            for target in node.targets:
                name = self._name(target)
                if name:
                    self._videocap_vars[name] = node.lineno
        # Track .eval() calls: model.eval()
        if (isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr == "eval"):
            obj_name = self._name(val.func.value)
            if obj_name:
                self._eval_called_vars.add(obj_name)
        self.generic_visit(node)

    def _check_wait_for_frames_timeout(self, node: ast.Call) -> None:
        """Flag pipeline.wait_for_frames() called without timeout_ms arg — can hang forever."""
        if not (isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for_frames"):
            return
        has_timeout = any(
            kw.arg == "timeout_ms" for kw in node.keywords
        ) or len(node.args) >= 1
        if not has_timeout:
            issue_id = f"cvstatic-rs-notimeout-{short_hash(self._rel() + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title="wait_for_frames() called without timeout_ms — can hang forever",
                desc=(
                    f"Line {node.lineno}: pipeline.wait_for_frames() with no timeout will block "
                    "indefinitely if camera disconnects. Use wait_for_frames(timeout_ms=5000) "
                    "and check if frame is valid before use."
                ),
                line=node.lineno,
                severity="medium",
                confidence=0.87,
            )

    def _check_rs_frame_used_unchecked(self, node: ast.Call) -> None:
        """Flag RealSense frame var used (e.g. .get_data()) without .is_valid() check."""
        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr not in {"get_data", "get_distance", "as_motion_frame",
                                   "as_depth_frame", "as_video_frame"}:
            return
        obj_name = self._name(node.func.value)
        if (obj_name
                and obj_name in self._rs_frame_vars
                and obj_name not in self._rs_valid_checked
                and obj_name not in self._rs_reported):
            issue_id = f"cvstatic-rs-novalidcheck-{short_hash(self._rel() + obj_name)}"
            self._issue(
                issue_id=issue_id,
                title="RealSense frame used without is_valid() check",
                desc=(
                    f"'{obj_name}' from RealSense at line {self._rs_frame_vars[obj_name]} "
                    f"used at line {node.lineno} without checking .is_valid(). "
                    "Returns invalid frame on timeout/disconnect causing AttributeError or bad data."
                ),
                line=self._rs_frame_vars[obj_name],
                severity="high",
                confidence=0.85,
            )
            self._rs_reported.add(obj_name)

    # ── YOLO checks ───────────────────────────────────────────────────────────

    def _check_yolo_masks_none(self, node: ast.Call) -> None:
        """Flag results.masks.* or results.boxes.* used without None check."""
        # Detect attribute chains: x.masks.xy, x.masks.data, x.boxes.xyxy etc.
        # These manifest as: Attribute(value=Attribute(value=Name, attr='masks'), attr='xy')
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        inner = func.value
        if not isinstance(inner, ast.Attribute):
            return
        if inner.attr in {"masks", "boxes", "keypoints", "probs", "obb"}:
            outer_name = self._name(inner.value)
            attr_chain = f"{inner.attr}.{func.attr}"
            issue_id = f"cvstatic-yolo-nocheck-{short_hash(self._rel() + attr_chain + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title=f"YOLO results.{inner.attr} accessed without None check",
                desc=(
                    f"Line {node.lineno}: '{outer_name}.{attr_chain}' accessed directly. "
                    f"results.{inner.attr} is None when no detections exist — "
                    f"check 'if results.{inner.attr} is not None' before accessing."
                ),
                line=node.lineno,
                severity="high",
                confidence=0.82,
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Track RealSense is_valid(), VideoCapture isOpened(), and model .eval() calls."""
        if node.attr == "is_valid" and isinstance(node.value, ast.Name):
            self._rs_valid_checked.add(node.value.id)
        if node.attr == "isOpened" and isinstance(node.value, ast.Name):
            self._cap_opened_checked.add(node.value.id)
        if node.attr == "eval" and isinstance(node.value, ast.Name):
            self._eval_called_vars.add(node.value.id)
        self.generic_visit(node)

    # ── Depth divide-by-zero ─────────────────────────────────────────────────

    _DEPTH_VAR_RE = re.compile(r"depth|dist|z_val|range_m|depth_m|depth_val", re.I)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """Flag division where denominator is a depth variable (could be 0)."""
        if not isinstance(node.op, ast.Div):
            self.generic_visit(node)
            return
        right = node.right
        denom_name = self._name(right)
        if denom_name and self._DEPTH_VAR_RE.search(denom_name):
            issue_id = f"cvstatic-depth-divzero-{short_hash(self._rel() + denom_name + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title=f"Division by depth variable '{denom_name}' — zero depth crashes",
                desc=(
                    f"Line {node.lineno}: dividing by '{denom_name}' which may be 0.0 "
                    "for invalid/occluded pixels in RealSense depth frames. "
                    "Guard with: 'if {denom_name} > 0:' before division."
                ).replace("{denom_name}", denom_name),
                line=node.lineno,
                severity="high",
                confidence=0.80,
            )
        self.generic_visit(node)

    # ── Queue.get() without Empty handler ────────────────────────────────────

    def visit_Try(self, node: ast.Try) -> None:
        """Track try/except blocks to detect queue.get() without Empty handler."""
        # Check if any handler catches queue.Empty or Empty
        handler_names: set[str] = set()
        for handler in node.handlers:
            if handler.type is None:
                handler_names.add("*")  # bare except
            elif isinstance(handler.type, ast.Attribute):
                handler_names.add(handler.type.attr)
            elif isinstance(handler.type, ast.Name):
                handler_names.add(handler.type.id)
        self._try_handler_stack.append(handler_names)
        self.generic_visit(node)
        self._try_handler_stack.pop()

    def _check_queue_get_no_handler(self, node: ast.Call) -> None:
        """Flag queue.get(timeout=...) not wrapped in try/except queue.Empty."""
        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr != "get":
            return
        # Only flag when 'timeout' keyword is explicitly present.
        # Positional-arg count is NOT used because dict.get(key, default) also has 2 args.
        has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
        if not has_timeout:
            return
        # Check if any enclosing try block catches Empty
        in_empty_handler = any(
            "Empty" in h or "*" in h for h in self._try_handler_stack
        )
        if not in_empty_handler:
            issue_id = f"cvstatic-queue-noempty-{short_hash(self._rel() + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title="queue.get(timeout=...) without queue.Empty handler",
                desc=(
                    f"Line {node.lineno}: .get(timeout=...) raises queue.Empty when timeout expires. "
                    "Wrap in try/except queue.Empty or the thread will crash silently."
                ),
                line=node.lineno,
                severity="medium",
                confidence=0.83,
            )

    # ── Thread lock violation ─────────────────────────────────────────────────

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Check each class for thread-unsafe shared attribute access."""
        _ThreadSafetyChecker(self._rel(), self._issue_raw).check_class(node)
        self.generic_visit(node)

    def _issue_raw(self, **kwargs: Any) -> None:
        self.issues.append(Issue(**kwargs))

    # ── VideoCapture checks ───────────────────────────────────────────────────

    def _check_videocap_read_no_opened(self, node: ast.Call) -> None:
        """Flag cap.read() / cap.grab() on VideoCapture var not guarded by isOpened()."""
        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr not in {"read", "grab"}:
            return
        obj_name = self._name(node.func.value)
        if (obj_name
                and obj_name in self._videocap_vars
                and obj_name not in self._cap_opened_checked
                and obj_name not in self._cap_reported):
            issue_id = f"cvstatic-cap-noopened-{short_hash(self._rel() + obj_name)}"
            self._issue(
                issue_id=issue_id,
                title=f"cv2.VideoCapture '{obj_name}' used without isOpened() check",
                desc=(
                    f"'{obj_name}' created at line {self._videocap_vars[obj_name]}, "
                    f".{node.func.attr}() called at line {node.lineno} without isOpened() guard. "
                    "VideoCapture silently fails on wrong camera index or unavailable device."
                ),
                line=self._videocap_vars[obj_name],
                severity="high",
                confidence=0.86,
            )
            self._cap_reported.add(obj_name)

    # ── torch.load map_location check ────────────────────────────────────────

    def _check_torch_load_no_map_location(self, node: ast.Call) -> None:
        """Flag torch.load(path) without map_location — crashes when CUDA weights loaded on CPU."""
        if not (isinstance(node.func, ast.Attribute)
                and node.func.attr == "load"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "torch"):
            return
        has_map_location = any(kw.arg == "map_location" for kw in node.keywords)
        if not has_map_location:
            issue_id = f"cvstatic-torch-load-nomap-{short_hash(self._rel() + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title="torch.load() missing map_location — device mismatch crash",
                desc=(
                    f"Line {node.lineno}: torch.load() without map_location will crash if weights "
                    "were saved on CUDA but loaded on a CPU-only machine. "
                    "Use torch.load(path, map_location='cpu') or map_location=torch.device('cpu')."
                ),
                line=node.lineno,
                severity="high",
                confidence=0.90,
            )

    # ── Hardcoded CUDA device check ───────────────────────────────────────────

    def _check_hardcoded_cuda(self, node: ast.Call) -> None:
        """Flag .to('cuda') or .cuda() without torch.cuda.is_available() guard."""
        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr == "cuda" and not node.args and not node.keywords:
            issue_id = f"cvstatic-hardcoded-cuda-{short_hash(self._rel() + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title="Hardcoded .cuda() — crashes on CPU-only machine",
                desc=(
                    f"Line {node.lineno}: .cuda() called unconditionally. "
                    "Use device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') "
                    "and .to(device) instead."
                ),
                line=node.lineno,
                severity="medium",
                confidence=0.82,
            )
            return
        if node.func.attr == "to" and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and first_arg.value == "cuda":
                issue_id = f"cvstatic-hardcoded-to-cuda-{short_hash(self._rel() + str(node.lineno))}"
                self._issue(
                    issue_id=issue_id,
                    title='Hardcoded .to("cuda") — crashes on CPU-only machine',
                    desc=(
                        f'Line {node.lineno}: .to("cuda") called unconditionally. '
                        "Use device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') "
                        "and .to(device) instead."
                    ),
                    line=node.lineno,
                    severity="medium",
                    confidence=0.82,
                )

    # ── threading.Thread daemon check ─────────────────────────────────────────

    def _check_thread_no_daemon(self, node: ast.Call) -> None:
        """Flag threading.Thread(...) without daemon=True — thread prevents clean exit."""
        if not (isinstance(node.func, ast.Attribute)
                and node.func.attr == "Thread"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "threading"):
            return
        daemon_kw = next((kw for kw in node.keywords if kw.arg == "daemon"), None)
        if daemon_kw is None:
            issue_id = f"cvstatic-thread-nodaemon-{short_hash(self._rel() + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title="threading.Thread() without daemon=True — blocks clean program exit",
                desc=(
                    f"Line {node.lineno}: Thread created without daemon=True. "
                    "Non-daemon threads block Python exit if they run forever (camera loops). "
                    "Add daemon=True or ensure .join() is called on shutdown."
                ),
                line=node.lineno,
                severity="low",
                confidence=0.72,
            )

    # ── Wire new checks into visit_Call ──────────────────────────────────────

    def visit_Expr(self, node: ast.Expr) -> None:
        """Flag cv2.imwrite() return value discarded — silent write failure."""
        if isinstance(node.value, ast.Call) and self._is_cv2_call(node.value, "imwrite"):
            issue_id = f"cvstatic-imwrite-unchecked-{short_hash(self._rel() + str(node.lineno))}"
            self._issue(
                issue_id=issue_id,
                title="cv2.imwrite() return value discarded — silent failure",
                desc=(
                    f"Line {node.lineno}: cv2.imwrite() returns False if write fails "
                    "(bad path, no disk space, wrong extension). "
                    "Capture return: ok = cv2.imwrite(...) then assert ok or raise."
                ),
                line=node.lineno,
                severity="low",
                confidence=0.75,
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_imread_none(node)
        self._check_numpy_no_detach(node)
        self._check_plt_imshow_bgr(node)
        self._check_cv2_resize_dsize(node)
        self._check_model_no_eval(node)
        self._check_wait_for_frames_timeout(node)
        self._check_rs_frame_used_unchecked(node)
        self._check_yolo_masks_none(node)
        self._check_queue_get_no_handler(node)
        self._check_videocap_read_no_opened(node)
        self._check_torch_load_no_map_location(node)
        self._check_hardcoded_cuda(node)
        self._check_thread_no_daemon(node)
        self.generic_visit(node)



class _ThreadSafetyChecker:
    """Inspect a ClassDef for methods that access shared self._ attrs outside a lock."""

    _LOCK_RE = re.compile(r"_lock|_mutex|_rlock", re.I)
    _SHARED_RE = re.compile(r"^_(frame|running|result|latest|data|state|cap|pipeline)$", re.I)
    _SKIP_METHODS = {"__init__", "__del__", "__enter__", "__exit__"}

    def __init__(self, rel_path: str, issue_fn: Any) -> None:
        self.rel_path = rel_path
        self._emit = issue_fn

    def check_class(self, cls_node: ast.ClassDef) -> None:
        # Only check classes that have a _lock attribute in __init__
        if not self._has_lock(cls_node):
            return
        for node in ast.walk(cls_node):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in self._SKIP_METHODS:
                continue
            self._check_method(node)

    def _has_lock(self, cls_node: ast.ClassDef) -> bool:
        for node in ast.walk(cls_node):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                            and self._LOCK_RE.search(target.attr)):
                        return True
        return False

    def _check_method(self, func: ast.FunctionDef) -> None:
        lock_lines: set[int] = set()  # lines covered by with self._lock:
        unsafe: list[tuple[str, int]] = []

        for node in ast.walk(func):
            if isinstance(node, ast.With):
                for item in node.items:
                    ctx = item.context_expr
                    if (isinstance(ctx, ast.Attribute)
                            and isinstance(ctx.value, ast.Name)
                            and ctx.value.id == "self"
                            and self._LOCK_RE.search(ctx.attr)):
                        # Mark all lines within this with block as lock-protected
                        for child in ast.walk(node):
                            if hasattr(child, "lineno"):
                                lock_lines.add(child.lineno)

        for node in ast.walk(func):
            if isinstance(node, ast.Attribute):
                if (isinstance(node.value, ast.Name)
                        and node.value.id == "self"
                        and self._SHARED_RE.match(node.attr)
                        and node.lineno not in lock_lines):
                    unsafe.append((node.attr, node.lineno))

        seen: set[str] = set()
        for attr, lineno in unsafe:
            if attr in seen:
                continue
            seen.add(attr)
            issue_id = f"cvstatic-thread-nolock-{short_hash(self.rel_path + func.name + attr)}"
            self._emit(
                issue_id=issue_id,
                source_check="cv_static",
                title=f"Thread-unsafe access to self.{attr} in {func.name}()",
                description=(
                    f"self.{attr} accessed at line {lineno} in {func.name}() "
                    "without holding self._lock. Other threads may write concurrently causing race condition."
                ),
                severity="high",
                confidence=0.78,
                file_path=self.rel_path,
                line=lineno,
            )


class CVStaticAnalyzer:
    """Run AST-based CV anti-pattern analysis across all Python files in the repo."""

    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        self.cfg = cfg

    def analyze(self) -> list[Issue]:
        ignore_tokens = self.cfg["watch"]["ignore"]
        # Also exclude files in checks.exclude_paths (e.g. bug_bodyguard.py itself)
        check_excludes = list(self.cfg.get("checks", {}).get("exclude_paths") or [])
        all_ignore = list(ignore_tokens) + check_excludes
        py_files = collect_python_files(self.root, all_ignore)
        issues: list[Issue] = []
        for file_path in py_files:
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(file_path))
            except SyntaxError:
                continue  # already caught by compileall check
            except Exception:
                continue
            visitor = CVASTVisitor(file_path, self.root)
            visitor.visit(tree)
            issues.extend(visitor.issues)
        return issues


class FixPlanner:
    NAME_CAPTURE_RE = re.compile(r"name '([^']+)' is not defined")
    IMPORT_MODULE_CAPTURE_RE = re.compile(r"No module named '([^']+)'")
    COMMON_MODULE_IMPORTS = {
        "os",
        "sys",
        "re",
        "json",
        "math",
        "time",
        "random",
        "pathlib",
        "datetime",
        "subprocess",
        "collections",
        "itertools",
    }
    TYPING_IMPORTS = {"List", "Dict", "Set", "Tuple", "Optional", "Any"}
    # CV library imports: name used in code -> import statement
    CV_IMPORTS: dict[str, str] = {
        "cv2": "import cv2",
        "np": "import numpy as np",
        "numpy": "import numpy as np",
        "torch": "import torch",
        "nn": "import torch.nn as nn",
        "F": "import torch.nn.functional as F",
        "torchvision": "import torchvision",
        "transforms": "from torchvision import transforms",
        "datasets": "from torchvision import datasets",
        "models": "from torchvision import models",
        "Image": "from PIL import Image",
        "ImageDraw": "from PIL import ImageDraw",
        "ImageFont": "from PIL import ImageFont",
        "PIL": "from PIL import Image",
        "plt": "import matplotlib.pyplot as plt",
        "skimage": "import skimage",
        "ski": "import skimage as ski",
        "io": "from skimage import io",
        "tqdm": "from tqdm import tqdm",
        "albumentations": "import albumentations as A",
        "A": "import albumentations as A",
        "DataLoader": "from torch.utils.data import DataLoader",
        "Dataset": "from torch.utils.data import Dataset",
        "optim": "import torch.optim as optim",
        "device": "device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')",
    }

    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        self.cfg = cfg

    def build_proposals(self, issues: list[Issue]) -> list[PatchProposal]:
        proposals: list[PatchProposal] = []
        for issue in issues:
            proposal = self._heuristic_fix(issue)
            if proposal:
                proposals.append(proposal)
        return proposals

    def _heuristic_fix(self, issue: Issue) -> PatchProposal | None:
        if not issue.file_path or not issue.line:
            return None
        raw_file_path = pathlib.Path(issue.file_path)
        file_path = raw_file_path if raw_file_path.is_absolute() else (self.root / raw_file_path)
        if not file_path.exists() or file_path.suffix != ".py":
            return None
        try:
            rel_file_path = to_root_relative(file_path, self.root)
        except Exception:
            rel_file_path = sanitize_path(issue.file_path)
        try:
            original = file_path.read_text(encoding="utf-8")
        except Exception:
            return None
        lines = original.splitlines(keepends=True)
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None

        fixed_lines = list(lines)
        current_line = fixed_lines[idx]
        candidate = current_line.rstrip("\n")

        if "expected ':'" in issue.description and not candidate.rstrip().endswith(":"):
            fixed_lines[idx] = candidate.rstrip() + ":\n"
            summary = "Add missing ':' to fix SyntaxError."
            confidence = 0.85
        elif "torch.load() missing map_location" in issue.title:
            # Find torch.load( in line and add map_location='cpu' before closing paren
            m = re.search(r"(torch\.load\s*\()([^)]*?)(\))", candidate)
            if not m:
                return None
            args_part = m.group(2).rstrip()
            # Add map_location kwarg
            if args_part:
                new_args = args_part + ", map_location='cpu'"
            else:
                new_args = "map_location='cpu'"
            fixed_line = candidate[: m.start(2)] + new_args + candidate[m.end(2):]
            fixed_lines[idx] = fixed_line + "\n"
            summary = "Add map_location='cpu' to torch.load() to prevent CUDA/CPU device mismatch."
            confidence = 0.88
        elif "wait_for_frames() called without timeout_ms" in issue.title:
            m = re.search(r"(\.wait_for_frames\s*\()(\s*\))", candidate)
            if not m:
                return None
            fixed_line = candidate[: m.start(2)] + "timeout_ms=5000" + candidate[m.end(2) - 1:]
            fixed_lines[idx] = fixed_line + "\n"
            summary = "Add timeout_ms=5000 to wait_for_frames() to prevent infinite hang on disconnect."
            confidence = 0.85
        elif "not defined" in issue.description:
            missing_name = self._extract_missing_name(issue.description)
            if not missing_name:
                return None
            maybe_fix = self._add_missing_import(original, lines, idx, missing_name)
            if not maybe_fix:
                return None
            fixed_lines, summary, confidence = maybe_fix
        elif "No module named" in issue.description:
            missing_module = self._extract_missing_module(issue.description)
            if not missing_module:
                return None
            maybe_fix = self._add_missing_import(original, lines, idx, missing_module)
            if not maybe_fix:
                return None
            fixed_lines, summary, confidence = maybe_fix
        else:
            return None

        patched = "".join(fixed_lines)
        if patched == original:
            return None

        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                patched.splitlines(keepends=True),
                fromfile=f"a/{rel_file_path}",
                tofile=f"b/{rel_file_path}",
                n=3,
            )
        )
        proposal_id = f"patch-{short_hash(issue.issue_id + rel_file_path)}"
        return PatchProposal(
            proposal_id=proposal_id,
            issue_id=issue.issue_id,
            file_path=rel_file_path,
            summary=summary,
            confidence=confidence,
            patch=diff,
            proposal_source="heuristic",
        )

    def _extract_missing_name(self, text: str) -> str | None:
        match = self.NAME_CAPTURE_RE.search(text)
        if not match:
            return None
        return match.group(1)

    def _extract_missing_module(self, text: str) -> str | None:
        match = self.IMPORT_MODULE_CAPTURE_RE.search(text)
        if not match:
            return None
        return match.group(1).split(".")[0]

    def _add_missing_import(
        self,
        original: str,
        lines: list[str],
        line_index: int,
        missing_name: str,
    ) -> tuple[list[str], str, float] | None:
        line_text = lines[line_index]
        stripped = line_text.strip()
        import_stmt: str | None = None
        summary = ""
        confidence = 0.0

        if missing_name in self.CV_IMPORTS:
            raw_stmt = self.CV_IMPORTS[missing_name]
            import_stmt = raw_stmt + "\n"
            summary = f"Add missing `{raw_stmt}` for CV NameError."
            confidence = 0.80
        elif missing_name in self.COMMON_MODULE_IMPORTS and f"{missing_name}." in stripped:
            import_stmt = f"import {missing_name}\n"
            summary = f"Add missing `import {missing_name}` for NameError."
            confidence = 0.74
        elif missing_name in self.TYPING_IMPORTS:
            import_stmt = f"from typing import {missing_name}\n"
            summary = f"Add missing `from typing import {missing_name}`."
            confidence = 0.72

        if not import_stmt:
            return None
        if any(line.strip() == import_stmt.strip() for line in lines):
            return None

        if bool(self.cfg.get("fixes", {}).get("prefer_libcst", True)):
            transformed = self._add_import_with_libcst(original, import_stmt)
            if transformed and transformed != original:
                return transformed.splitlines(keepends=True), summary + " (LibCST codemod).", confidence + 0.04

        insertion_idx = find_import_insertion_index(lines)
        fixed_lines = list(lines)
        fixed_lines.insert(insertion_idx, import_stmt)
        return fixed_lines, summary, confidence

    def _add_import_with_libcst(self, source: str, import_stmt: str) -> str | None:
        try:
            import libcst as cst  # type: ignore[import-not-found]
        except Exception:
            return None
        try:
            module = cst.parse_module(source)
            statement = cst.parse_statement(import_stmt)
            body = list(module.body)
            insert_idx = 0
            if body and self._is_docstring_stmt(body[0]):
                insert_idx = 1
            while insert_idx < len(body) and self._is_import_stmt(body[insert_idx]):
                insert_idx += 1
            body.insert(insert_idx, statement)
            new_module = module.with_changes(body=body)
            return new_module.code
        except Exception:
            return None

    def _is_import_stmt(self, stmt: Any) -> bool:
        stmt_name = stmt.__class__.__name__
        if stmt_name != "SimpleStatementLine":
            return False
        body = getattr(stmt, "body", [])
        if len(body) != 1:
            return False
        return body[0].__class__.__name__ in {"Import", "ImportFrom"}

    def _is_docstring_stmt(self, stmt: Any) -> bool:
        stmt_name = stmt.__class__.__name__
        if stmt_name != "SimpleStatementLine":
            return False
        body = getattr(stmt, "body", [])
        if len(body) != 1:
            return False
        if body[0].__class__.__name__ != "Expr":
            return False
        value = getattr(body[0], "value", None)
        return value is not None and value.__class__.__name__ in {"SimpleString", "ConcatenatedString"}


def find_import_insertion_index(lines: list[str]) -> int:
    idx = 0
    if idx < len(lines) and lines[idx].startswith("#!"):
        idx += 1
    if idx < len(lines) and "coding" in lines[idx]:
        idx += 1

    if idx < len(lines) and lines[idx].lstrip().startswith(('"""', "'''")):
        quote = lines[idx].lstrip()[:3]
        idx += 1
        while idx < len(lines):
            if quote in lines[idx]:
                idx += 1
                break
            idx += 1

    while idx < len(lines):
        stripped = lines[idx].strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            idx += 1
            continue
        if stripped == "":
            idx += 1
            continue
        break
    return idx


class ValidationGate:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def validate(self, proposal: PatchProposal) -> PatchProposal:
        target = self.root / proposal.file_path
        if not target.exists():
            proposal.validated = False
            proposal.validation_note = "Target file missing at validation time."
            return proposal
        try:
            patched_text = apply_unified_diff_to_text(target.read_text(encoding="utf-8"), proposal.patch)
            compile(patched_text, proposal.file_path, "exec")
        except Exception as exc:
            proposal.validated = False
            proposal.validation_note = f"Validation failed: {exc}"
            return proposal
        proposal.validated = True
        proposal.validation_note = "Patch compiles successfully (syntax check)."
        return proposal


def apply_unified_diff_to_text(original: str, patch: str) -> str:
    lines = original.splitlines(keepends=True)
    patched = list(lines)
    hunks = parse_unified_hunks(patch)
    offset = 0
    for hunk in hunks:
        start_old = hunk["start_old"] - 1 + offset
        remove_count = hunk["count_old"]
        to_insert = [line for sign, line in hunk["body"] if sign == "+" or sign == " "]
        expected = [line for sign, line in hunk["body"] if sign == "-" or sign == " "]
        segment = patched[start_old : start_old + remove_count]
        if [line_seg.rstrip("\n") for line_seg in segment] != [
            line_seg.rstrip("\n") for line_seg in expected
        ]:
            raise ValueError("Patch hunk context mismatch.")
        patched[start_old : start_old + remove_count] = [
            line_seg + "\n" if not line_seg.endswith("\n") else line_seg for line_seg in to_insert
        ]
        offset += len(to_insert) - remove_count
    return "".join(patched)


def parse_unified_hunks(patch: str) -> list[dict[str, Any]]:
    lines = patch.splitlines()
    hunks: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@"):
            match = re.match(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
            if not match:
                raise ValueError("Invalid unified diff header.")
            start_old = int(match.group(1))
            count_old = int(match.group(2) or "1")
            body: list[tuple[str, str]] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("@@"):
                marker = lines[i][:1]
                if marker in {" ", "-", "+"}:
                    body.append((marker, lines[i][1:]))
                i += 1
            hunks.append({"start_old": start_old, "count_old": count_old, "body": body})
            continue
        i += 1
    return hunks


def read_source_context(file_path: str | None, line: int | None, root: pathlib.Path, context: int = 3) -> str:
    """Return ±context lines around the flagged line, with >>> marker on the target line."""
    if not file_path or not line:
        return ""
    try:
        abs_path = pathlib.Path(file_path) if pathlib.Path(file_path).is_absolute() else root / file_path
        if not abs_path.exists():
            return ""
        src_lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - context - 1)
        end = min(len(src_lines), line + context)
        numbered: list[str] = []
        for i, src_line in enumerate(src_lines[start:end], start=start + 1):
            marker = ">>>" if i == line else "   "
            numbered.append(f"{marker} {i:4d} | {src_line}")
        return "\n".join(numbered)
    except Exception:
        return ""


class ReportWriter:
    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        self.cfg = cfg
        self.report_dir = root / cfg["reporting"]["path"]
        self.patch_dir = root / ".agent" / "patches"

    def _next_report_number(self) -> int:
        ensure_dir(self.report_dir)
        max_n = 0
        for p in self.report_dir.glob("report_*.md"):
            m = re.match(r"report_(\d+)\.md$", p.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return max_n + 1

    def write(
        self,
        check_results: list[CheckResult],
        issues: list[Issue],
        proposals: list[PatchProposal],
        changed_files: list[pathlib.Path],
        static_issues: list[Issue] | None = None,
    ) -> pathlib.Path:
        static_issues = static_issues or []
        ensure_dir(self.report_dir)
        ensure_dir(self.patch_dir)

        for proposal in proposals:
            patch_file = self.patch_dir / f"{proposal.proposal_id}.patch"
            patch_file.write_text(proposal.patch, encoding="utf-8")

        report_num = self._next_report_number()

        # Merge all issues into one ranked list: severity first, then confidence
        all_bugs = sorted(
            issues,
            key=lambda i: (severity_rank(i.severity), -i.confidence),
        )

        # Build issue_id -> bug_number map for patch cross-references
        bug_num_map: dict[str, int] = {bug.issue_id: idx + 1 for idx, bug in enumerate(all_bugs)}

        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        scan_summary = "  |  ".join(
            f"{r.name} {'PASS' if r.returncode == 0 else 'FAIL'}" for r in check_results
        )
        auto_applied = [p for p in proposals if p.auto_applied]
        ready_patches = [p for p in proposals if p.validated and not p.auto_applied]

        lines: list[str] = []
        lines.append(f"# VisionGuard Report #{report_num}")
        lines.append("")
        lines.append(f"**Scan time:** {ts}  ")
        lines.append(f"**Files scanned:** {len(changed_files)} changed  ")
        lines.append(f"**Checks:** {scan_summary}  ")
        lines.append(f"**Total bugs found:** {len(all_bugs)}  ")
        lines.append(f"**Auto-fix patches ready:** {len(ready_patches)}  ")
        lines.append(f"**Auto-applied patches:** {len(auto_applied)}  ")
        lines.append("")
        lines.append("---")
        lines.append("")

        # ── Unified numbered bug list ─────────────────────────────────────────
        lines.append(f"## Bugs Found ({len(all_bugs)})")
        lines.append("")
        if not all_bugs:
            lines.append("No bugs detected.")
        else:
            SEV_ICON = {"critical": "[CRITICAL]", "high": "[HIGH]", "medium": "[MEDIUM]", "low": "[LOW]"}
            SOURCE_LABEL = {"cv_static": "CV Static (AST)", "compileall": "Compile", "pytest": "pytest",
                            "unittest": "unittest", "ruff": "Ruff", "basedpyright": "Type Check"}
            for idx, bug in enumerate(all_bugs, start=1):
                sev_icon = SEV_ICON.get(bug.severity, "[LOW]")
                src_label = SOURCE_LABEL.get(bug.source_check, bug.source_check)
                loc = f"`{bug.file_path}` line {bug.line}" if bug.file_path and bug.line else (
                    f"`{bug.file_path}`" if bug.file_path else "location unknown"
                )
                lines.append(f"### Bug #{idx} — {sev_icon} {bug.title}")
                lines.append("")
                lines.append(f"| Field | Detail |")
                lines.append(f"|---|---|")
                lines.append(f"| **Location** | {loc} |")
                lines.append(f"| **Detected by** | {src_label} |")
                lines.append(f"| **Confidence** | {bug.confidence:.0%} |")
                lines.append(f"| **Description** | {bug.description} |")
                lines.append(f"| **How to fix** | {root_cause_hypothesis(bug)} |")
                lines.append("")
                ctx = read_source_context(bug.file_path, bug.line, self.root)
                if ctx:
                    lines.append("```python")
                    lines.append(ctx)
                    lines.append("```")
                    lines.append("")
                elif bug.evidence:
                    lines.append("```text")
                    lines.append(trim_text(bug.evidence, 400))
                    lines.append("```")
                    lines.append("")

        lines.append("---")
        lines.append("")

        # ── Auto-fix patches ──────────────────────────────────────────────────
        if proposals:
            lines.append(f"## Auto-Fix Patches ({len(proposals)})")
            lines.append("")
            for pidx, proposal in enumerate(proposals, start=1):
                bug_n = bug_num_map.get(proposal.issue_id, "?")
                status_parts = []
                if proposal.validated:
                    status_parts.append("Validated ✓")
                else:
                    status_parts.append(f"Validation failed: {proposal.validation_note}")
                if proposal.auto_applied:
                    status_parts.append("Auto-applied ✓")
                elif proposal.apply_note and proposal.apply_note != "Not applied.":
                    status_parts.append(proposal.apply_note)
                else:
                    status_parts.append("Not applied — review and apply manually")
                status = " | ".join(status_parts)
                lines.append(f"### Patch #{pidx} — {proposal.summary}")
                lines.append("")
                lines.append(f"| Field | Detail |")
                lines.append(f"|---|---|")
                lines.append(f"| **File** | `{proposal.file_path}` |")
                lines.append(f"| **Fixes** | Bug #{bug_n} |")
                lines.append(f"| **Confidence** | {proposal.confidence:.0%} |")
                lines.append(f"| **Status** | {status} |")
                lines.append("")
                if proposal.patch:
                    lines.append("```diff")
                    # Show only the hunk lines (skip --- +++ headers for brevity)
                    patch_lines = [ln for ln in proposal.patch.splitlines()
                                   if not ln.startswith("--- ") and not ln.startswith("+++ ")]
                    lines.append("\n".join(patch_lines))
                    lines.append("```")
                lines.append("")
            lines.append("---")
            lines.append("")

        # ── Check output (failures only) ──────────────────────────────────────
        failed_checks = [r for r in check_results if r.returncode != 0]
        if failed_checks:
            lines.append("## Check Output (Failures)")
            lines.append("")
            for result in failed_checks:
                lines.append(f"### `{result.name}` — FAILED (exit {result.returncode}, {result.duration_seconds:.1f}s)")
                lines.append("")
                lines.append("```text")
                lines.append(trim_text(result.combined_output or "(no output)", 1200))
                lines.append("```")
                lines.append("")

        filename = self.report_dir / f"report_{report_num}.md"
        filename.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return filename


class Notifier:
    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        self.cfg = cfg
        self.enabled = bool(cfg.get("notifications", {}).get("enabled", True))
        self.console = bool(cfg.get("notifications", {}).get("console", True))
        self.min_confidence = float(cfg.get("notifications", {}).get("min_confidence", 0.75))
        self.summary_file = root / cfg.get("notifications", {}).get("summary_file", ".agent/last_notification.txt")

    def publish(
        self,
        issues: list[Issue],
        proposals: list[PatchProposal],
        report_path: pathlib.Path,
    ) -> None:
        if not self.enabled:
            return
        high_conf_issues = [i for i in issues if i.confidence >= self.min_confidence]
        ready_fixes = [p for p in proposals if p.validated and p.confidence >= self.min_confidence]
        message = (
            f"[notify] issues={len(high_conf_issues)} high-confidence, "
            f"ready_fixes={len(ready_fixes)}, report={report_path.as_posix()}"
        )
        ensure_dir(self.summary_file.parent)
        self.summary_file.write_text(message + "\n", encoding="utf-8")
        if self.console:
            print(message)


class Telemetry:
    def __init__(self, root: pathlib.Path, cfg: dict[str, Any]) -> None:
        self.root = root
        telemetry_cfg = cfg.get("telemetry", {})
        self.enabled = bool(telemetry_cfg.get("enabled", True))
        self.events_path = root / telemetry_cfg.get("events_path", ".agent/telemetry/events.jsonl")
        self.tracer = None
        if self.enabled:
            self._init_otel()

    def _init_otel(self) -> None:
        try:
            from opentelemetry import trace  # type: ignore

            self.tracer = trace.get_tracer("bug_bodyguard")
        except Exception:
            self.tracer = None

    def emit(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        payload = {"event": event, "ts": utc_ts(), **fields}
        ensure_dir(self.events_path.parent)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    @contextlib.contextmanager
    def span(self, name: str, **fields: Any):
        span_id = str(uuid.uuid4())
        start = time.perf_counter()
        self.emit("span_start", span=name, span_id=span_id, **fields)
        if self.tracer is not None:
            with self.tracer.start_as_current_span(name) as otel_span:
                for key, value in fields.items():
                    otel_span.set_attribute(key, str(value))
                try:
                    yield
                finally:
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    self.emit("span_end", span=name, span_id=span_id, elapsed_ms=elapsed_ms)
            return
        try:
            yield
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            self.emit("span_end", span=name, span_id=span_id, elapsed_ms=elapsed_ms)


class BodyguardAgent:
    def __init__(self, root: pathlib.Path, config_path: pathlib.Path) -> None:
        self.root = root
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.indexer = RepoIndexer(root, self.cfg)
        self.checks = CheckRunner(root, self.cfg)
        self.detector = BugDetector()
        self.cv_analyzer = CVStaticAnalyzer(root, self.cfg)
        self.fix_planner = FixPlanner(root, self.cfg)
        self.validator = ValidationGate(root)
        self.reporter = ReportWriter(root, self.cfg)
        self.notifier = Notifier(root, self.cfg)
        self.telemetry = Telemetry(root, self.cfg)

    def init_workspace(self) -> None:
        ensure_dir(self.root / ".agent" / "index")
        ensure_dir(self.root / ".agent" / "memory")
        ensure_dir(self.root / ".agent" / "patches")
        ensure_dir(self.root / ".agent" / "reports")
        py_count, symbols = self.indexer.rebuild()
        print(f"[init] Ready. indexed_python_files={py_count}, symbols_indexed={symbols}")

    def scan(self, changed_paths: list[pathlib.Path] | None = None) -> pathlib.Path:
        changed_paths = changed_paths or []
        with self.telemetry.span("scan", changed_files=len(changed_paths)):
            with self.telemetry.span("index_rebuild"):
                py_count, _ = self.indexer.rebuild()
            print(f"[scan] Rebuilt code index ({py_count} Python files).")
            with self.telemetry.span("checks_run"):
                check_results = self.checks.run_all(changed_paths)
            with self.telemetry.span("issue_detection"):
                issues = self.detector.detect(check_results)
            with self.telemetry.span("cv_static_analysis"):
                static_issues = self.cv_analyzer.analyze()
                print(f"[scan] CV static analysis: {len(static_issues)} issue(s) found across codebase.")
                issues = _merge_issues(issues, static_issues)
            with self.telemetry.span("fix_planning"):
                heuristic_proposals = [self.validator.validate(p) for p in self.fix_planner.build_proposals(issues)]
            with self.telemetry.span("ruff_diff_proposals"):
                ruff_proposals = [
                    self.validator.validate(p) for p in RuffDiffProposer(self.root, self.cfg).build_proposals()
                ]
            proposals = merge_heuristic_and_ruff_proposals(heuristic_proposals, ruff_proposals)
            check_results, issues, proposals = self._maybe_auto_apply_guarded(check_results, issues, proposals)
        report = self.reporter.write(check_results, issues, proposals, changed_paths, static_issues)
        self.notifier.publish(issues, proposals, report)
        self.telemetry.emit(
            "scan_summary",
            checks=len(check_results),
            issues=len(issues),
            static_issues=len(static_issues),
            proposals=len(proposals),
            report=report.as_posix(),
        )
        print(
            f"[scan] done: checks={len(check_results)} issues={len(issues)} "
            f"static={len(static_issues)} proposals={len(proposals)} report={report}"
        )
        return report

    def _maybe_auto_apply_guarded(
        self,
        check_results: list[CheckResult],
        issues: list[Issue],
        proposals: list[PatchProposal],
    ) -> tuple[list[CheckResult], list[Issue], list[PatchProposal]]:
        auto_cfg = self.cfg.get("auto_apply", {})
        mode = str(self.cfg.get("mode", "safe_pr"))
        enabled = bool(auto_cfg.get("enabled", False)) or mode == "guarded_auto_apply"
        if not enabled or not proposals:
            return check_results, issues, proposals

        min_conf = float(auto_cfg.get("min_confidence", 0.8))
        max_patches = int(auto_cfg.get("max_patches_per_scan", 1))
        require_all_pass = bool(auto_cfg.get("require_all_checks_pass_after_apply", True))
        preview_only = bool(auto_cfg.get("preview_only", False))
        applied_count = 0

        ranked = sorted(proposals, key=lambda p: p.confidence, reverse=True)
        for proposal in ranked:
            if applied_count >= max_patches:
                proposal.apply_note = "Skipped: auto-apply patch limit reached."
                continue
            if not proposal.validated:
                proposal.apply_note = "Skipped: proposal validation failed."
                continue
            if proposal.confidence < min_conf:
                proposal.apply_note = f"Skipped: confidence {proposal.confidence:.2f} below threshold {min_conf:.2f}."
                continue

            target = self.root / proposal.file_path
            if not target.exists():
                proposal.apply_note = "Skipped: target file not found."
                continue

            original_text = target.read_text(encoding="utf-8")
            try:
                patched_text = apply_unified_diff_to_text(original_text, proposal.patch)
            except Exception as exc:
                proposal.apply_note = f"Skipped: patch apply failed ({exc})."
                continue

            if preview_only:
                target.write_text(patched_text, encoding="utf-8")
                post_checks = self.checks.run_all([target])
                target.write_text(original_text, encoding="utf-8")
                all_pass = all(r.returncode == 0 for r in post_checks)
                if require_all_pass and not all_pass:
                    proposal.apply_note = "Preview: would be reverted because checks fail after apply."
                    continue
                proposal.apply_note = "Preview: eligible and would apply (checks pass)."
                continue

            target.write_text(patched_text, encoding="utf-8")
            post_checks = self.checks.run_all([target])
            all_pass = all(r.returncode == 0 for r in post_checks)
            if require_all_pass and not all_pass:
                target.write_text(original_text, encoding="utf-8")
                proposal.apply_note = "Reverted: checks failed after apply."
                continue

            proposal.auto_applied = True
            proposal.apply_note = "Applied: checks passed after patch."
            applied_count += 1

        if applied_count > 0:
            print(f"[auto-apply] applied {applied_count} guarded patch(es).")
            refreshed_checks = self.checks.run_all([])
            refreshed_issues = self.detector.detect(refreshed_checks)
            return refreshed_checks, refreshed_issues, proposals
        if preview_only:
            print("[auto-apply] preview-only mode: no files were modified.")
        return check_results, issues, proposals

    def watch(self) -> None:
        lock_path = self.root / ".agent" / "bodyguard.lock"
        if self._already_running(lock_path):
            print("[watch] another bodyguard watcher is already running; skipping duplicate start.")
            return
        self._write_lock(lock_path)

        debounce = float(self.cfg["watch"].get("debounce_seconds", 2))
        run_initial_scan = bool(self.cfg["watch"].get("run_initial_scan", True))
        watch_paths = self.cfg["watch"]["paths"]
        ignore_tokens = self.cfg["watch"]["ignore"]
        targets = [(self.root / rel).resolve() for rel in watch_paths if (self.root / rel).resolve().exists()]

        if run_initial_scan:
            print("[watch] running initial scan...")
            self.scan([])

        if self._watch_with_watchfiles(targets, ignore_tokens, debounce):
            self._clear_lock(lock_path)
            return

        changed = queue.Queue()
        try:
            from watchdog.events import FileSystemEventHandler  # type: ignore
            from watchdog.observers import Observer  # type: ignore
        except Exception:
            print("[watch] watchdog unavailable, using polling mode.")
            self._watch_polling()
            return

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # type: ignore[override]
                if event.is_directory:
                    return
                path = pathlib.Path(os.fsdecode(event.src_path))
                if is_ignored(path, ignore_tokens):
                    return
                changed.put(path)

        observer = Observer()
        handler = Handler()
        for target in targets:
            observer.schedule(handler, str(target), recursive=True)
            print(f"[watch] monitoring {target}")
        observer.start()
        print("[watch] started (Ctrl+C to stop)")

        try:
            batch: set[pathlib.Path] = set()
            last_event = 0.0
            while True:
                try:
                    path = changed.get(timeout=0.5)
                    batch.add(pathlib.Path(path))
                    last_event = time.time()
                except queue.Empty:
                    pass

                if batch and (time.time() - last_event) >= debounce:
                    rel_paths = normalize_to_root(self.root, list(batch))
                    print(f"[watch] triggering scan for {len(rel_paths)} changed files.")
                    self.scan(rel_paths)
                    batch.clear()
        except KeyboardInterrupt:
            print("[watch] stopping...")
        finally:
            observer.stop()
            observer.join()
            self._clear_lock(lock_path)

    def _watch_with_watchfiles(
        self,
        targets: list[pathlib.Path],
        ignore_tokens: list[str],
        debounce: float,
    ) -> bool:
        try:
            from watchfiles import watch  # type: ignore
        except Exception:
            return False
        if not targets:
            return False

        for target in targets:
            print(f"[watch] monitoring {target} (watchfiles)")
        print("[watch] started with watchfiles (Ctrl+C to stop)")
        try:
            iterator = watch(*(str(t) for t in targets), debounce=max(200, int(debounce * 1000)), step=100)
            for changes in iterator:
                changed_paths = [pathlib.Path(change[1]) for change in changes]
                filtered = [p for p in changed_paths if not is_ignored(p, ignore_tokens)]
                rel_paths = normalize_to_root(self.root, filtered)
                if not rel_paths:
                    continue
                print(f"[watch] triggering scan for {len(rel_paths)} changed files.")
                self.scan(rel_paths)
        except KeyboardInterrupt:
            print("[watch] stopping...")
        return True

    def _watch_polling(self) -> None:
        poll_seconds = float(self.cfg["watch"].get("poll_seconds", 2))
        ignore_tokens = self.cfg["watch"]["ignore"]
        print(f"[watch] polling every {poll_seconds:.1f}s (Ctrl+C to stop)")
        previous: dict[str, float] = {}
        try:
            while True:
                current: dict[str, float] = {}
                changed: list[pathlib.Path] = []
                for file_path in collect_python_files(self.root, ignore_tokens):
                    rel = file_path.relative_to(self.root).as_posix()
                    mtime = file_path.stat().st_mtime
                    current[rel] = mtime
                    if rel not in previous or previous[rel] != mtime:
                        changed.append(file_path)
                if changed:
                    abs_changed = [p.resolve() for p in changed]
                    print(f"[watch] detected {len(abs_changed)} change(s), scanning.")
                    self.scan(abs_changed)
                previous = current
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("[watch] stopped.")
        finally:
            self._clear_lock(self.root / ".agent" / "bodyguard.lock")

    def _already_running(self, lock_path: pathlib.Path) -> bool:
        if not lock_path.exists():
            return False
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
            if process_is_alive(pid):
                return True
        except Exception:
            pass
        return False

    def _write_lock(self, lock_path: pathlib.Path) -> None:
        ensure_dir(lock_path.parent)
        data = {"pid": os.getpid(), "created_at": utc_ts()}
        lock_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _clear_lock(self, lock_path: pathlib.Path) -> None:
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            # Avoid crashing shutdown on file lock cleanup problems.
            pass

    def list_reports(self, limit: int = 5) -> list[pathlib.Path]:
        report_dir = self.root / self.cfg["reporting"]["path"]
        if not report_dir.exists():
            return []
        reports = sorted(report_dir.glob("report_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        return reports[:limit]


def normalize_to_root(root: pathlib.Path, paths: list[pathlib.Path]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for path in paths:
        try:
            rel = path.resolve().relative_to(root.resolve())
            out.append(root / rel)
        except Exception:
            continue
    return sorted(set(out))


def create_default_files(root: pathlib.Path, config_path: pathlib.Path) -> None:
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"[init] wrote {config_path.name}")
    else:
        print(f"[init] {config_path.name} already exists; keeping current config.")
    ensure_dir(root / ".agent")
    ensure_dir(root / ".agent" / "index")
    ensure_dir(root / ".agent" / "memory")
    ensure_dir(root / ".agent" / "patches")
    ensure_dir(root / ".agent" / "reports")


def write_vscode_autostart_files(root: pathlib.Path, script_name: str) -> None:
    ensure_dir(root / ".vscode")
    tasks = {
        "version": "2.0.0",
        "tasks": [
            {
                "label": "Bodyguard: Auto Start",
                "dependsOrder": "sequence",
                "dependsOn": ["Bodyguard: Init", "Bodyguard: Watch"],
                "runOptions": {"runOn": "folderOpen"},
                "problemMatcher": [],
            },
            {
                "label": "Bodyguard: Init",
                "type": "shell",
                "command": "python",
                "args": [script_name, "init"],
                "presentation": {"reveal": "never", "panel": "dedicated"},
                "problemMatcher": [],
            },
            {
                "label": "Bodyguard: Watch",
                "type": "shell",
                "command": "python",
                "args": [script_name, "watch"],
                "isBackground": True,
                "presentation": {"reveal": "never", "panel": "dedicated"},
                "problemMatcher": [],
            },
            {
                "label": "Bodyguard: Scan",
                "type": "shell",
                "command": "python",
                "args": [script_name, "scan"],
                "presentation": {"reveal": "always", "panel": "dedicated"},
                "problemMatcher": [],
            },
        ],
    }
    settings = {"task.allowAutomaticTasks": "on"}
    (root / ".vscode" / "tasks.json").write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")
    (root / ".vscode" / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print("[bootstrap] wrote .vscode/tasks.json and .vscode/settings.json")


def install_optional_dependencies() -> None:
    packages = [
        "watchfiles",
        "watchdog",
        "libcst",
        "ruff",
        "semgrep",
        "opentelemetry-api",
        "opentelemetry-sdk",
    ]
    print("[bootstrap] installing optional Python packages...")
    subprocess.run([sys.executable, "-m", "pip", "install", *packages], check=False)
    if shutil.which("basedpyright") is not None:
        print("[bootstrap] basedpyright already available.")
        return
    if shutil.which("npm") is not None:
        print("[bootstrap] installing basedpyright via npm...")
        subprocess.run(["npm", "install", "-g", "basedpyright"], check=False)
    else:
        print("[bootstrap] npm not found; skipping basedpyright install.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bug_bodyguard.py",
        description="Drop-in Python bug bodyguard agent (safe patch mode).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              python bug_bodyguard.py bootstrap --install-optional-deps
              python bug_bodyguard.py init
              python bug_bodyguard.py scan
              python bug_bodyguard.py scan src/app.py
              python bug_bodyguard.py watch
              python bug_bodyguard.py report --latest 3
            """
        ),
    )
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--config", default="agent.yml", help="Config filename")

    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap", help="One-file setup: init + optional VS Code + optional deps")
    boot.add_argument("--install-optional-deps", action="store_true", help="Install optional frameworks/tools")
    boot.add_argument("--no-vscode", action="store_true", help="Skip creating VS Code auto-start files")
    boot.add_argument("--start-watch", action="store_true", help="Immediately start watch after setup")
    sub.add_parser("init", help="Create default config and .agent directories")
    scan = sub.add_parser("scan", help="Run checks and generate report")
    scan.add_argument("paths", nargs="*", help="Optional changed file paths")
    sub.add_parser("watch", help="Continuously monitor and scan on changes")
    rep = sub.add_parser("report", help="List latest reports")
    rep.add_argument("--latest", type=int, default=5, help="How many report paths to print")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = pathlib.Path(args.root).resolve()
    config_path = root / args.config

    if args.command == "bootstrap":
        create_default_files(root, config_path)
        if not args.no_vscode:
            write_vscode_autostart_files(root, pathlib.Path(__file__).name)
        if args.install_optional_deps:
            install_optional_dependencies()
        agent = BodyguardAgent(root, config_path)
        agent.init_workspace()
        print("[bootstrap] setup complete.")
        if args.start_watch:
            agent.watch()
        return 0

    if args.command == "init":
        create_default_files(root, config_path)
        agent = BodyguardAgent(root, config_path)
        agent.init_workspace()
        return 0

    if not config_path.exists():
        print("[bootstrap] missing config, creating default setup automatically.")
        create_default_files(root, config_path)
        bootstrap_agent = BodyguardAgent(root, config_path)
        bootstrap_agent.init_workspace()

    agent = BodyguardAgent(root, config_path)

    try:
        if args.command == "scan":
            changed = [root / p for p in args.paths]
            agent.scan(changed if args.paths else None)
        elif args.command == "watch":
            agent.watch()
        elif args.command == "report":
            reports = agent.list_reports(limit=args.latest)
            if not reports:
                print("[report] No reports found.")
            for report in reports:
                print(report.relative_to(root).as_posix())
        else:
            raise ValueError(f"Unsupported command: {args.command}")
        return 0
    except Exception as exc:
        print(f"[fatal] {exc}")
        print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
