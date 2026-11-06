"""Run one robot program inside a throwaway, locked-down container.

What the program can and can't do in there:
  - no network at all (--network none): it can't reach the internet, the host or other containers
  - no secrets: the container gets no environment variables from the host and no API keys exist inside
  - read-only filesystem except /job (its own output folder) and a small /tmp
  - capped memory (no swap), CPU, number of processes, and wall-clock time
  - runs as an unprivileged user, every Linux capability dropped, no privilege escalation
  - gVisor (runsc) as the runtime when available: the program's system calls hit a user-space kernel,
    not the host's
  - the container is deleted after every run
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("SAFEHANDS_IMAGE", "safehands-sandbox:latest")
RUNTIME = os.environ.get("SAFEHANDS_RUNTIME", "")            # "runsc" for gVisor
TIMEOUT_S = int(os.environ.get("SAFEHANDS_TIMEOUT", "120"))
MEMORY = os.environ.get("SAFEHANDS_MEMORY", "768m")                 # untrusted stage
REPLAY_MEMORY = os.environ.get("SAFEHANDS_REPLAY_MEMORY", "1536m")  # trusted replay: renders video, runs no model code
CPUS = os.environ.get("SAFEHANDS_CPUS", "1.5")
PIDS = os.environ.get("SAFEHANDS_PIDS", "64")
GL = os.environ.get("SAFEHANDS_GL", "osmesa")
# For local testing only: extra read-only mounts (e.g. "/usr") when the image is a bare shell.
DEV_MOUNTS = [m for m in os.environ.get("SAFEHANDS_DEV_MOUNTS", "").split(",") if m]
PYTHON = os.environ.get("SAFEHANDS_PYTHON", "python3")


def runtime_available() -> str:
    """The container runtime actually in use, for the dashboard."""
    if RUNTIME:
        return RUNTIME
    return "runc"


def run(job_dir: Path, mode: str, timeout_s: int | None = None) -> dict:
    """Run the runner in `mode` ('plan' = the untrusted program, 'replay' = a validated command list)
    in a fresh container whose only writable folder is `job_dir`."""
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o777)  # the sandbox user (uid 1000) writes its results here, nowhere else
    name = f"safehands-{uuid.uuid4().hex[:12]}"
    mem = MEMORY if mode == "plan" else REPLAY_MEMORY
    cmd = ["docker", "run", "--name", name, "--rm=false",
           "--network", "none",
           "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
           "--memory", mem, "--memory-swap", mem,
           "--cpus", CPUS, "--pids-limit", PIDS,
           "--user", "1000:1000",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--env", f"MUJOCO_GL={GL}", "--env", "LP_NUM_THREADS=2", "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
           "-v", f"{ROOT / 'sandbox'}:/app/sandbox:ro",
           "-v", f"{ROOT / 'sim'}:/app/sim:ro",
           "-v", f"{job_dir}:/job:rw"]
    if RUNTIME:
        cmd += ["--runtime", RUNTIME]
    for m in DEV_MOUNTS:
        cmd += ["-v", f"{m}:{m}:ro"]
    cmd += [IMAGE, PYTHON, "-I", "/app/sandbox/runner.py", "/job", mode]

    timeout_s = timeout_s or TIMEOUT_S
    t0 = time.time()
    timed_out = False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        subprocess.run(["docker", "kill", name], capture_output=True)
        exit_code = -9
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
    inspect = subprocess.run(["docker", "inspect", "-f", "{{json .State}}", name], capture_output=True, text=True)
    state = json.loads(inspect.stdout) if inspect.returncode == 0 and inspect.stdout.strip() else {}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    out_name = "plan.json" if mode == "plan" else "trajectory.json"
    output = None
    of = job_dir / out_name
    if of.exists() and of.stat().st_size < 50_000_000:
        try:
            output = json.loads(of.read_text())
        except json.JSONDecodeError:
            output = None
    return {
        "mode": mode,
        "container": name,
        "runtime": runtime_available(),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_s": timeout_s,
        "oom_killed": bool(state.get("OOMKilled")),
        "wall_seconds": round(time.time() - t0, 2),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-3000:],
        "limits": {"network": "none", "memory": mem, "cpus": CPUS, "pids": PIDS,
                   "filesystem": "read-only (+ /job, 64 MB /tmp)", "user": "uid 1000, no capabilities",
                   "secrets": "none", "timeout_s": timeout_s},
        "output": output,
        "video": "sim.mp4" if (job_dir / "sim.mp4").exists() else None,
    }


def cleanup(job_dir: Path):
    shutil.rmtree(job_dir, ignore_errors=True)
