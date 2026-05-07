# VisionGuard

Deep static + runtime bug detection agent for Python computer vision projects.

Tuned for **YOLO**, **Intel RealSense**, **MegaPose**, **OpenCV**, **PyTorch**, and **PyQt5**.

---

## Install

### One command — works from any terminal inside your project folder

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/Rohit11-OG/VisionGuard/main/install.ps1 | iex
```

**Linux / macOS:**
```bash
curl -sSL https://raw.githubusercontent.com/Rohit11-OG/VisionGuard/main/install.sh | bash
```

**Or install manually with pip:**
```bash
pip install "git+https://github.com/Rohit11-OG/VisionGuard.git[full]"
```

> `[full]` adds optional extras: `watchfiles`, `ruff`, `libcst`, `opentelemetry`.  
> Drop `[full]` for a minimal install — core scanning works on stdlib only.

---

## Usage

Run every command from inside your CV project directory.

```bash
# Step 1 — one-time setup (creates agent.yml + .vscode tasks)
visionguard bootstrap

# Step 2 — scan now
visionguard scan

# Step 3 — watch mode: auto-scans on every file save
visionguard watch

# List recent reports
visionguard report --latest 5
```

Reports are written to `.agent/reports/report_1.md`, `report_2.md`, ... — each scan makes a new numbered report.

---

## Remove / Uninstall

### Remove agent files from a project

Deletes `.agent/`, `agent.yml`, and the VS Code task files VisionGuard created.  
Your actual source code is never touched.

```bash
# Interactive — asks for confirmation
visionguard clean

# Skip confirmation prompt
visionguard clean --yes
```

### Also remove the CLI tool from your system

```bash
pip uninstall visionguard -y
```

### Full removal (project files + CLI)

```bash
visionguard clean --yes && pip uninstall visionguard -y
```

---

## What It Detects

### CV Static Analysis — no test run needed (AST)

| Bug | Example crash |
|---|---|
| `cv2.imread` without `None` check | Returns `None` on bad path → next line crashes |
| `.numpy()` without `.detach()/.cpu()` | Fails on GPU or grad tensor |
| `plt.imshow` on BGR image | Colors inverted — cv2=BGR, plt=RGB |
| `cv2.resize` dsize wrong order | `.shape[:2]` is `(H,W)` but dsize needs `(W,H)` |
| `wait_for_frames()` without `timeout_ms` | Hangs forever on camera disconnect |
| RealSense frame without `.is_valid()` | `get_data()` on invalid frame crashes |
| `results.masks.xy` without None check | YOLO returns `None` when nothing detected |
| Division by depth variable | RealSense returns `0` for invalid pixels |
| `queue.get(timeout=...)` without `Empty` handler | Silent thread crash on timeout |
| `self._frame` outside `with self._lock:` | Race condition in camera threads |
| `cv2.VideoCapture` without `isOpened()` | Silent fail on wrong camera index |
| `torch.load()` without `map_location` | Crashes loading CUDA weights on CPU-only machine |
| `.to("cuda")` / `.cuda()` hardcoded | Crashes on machines without GPU |
| `threading.Thread` without `daemon=True` | Blocks clean program exit |
| `cv2.imwrite()` return discarded | Silent write failure on bad path / disk full |

### Runtime Error Parsing (from test output)

- `SyntaxError`, `NameError`, `AttributeError`, `TypeError`, `ImportError`
- `ValueError`, `IndexError`, `KeyError`, `ZeroDivisionError`
- `FileNotFoundError` — missing model/weight files
- `RecursionError`, `MemoryError`
- Tensor shape/broadcast mismatches
- CUDA OOM, device mismatch, dtype mismatch, `torch.load` device error
- `cv2.error`, PIL image errors
- RealSense frame timeout, pipeline/device errors
- YOLO/ultralytics model errors
- Threading errors (cross-thread CUDA, event loop)

### Full Call Chain Tracebacks

Captures every user-code frame — not just the crash point.  
Skips `site-packages`, `venv`, stdlib frames automatically.

---

## Report Format

Each scan writes a clean Markdown report:

```
VisionGuard Report #3

Checks: compileall PASS | ruff PASS | basedpyright FAIL
Total bugs found: 6
Auto-fix patches ready: 1

Bug #1 — [HIGH] RealSense frame used without is_valid() check
  File: realsense_camera.py line 47
  Fix:  Always call frame.is_valid() before frame.get_data()
  Code:
    >>> 47 | color_frame = frames.get_color_frame()
        48 | data = color_frame.get_data()   # crashes if frame invalid

Bug #2 — [HIGH] torch.load() missing map_location ...
...

Patch #1 — Add timeout_ms=5000 to wait_for_frames() (fixes Bug #4)
  -    pipeline.wait_for_frames()
  +    pipeline.wait_for_frames(timeout_ms=5000)
```

---

## Checks Run

| Tool | Purpose | Required |
|---|---|---|
| `compileall` | Syntax validation | Always |
| `pytest` | Test failures, runtime errors | If tests exist |
| `unittest` | Test discovery | If tests exist |
| `ruff` | Lint, unused vars, style | Optional |
| `basedpyright` | Static type checking | Optional |

---

## Auto-Fix Proposals

VisionGuard proposes (never silently applies) patches for:

- `torch.load(path)` → `torch.load(path, map_location='cpu')`
- `pipeline.wait_for_frames()` → `pipeline.wait_for_frames(timeout_ms=5000)`
- Missing block `:` (SyntaxError)
- Missing CV imports: `cv2`, `torch`, `np`, `nn`, `F`, `transforms`, `Image`, `plt`, `DataLoader`, `tqdm`, `albumentations`, etc.

Patches written to `.agent/patches/` as `.patch` files. Enable auto-apply in `agent.yml`:

```json
{
  "auto_apply": {
    "enabled": true,
    "min_confidence": 0.85
  }
}
```

---

## Config (`agent.yml`)

Auto-created on first run. Key options:

```json
{
  "mode": "safe_pr",
  "watch": {
    "ignore": ["datasets", "checkpoints", "weights", "runs", "models", "vendor"]
  },
  "auto_apply": {
    "enabled": false,
    "min_confidence": 0.85
  }
}
```

---

## VS Code Integration

`visionguard bootstrap` writes `.vscode/tasks.json` with:
- **Bodyguard: Auto Start** — runs on folder open
- **Bodyguard: Scan** — manual scan
- **Bodyguard: Watch** — background watcher

---

## Target Stack

- `ultralytics` (YOLOv8 / v11)
- `pyrealsense2` (Intel RealSense D4xx / L5xx)
- `torch` / `torchvision`
- `cv2` (OpenCV)
- `numpy`
- `PIL` / `Pillow`
- `PyQt5`
- `albumentations`
- `scipy`
- `threading` / `queue`
