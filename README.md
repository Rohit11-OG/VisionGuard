<!-- ════════════════════════════════════════════════════════════════ -->
<!--                        V I S I O N G U A R D                      -->
<!-- ════════════════════════════════════════════════════════════════ -->

<div align="center">

<a href="https://github.com/Rohit11-OG/VisionGuard">
  <img src="https://capsule-render.vercel.app/api?type=waving&color=0:8E2DE2,50:4A00E0,100:00C9FF&height=260&section=header&text=VisionGuard&fontSize=82&fontColor=ffffff&animation=fadeIn&fontAlignY=38&desc=Deep%20static%20%2B%20runtime%20bug%20detection%20for%20Computer%20Vision&descSize=18&descAlignY=60" alt="VisionGuard" />
</a>

<br/>

<img src="https://readme-typing-svg.demolab.com?font=Fira+Code&weight=600&size=24&pause=900&color=00C9FF&center=true&vCenter=true&width=820&lines=Catches+the+CV+bug+before+the+camera+does.;cv2.imread()+returned+None%3F+Caught.;CUDA+weights+on+a+CPU+box%3F+Caught.;Tensor+shape+mismatch%3F+Caught+with+shapes.;Scan+%E2%86%92+Detect+%E2%86%92+Fix+%E2%86%92+Verify+%E2%86%92+Report." alt="Typing banner" />

<br/><br/>

<!-- ░░░ BADGES ░░░ -->
<img src="https://img.shields.io/badge/python-3.9%20%E2%86%92%203.12-3776AB?style=for-the-badge&logo=python&logoColor=white" />
<img src="https://img.shields.io/badge/zero-dependencies-00C9FF?style=for-the-badge" />
<img src="https://img.shields.io/badge/license-MIT-8E2DE2?style=for-the-badge" />
<img src="https://img.shields.io/badge/tests-31%20passing-4A00E0?style=for-the-badge&logo=pytest&logoColor=white" />

<br/>

<img src="https://img.shields.io/badge/YOLO-purple?style=flat-square&logo=yolo&logoColor=white" />
<img src="https://img.shields.io/badge/Intel%20RealSense-0071C5?style=flat-square&logo=intel&logoColor=white" />
<img src="https://img.shields.io/badge/PyTorch-EE4C2C?style=flat-square&logo=pytorch&logoColor=white" />
<img src="https://img.shields.io/badge/OpenCV-5C3EE8?style=flat-square&logo=opencv&logoColor=white" />
<img src="https://img.shields.io/badge/PyQt5-41CD52?style=flat-square&logo=qt&logoColor=white" />
<img src="https://img.shields.io/badge/MegaPose-FF6F00?style=flat-square" />

</div>

<!-- ░░░ NEON DIVIDER ░░░ -->
<img src="https://capsule-render.vercel.app/api?type=rect&color=0:8E2DE2,50:4A00E0,100:00C9FF&height=3" width="100%" />

```text
        ╔═══════════════════════════════════════════════════════════╗
        ║   ░▒▓  S C A N  →  D E T E C T  →  F I X  →  R E P O R T  ▓▒░ ║
        ╚═══════════════════════════════════════════════════════════╝
              \                                             /
               \         ┌───────────────────────┐         /
                ●────────┤   visionguard scan     ├────────●
                         └───────────┬───────────┘
                          ▼          ▼          ▼
                    [AST checks]  [runtime]  [lint/type]
                          \          |          /
                           ●─────────●─────────●
                                     ▼
                            ╔════════════════╗
                            ║   report.md    ║
                            ╚════════════════╝
```

<div align="center">

### ⚡ The pipeline

</div>

```mermaid
flowchart LR
    A([Your CV code]) -->|scan| B{VisionGuard}
    B --> C[AST static checks]
    B --> D[Check runners<br/>compileall · ruff · pyright · semgrep]
    B --> E[Runtime trace<br/>tensor shapes at crash]
    C --> F[(Issues)]
    D --> F
    E --> F
    F --> G[Fix planner]
    G -->|apply in sandbox| H{Re-run check}
    H -->|passes| I[[Verified patch]]
    H -->|fails| J[Discarded]
    F --> K[[report.md / .sarif / .json]]
    style B fill:#4A00E0,stroke:#00C9FF,color:#fff
    style K fill:#8E2DE2,stroke:#00C9FF,color:#fff
    style I fill:#00C9FF,stroke:#4A00E0,color:#000
```

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:00C9FF,100:8E2DE2&height=3" width="100%" />

