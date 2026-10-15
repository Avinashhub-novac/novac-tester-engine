"""Frozen release of eQC360 AI.

Every run must give the same result for the same input. That needs three things to stay fixed:
  1. the checks (this engine)            -> RELEASE + checksums of every engine file (manifest.json)
  2. the tools the checks run in         -> FROZEN_TOOLS: a run stops if any tool version is different
  3. the reviewer model and its rules    -> MODEL + the fixed instructions of the background task
A run never produces a report with a different engine or different tools: it stops and says why.
"""
import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path

RELEASE = "1.4.0"
MODEL = "claude-opus-5-5"

# Exact versions the release was built and tested with.
FROZEN_TOOLS = {
    "python": "3.13",
    "chromium": "141.0.7390.37",
    "playwright": "1.56.0",
    "ffmpeg": "6.1.1",
    "libreoffice": "24.2.7.2",
    "poppler": "24.02.0",
    "pillow": "12.3.0",
    "openpyxl": "3.1.5",
    "python-docx": "1.2.0",
    "python-pptx": "1.0.2",
}


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.stdout + r.stderr
    except Exception:
        return ""


def tool_versions():
    from importlib.metadata import version
    v = {"python": ".".join(platform.python_version_tuple()[:2])}
    for pkg in ("playwright", "pillow", "openpyxl", "python-docx", "python-pptx"):
        try:
            v[pkg] = version(pkg)
        except Exception:
            v[pkg] = "missing"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(channel="chromium")
            v["chromium"] = b.version
            b.close()
    except Exception as e:
        v["chromium"] = f"error: {e!r}"[:120]
    m = re.search(r"ffmpeg version (\d+\.\d+(?:\.\d+)?)", _run(["ffmpeg", "-version"]))
    v["ffmpeg"] = m.group(1) if m else "missing"
    m = re.search(r"LibreOffice (\d+(?:\.\d+)+)", _run(["soffice", "--version"]))
    v["libreoffice"] = m.group(1) if m else "missing"
    m = re.search(r"pdftoppm version (\d+(?:\.\d+)+)", _run(["pdftoppm", "-v"]))
    v["poppler"] = m.group(1) if m else "missing"
    return v


def engine_checksums(engine_dir: Path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(engine_dir).glob("*.py")) if p.name != "release_manifest.py"}


def check(engine_dir: Path, manifest: dict | None = None):
    """{ok, release, model, tools, problems: [...]} – problems are in plain words."""
    problems = []
    tools = tool_versions()
    for k, want in FROZEN_TOOLS.items():
        got = tools.get(k, "missing")
        if got != want:
            problems.append(f"{k} is {got}, the frozen release uses {want}")
    if manifest:
        if manifest.get("release") != RELEASE:
            problems.append(f"engine files belong to release {manifest.get('release')}, not {RELEASE}")
        sums = engine_checksums(engine_dir)
        for name, h in (manifest.get("files") or {}).items():
            if sums.get(name) != h:
                problems.append(f"engine file {name} is not the frozen copy")
        extra = set(sums) - set(manifest.get("files") or {})
        if extra:
            problems.append("engine has files that are not part of the release: " + ", ".join(sorted(extra)))
    return {"ok": not problems, "release": RELEASE, "model": MODEL, "tools": tools, "problems": problems}


def stamp():
    """What every report shows about how it was made."""
    return {"release": RELEASE, "model": MODEL, "tools": dict(FROZEN_TOOLS)}


def write_manifest(engine_dir: Path, out: Path):
    out.write_text(json.dumps({"release": RELEASE, "files": engine_checksums(engine_dir)}, indent=1))
