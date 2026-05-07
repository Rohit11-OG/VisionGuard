# VisionGuard

A deep, static + runtime bug detection agent for Python computer vision projects.

Built specifically for codebases using **YOLO**, **Intel RealSense**, **MegaPose**, **OpenCV**, **PyTorch**, and **PyQt5**.

---

## What It Detects

### CV Static Analysis (AST — no test run needed)
| Check | Example |
|---|---|
| `cv2.imread` result used without `None` check | `imread()` returns `None` on bad path |
| `.numpy()` without `.detach()/.cpu()` | Crashes on GPU tensor or grad tensor |
| `plt.imshow` on BGR image | Colors inverted — cv2 is BGR, plt expects RGB |
| `cv2.resize` dsize wrong order | `.shape[:2]` gives `(H,W)` but dsize needs `(W,H)` |
| `wait_for_frames()` without `timeout_ms` | Hangs forever on camera disconnect |
| RealSense frame used without `.is_valid()` | `get_data()` on invalid frame crashes |
| `results.masks.xy` without None check | YOLO returns `None` masks when nothing detected |
| Division by depth variable | RealSense returns `0` for invalid pixels |
| `queue.get(timeout=...)` without `Empty` handler | Unhandled exception in camera threads |
| `self._frame` accessed outside `with self._lock:` | Race condition in threaded camera loops |

### Runtime Error Parsing (from test/check output)
- `SyntaxError`, `NameError`, `AttributeError`, `TypeError`, `ImportError`
- `ValueError`, `IndexError`, `KeyError`
- Tensor shape/broadcast mismatches
- CUDA OOM, device mismatch, dtype mismatch
- `cv2.error`, `PIL` image errors
- RealSense frame timeout, pipeline/device errors
- YOLO/ultralytics model errors
- Threading errors (cross-thread CUDA, event loop)

### Full Call Chain Tracebacks
Captures every user-code frame in a traceback — not just the crash point. Skips `site-packages`, `venv`, stdlib frames automatically.

---

## Quick Start

```bash
# One-time setup
python bug_bodyguard.py bootstrap --install-optional-deps

# Run a single scan
python bug_bodyguard.py scan

# Watch mode — auto-scan on every file save
python bug_bodyguard.py watch

# List recent reports
python bug_bodyguard.py report --latest 5
```

Reports are written to `.agent/reports/` as Markdown files.

---

## Checks Run

| Tool | Purpose |
|---|---|
| `compileall` | Syntax validation across all files |
| `pytest` | Test failures, runtime errors |
| `ruff` | Lint, unused vars, code style |
| `basedpyright` | Static type checking |

Security tools (semgrep, bandit) are excluded — this agent focuses on **code correctness only**.

---

## Auto-Fix Proposals

VisionGuard can propose (but not silently apply) patches for:
- Missing `:` on block headers
- Missing CV imports: `cv2`, `torch`, `np`, `nn`, `F`, `transforms`, `Image`, `plt`, `DataLoader`, `tqdm`, `albumentations`, etc.
- Missing stdlib imports causing `NameError`

Patches are written to `.agent/patches/` as `.patch` files for manual review.

---

## Config (`agent.yml`)

```json
{
  "mode": "safe_pr",
  "watch": {
    "ignore": ["datasets", "checkpoints", "weights", "runs", "models", "vendor", "stl_models"]
  },
  "auto_apply": {
    "enabled": false,
    "min_confidence": 0.8
  }
}
```

Set `"enabled": true` under `auto_apply` to let VisionGuard apply high-confidence patches automatically (with post-patch check validation).

---

## VS Code Integration

`bootstrap` writes `.vscode/tasks.json` with autostart tasks:
- **Bodyguard: Auto Start** — runs on folder open
- **Bodyguard: Scan** — manual scan
- **Bodyguard: Watch** — background watcher

---

## Target Stack

Tuned for projects using:
- `ultralytics` (YOLOv8/v11)
- `pyrealsense2` (Intel RealSense D4xx/L5xx)
- `torch` / `torchvision`
- `cv2` (OpenCV)
- `numpy`
- `PIL` / `Pillow`
- `PyQt5`
- `albumentations`
- `scipy`
- `threading` / `queue`
