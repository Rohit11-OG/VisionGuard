# Python Bug Bodyguard

Drop this agent into any Python project root and run it in safe mode. It monitors your codebase, hunts bugs using your configured checks, proposes reviewable patch files, validates them, and writes markdown reports.

## What It Can Do Now

- Auto-start in VS Code when the folder opens.
- Run impacted tests first (for `unittest`) when changed files are known.
- Prefer `watchfiles` for high-performance monitoring, then fallback to `watchdog`, then polling.
- Detect syntax, NameError, lint-style lines, and traceback failures.
- Propose safe patch files (no silent source rewrite).
- Optionally auto-apply patches in guarded mode only when confidence and checks gates pass.
- Use LibCST codemods (when installed) for safer import-fix rewrites while preserving formatting.
- Heuristically fix:
  - missing `:` SyntaxError
  - missing common imports causing NameError (example: `json`, `re`, `os`, `pathlib`)
  - missing typing imports (example: `List`, `Dict`, `Optional`)
- Generate **reviewable Ruff patch previews** from `python -m ruff check . --fix --diff` (no silent rewrite), merged with heuristic proposals.
- Group **issues by bucket** in reports (application code vs agent drop-in vs tooling/excludes vs dependencies).
- Emit confidence-threshold notifications to console and `.agent/last_notification.txt`.

## Files

- `bug_bodyguard.py` - main agent
- `agent.yml` - behavior/config
- `.agent/index/index.json` - code index snapshot
- `.agent/memory/memory.json` - codebase symbol memory
- `.agent/patches/*.patch` - generated fix proposals
- `.agent/reports/*.md` - run reports

## Zero-Click Start in VS Code

1. Put these files in your project root:
   - `bug_bodyguard.py`
   - `agent.yml`
   - `.vscode/tasks.json`
   - `.vscode/settings.json`
   - `scripts/run_bodyguard.ps1`
2. Open the folder in VS Code.
3. Bodyguard auto-starts on folder open:
   - It runs init automatically.
   - It launches watcher automatically.
   - It writes reports under `.agent/reports`.

No manual command is required for normal usage in VS Code.

## One-Command Optional Tool Installer (Windows)