## 🚀 Install

<table>
<tr>
<td width="50%">

**Windows · PowerShell**
```powershell
irm https://raw.githubusercontent.com/Rohit11-OG/VisionGuard/main/install.ps1 | iex
```

</td>
<td width="50%">

**Linux / macOS**
```bash
curl -sSL https://raw.githubusercontent.com/Rohit11-OG/VisionGuard/main/install.sh | bash
```

</td>
</tr>
</table>

**Or with pip:**
```bash
pip install "git+https://github.com/Rohit11-OG/VisionGuard.git[full]"
```

> `[full]` adds optional extras — `watchfiles`, `ruff`, `libcst`, `opentelemetry`.
> Drop it for a minimal install: **core scanning runs on the stdlib alone.**

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:8E2DE2,100:00C9FF&height=3" width="100%" />

## 🎮 Usage

```bash
visionguard bootstrap          # one-time setup — agent.yml + VS Code tasks
visionguard scan               # scan now
visionguard watch              # auto-scan on every file save
visionguard report --latest 5  # list recent reports
```

Each scan writes a single rolling **`.agent/reports/report.md`** (set
`reporting.rolling: false` for a numbered history).

<details>
<summary><b>🎯 Scoped scans — faster, less noise</b></summary>

```bash
visionguard scan --since main      # only .py files changed vs a git ref
visionguard scan --since HEAD~1
visionguard scan --staged          # only git-staged files
```

</details>

<details>
<summary><b>📊 Baseline — show only NEW bugs</b></summary>

```bash
visionguard scan --update-baseline # accept current issues
visionguard scan --new-only        # report only what's new
```

Report headers show **new** vs **known** counts. Issue identity is
line-number-independent — a known bug stays matched as code shifts.

</details>

<details>
<summary><b>💥 Runtime crash capture — with tensor shapes</b></summary>

```bash
visionguard run train.py --epochs 10
```

On an uncaught crash, records the traceback **plus the shape, dtype and
device of every array/tensor** live in the failing frames.

</details>

<details>
<summary><b>🛡️ CI gate & pre-commit hook</b></summary>

```bash
visionguard scan --fail-on high    # exit non-zero — drop into CI
visionguard scan --format sarif    # GitHub code-scanning
visionguard scan --format json
visionguard hook                   # install git pre-commit hook
visionguard hook --remove
```

The hook runs `scan --staged --new-only --fail-on high` on every commit.
Override with `git commit --no-verify`.

</details>

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:00C9FF,100:4A00E0&height=3" width="100%" />

## 🔍 What It Detects

<div align="center">

### 🧠 CV Static Analysis — pure AST, no test run needed

</div>

| 🐞 Bug | 💀 Crash it prevents |
|---|---|
| `cv2.imread` without `None` check | Returns `None` on bad path → next line crashes |
| `.numpy()` without `.detach()/.cpu()` | Fails on a GPU or grad tensor |
| `plt.imshow` on a BGR image | Colors inverted — cv2 is BGR, plt is RGB |
| `cv2.resize` dsize order | `.shape[:2]` is `(H,W)` but dsize needs `(W,H)` |
| `wait_for_frames()` without `timeout_ms` | Hangs forever on camera disconnect |
| RealSense frame without `.is_valid()` | `get_data()` on an invalid frame crashes |
| `results.masks.xy` without None check | YOLO returns `None` when nothing detected |
| Division by a depth variable | RealSense returns `0` for invalid pixels |
| `queue.get(timeout=…)` without `Empty` handler | Silent thread crash on timeout |
| `self._frame` outside `with self._lock:` | Race condition in camera threads |
| `cv2.VideoCapture` without `isOpened()` | Silent fail on a wrong camera index |
| `cv2.VideoCapture` never released | Camera/file handle leaks — later opens fail |
| RealSense pipeline started, never stopped | Device stays locked — re-run can't acquire it |
| `torch.load()` without `map_location` | Crashes loading CUDA weights on a CPU-only box |
| `.to("cuda")` / `.cuda()` hardcoded | Crashes on machines without a GPU |
| Discarded tensor transform (`.to`/`.cuda`/`.cpu`/`.half`/`.detach`) | Not in-place — result silently lost |
| `threading.Thread` without `daemon=True` | Blocks clean program exit |
| `cv2.imwrite()` return discarded | Silent write failure on bad path / full disk |