Install stronger optional tooling in one command:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_bodyguard_deps.ps1
```

What it installs:

- `watchfiles` (fast watcher)
- `watchdog` (fallback watcher)
- `libcst` (safe codemod rewrites)
- `ruff` (fast lint checks)
- `semgrep` (pattern/security checks)
- `basedpyright` (type checking; npm first, pip fallback)
- `opentelemetry-api` + `opentelemetry-sdk` (telemetry hooks)

VS Code task alternative:

- Run task: `Bodyguard: Install Optional Deps`

## Manual Commands (Optional)

If you want to run directly:

```powershell
python bug_bodyguard.py init
```

3. Run one scan:

```powershell
python bug_bodyguard.py scan
```

3. Start continuous monitoring:

```powershell
python bug_bodyguard.py watch
```

4. Show latest reports:

```powershell
python bug_bodyguard.py report --latest 5
```

## Safe Mode Behavior

- Never edits your source files automatically.
- Writes `.patch` files for review.
- Runs validation checks and marks patch confidence.
- Logs exactly what was detected and proposed.

## Config

`agent.yml` is JSON-shaped YAML for portability. Edit these keys:

- `watch.paths` - folders to monitor
- `watch.ignore` - ignored paths
- `checks.commands` - lint/type/test commands to run
- `checks.timeout_seconds` - timeout per check
- `checks.run_impacted_tests` - run focused unittest modules from changed files
- `checks.impacted_test_limit` - cap number of impacted test modules
- `checks.exclude_paths` - passed to **ruff** (`--exclude`), **semgrep** (`--exclude`), and **bandit** (`-x` comma list) so dropped-in `bug_bodyguard.py` and `.agent` are not scanned as app code
- `checks.basedpyright` - `level` (`error` or `warning`, minimum diagnostic level) and `fail_on_warnings` (maps to `basedpyright --warnings` non-zero exit when warnings exist)
- `checks.commands[].optional` - skip check automatically if tool is missing
- `checks.commands[]` may include optional **bandit**, **pip_audit**, and **pytest_cov** entries in the default schema; enable or remove them to match your stack
- `fixes.prefer_libcst` - use LibCST codemod before line-based fallback
- `fixes.ruff_diff_proposals` - when true, run `ruff check --fix --diff` and add validated unified-diff proposals (`proposal_source: ruff-diff`)
- `fixes.ruff_diff_extra_args` - extra CLI tokens after `ruff check . --fix --diff` (for example project-specific flags)
- `fixes.ruff_diff_max_proposals` - cap how many Ruff-generated patch files are attached per scan
- `reporting.path` - markdown report output directory
- `notifications.enabled` - turn notifications on/off
- `notifications.min_confidence` - only alert on higher-confidence items
- `notifications.summary_file` - latest notification output file
- `auto_apply.enabled` - allow guarded auto-apply
- `auto_apply.min_confidence` - minimum confidence to apply automatically
- `auto_apply.max_patches_per_scan` - cap applied patches per scan
- `auto_apply.require_all_checks_pass_after_apply` - revert if post-apply checks fail
- `auto_apply.preview_only` - simulate apply and checks, but never write source changes
- `telemetry.enabled` - write structured telemetry events
- `telemetry.events_path` - JSONL file path for spans/events

Command templates support:

- `{changed_files}` - changed file list (quoted)
- `{project_root}` - absolute project root
- `{impacted_tests}` - dotted unittest module list inferred from changed files

## Recommended Check Commands

Start with dependency-free defaults, then tune to your stack:

```json
[
  {"name": "compileall", "command": "python -m compileall -q ."},
  {"name": "unittest", "command": "python -m unittest discover -q"}
]
```

Optional richer setup:

```json
[
  {"name": "pytest", "command": "python -m pytest -q"},
  {"name": "ruff", "command": "python -m ruff check ."},
  {"name": "mypy", "command": "python -m mypy ."}
]
```

Framework-powered stack (all optional):

```json
[
  {"name": "ruff", "command": "python -m ruff check .", "optional": true},
  {"name": "basedpyright", "command": "basedpyright", "optional": true},
  {"name": "semgrep", "command": "semgrep --config auto --error", "optional": true}
]
```

Impacted unittest example command:

```json
{"name": "unittest", "command": "python -m unittest -q {impacted_tests}"}
```

If no impacted tests are inferred, Bodyguard falls back to discovery mode.

## Guarded Auto-Apply Mode

Set either:

- `mode: "guarded_auto_apply"` or
- `auto_apply.enabled: true`

Guard rails:

- Only proposals that already pass validation are eligible.
- Proposal confidence must be `>= auto_apply.min_confidence`.
- Bodyguard reruns checks after apply.
- If checks fail and `require_all_checks_pass_after_apply` is true, Bodyguard reverts the file change automatically.
- Report includes per-proposal apply status and notes.

### Dry-Run Preview Mode

Set:

- `auto_apply.preview_only: true`

Behavior:

- Bodyguard simulates patch application.
- It runs post-apply checks against the simulated state.
- It always restores the original file content.
- Report `apply_note` shows whether the patch would be applied or reverted.

## Notes

- For event-driven watch mode, install `watchdog`:
- For faster event-driven watch mode, install `watchfiles` (preferred):

```powershell
pip install watchfiles
```

- Optional: install `watchdog` as secondary fallback:

```powershell
pip install watchdog
```

- Optional safer codemod rewrites:

```powershell
pip install libcst
```

- Without `watchdog`, the agent automatically falls back to polling mode.
- The default heuristic fixer currently handles conservative syntax fixes (example: missing `:` cases). Keep checks and report review in your loop.
- The agent prevents duplicate watcher instances by using `.agent/bodyguard.lock`.