<div align="center">

### ⚙️ Runtime Error Parsing &nbsp;·&nbsp; 🧵 Full Call-Chain Tracebacks

</div>

`SyntaxError` · `NameError` · `AttributeError` · `TypeError` · `ImportError` ·
`ValueError` · `IndexError` · `KeyError` · `ZeroDivisionError` ·
`FileNotFoundError` · `RecursionError` · `MemoryError` · tensor
shape/broadcast mismatches · CUDA OOM / device / dtype errors ·
`cv2.error` · PIL errors · RealSense timeout & pipeline errors ·
YOLO/ultralytics errors · cross-thread CUDA & event-loop errors.

> Every user-code frame is captured — `site-packages`, `venv` and stdlib
> frames are skipped automatically, so a tool's own crash is never
> mistaken for your bug.

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:4A00E0,100:8E2DE2&height=3" width="100%" />

## 📰 Report Format

```text
 ╔══════════════════════════════════════════════════════╗
 ║              V I S I O N G U A R D   R E P O R T      ║
 ╚══════════════════════════════════════════════════════╝
  Checks: compileall PASS | ruff PASS | basedpyright FAIL
  Total bugs: 6   New: 2   Known: 4   Patches ready: 1

  ▸ Bug #1 — [HIGH] RealSense frame used without is_valid()
      realsense_camera.py:47
      >>> 47 | color_frame = frames.get_color_frame()
          48 | data = color_frame.get_data()   # crashes if invalid

  ▸ Patch #1 — add timeout_ms=5000 to wait_for_frames()  [Verified ✓]
      -    pipeline.wait_for_frames()
      +    pipeline.wait_for_frames(timeout_ms=5000)
```

Patches are **sandbox-verified** — applied to a temp copy, the relevant
check re-run, and kept only if it passes. Nothing is silently changed.

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:8E2DE2,100:00C9FF&height=3" width="100%" />

## 🧰 Checks Run

| Tool | Purpose | Required |
|---|---|---|
| `compileall` | Syntax validation | Always |
| `pytest` | Test failures, runtime errors | If tests exist |
| `unittest` | Test discovery | If tests exist |
| `ruff` | Lint, unused vars, style | Optional |
| `basedpyright` | Static type checking | Optional |
| `semgrep` | Pattern-based security/bug rules | Optional |

Checks run **in parallel** and results are **cached on a project
fingerprint** — a rescan with no source change is near-instant.

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:00C9FF,100:8E2DE2&height=3" width="100%" />

## ⚙️ Config — `agent.yml`

Auto-created on first run.

```jsonc
{
  "mode": "safe_pr",
  "watch":     { "ignore": ["datasets", "checkpoints", "weights", "runs"] },
  "checks":    { "cache_results": true, "verify_fixes": true },
  "reporting": { "rolling": true },
  "auto_apply":{ "enabled": false, "min_confidence": 0.85 }
}
```

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:4A00E0,100:00C9FF&height=3" width="100%" />

## 🧹 Remove

```bash
visionguard clean              # delete .agent/, agent.yml, VS Code tasks
visionguard clean --yes        # skip the prompt
pip uninstall visionguard -y   # remove the CLI
```

Your source code is **never** touched.

<img src="https://capsule-render.vercel.app/api?type=rect&color=0:00C9FF,100:8E2DE2&height=3" width="100%" />

## 🎯 Target Stack

<div align="center">

`ultralytics` · `pyrealsense2` · `torch` · `torchvision` · `cv2` ·
`numpy` · `PIL` · `PyQt5` · `albumentations` · `scipy` · `threading` · `queue`

</div>

<br/>

<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:00C9FF,50:4A00E0,100:8E2DE2&height=160&section=footer&text=Ship%20CV%20code%20that%20doesn't%20crash%20on%20the%20robot.&fontSize=20&fontColor=ffffff&animation=twinkling" alt="footer" />

<sub>⭐ Star it if VisionGuard caught a bug for you · MIT Licensed</sub>

</div>
