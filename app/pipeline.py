"""One SafeHands run: untrusted program -> command list -> trusted replay -> safety verdict.

    stage 1  sandbox, model-written code     produces plan.json (arm commands only)
    check    host, plain code                validates the command list
    stage 2  sandbox, NO model-written code  replays the commands, records motion + video
    verdict  host, plain code                safety rules over the recorded motion

Every step is appended to a hash-chained audit log (each line includes the hash of the one before),
so an edited or deleted entry breaks the chain.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import plan as planmod
from . import safety, sandbox

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
AUDIT = RUNS / "audit.jsonl"
_audit_lock = threading.Lock()
PLAN_TIMEOUT_S = 20   # the untrusted stage only runs physics, no video: it never needs long


def audit(run_id: str, kind: str, **data) -> dict:
    """Append one entry to the hash-chained audit log."""
    RUNS.mkdir(exist_ok=True)
    with _audit_lock:
        prev = "0" * 64
        if AUDIT.exists():
            last = AUDIT.read_text().strip().splitlines()
            if last:
                prev = json.loads(last[-1])["hash"]
        entry = {"ts": round(time.time(), 3), "run": run_id, "kind": kind, "data": data, "prev": prev}
        entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        with AUDIT.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def verify_audit() -> dict:
    """Recompute the chain. Returns {'ok': bool, 'entries': n, 'broken_at': index or None}."""
    if not AUDIT.exists():
        return {"ok": True, "entries": 0, "broken_at": None}
    prev = "0" * 64
    lines = AUDIT.read_text().strip().splitlines()
    for i, line in enumerate(lines):
        e = json.loads(line)
        h = e.pop("hash")
        if e["prev"] != prev or hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest() != h:
            return {"ok": False, "entries": len(lines), "broken_at": i}
        prev = h
    return {"ok": True, "entries": len(lines), "broken_at": None}


# Signs, in the untrusted stage's output, that the program tried something the sandbox stopped.
CONTAINMENT_SIGNS = [
    (r"Temporary failure in name resolution|Network is unreachable|Name or service not known|"
     r"getaddrinfo failed|No address associated|Connection refused|nodename nor servname",
     "network", "Network access blocked (no network in the sandbox)"),
    (r"Read-only file system", "filesystem", "Write blocked (read-only filesystem)"),
    (r"Permission denied|Operation not permitted", "permissions", "Privileged action blocked (unprivileged user, no capabilities)"),
    (r"Resource temporarily unavailable|can't start new thread|BlockingIOError|Errno 11\b|EAGAIN",
     "processes", "Process flood blocked (process limit)"),
    (r"MemoryError|Cannot allocate memory", "memory", "Memory grab blocked (memory cap)"),
]


def containment_hits(text: str, sb: dict) -> list[dict]:
    hits = []
    for pattern, kind, label in CONTAINMENT_SIGNS:
        if re.search(pattern, text or ""):
            hits.append({"kind": kind, "label": label})
    if sb.get("timed_out"):
        hits.append({"kind": "time", "label": f"Runaway program killed after {sb['timeout_s']} s (time limit)"})
    if sb.get("oom_killed") and not any(h["kind"] == "memory" for h in hits):
        hits.append({"kind": "memory", "label": "Memory grab killed (memory cap)"})
    return hits


def run(program: str, task: str = "", source: str = "user", emit=lambda event, **d: None) -> dict:
    """Run a program through the whole SafeHands pipeline. `emit` streams progress to the dashboard."""
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    base = RUNS / run_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "program.py").write_text(program)
    audit(run_id, "start", task=task, source=source,
          program_sha256=hashlib.sha256(program.encode()).hexdigest())
    emit("stage", stage="plan", text="Stage 1: running the program in a sealed sandbox")

    # ---- stage 1: untrusted code, sealed, produces only a command list
    untrusted = base / "untrusted"
    untrusted.mkdir()
    (untrusted / "program.py").write_text(program)
    host_before = host_state()
    s1 = sandbox.run(untrusted, "plan", timeout_s=PLAN_TIMEOUT_S)
    host_after = host_state()
    raw_plan = s1.pop("output")
    hits = containment_hits(s1["stderr_tail"] + s1["stdout_tail"], s1)
    audit(run_id, "stage1", exit_code=s1["exit_code"], timed_out=s1["timed_out"], oom=s1["oom_killed"],
          seconds=s1["wall_seconds"], runtime=s1["runtime"], containment=[h["kind"] for h in hits])
    for h in hits:
        emit("contained", **h)
    emit("stage", stage="plan_done", text=f"Stage 1 finished in {s1['wall_seconds']} s", sandbox=_public(s1))

    result = {"id": run_id, "task": task, "source": source, "program": program, "stage1": _public(s1),
              "containment": hits, "plan": None, "plan_error": None, "stage2": None, "verdict": None,
              "video": None, "program_status": (raw_plan or {}).get("status"),
              "program_error": (raw_plan or {}).get("error"),
              "program_output": _tail(s1["stdout_tail"] + "\n" + s1["stderr_tail"]),
              "host": {"processes_before": host_before["processes"], "processes_after": host_after["processes"],
                       "sandboxes_left_running": host_after["sandboxes"]}}

    # ---- host check of the command list
    try:
        clean = planmod.validate(raw_plan)
    except planmod.PlanError as e:
        result["plan_error"] = str(e)
        verdict = safety.Verdict(False, f"Blocked: no usable arm commands ({e}).",
                                 [safety.Rule("sandbox", "Program ran to completion inside the sandbox", False, None, str(e), 1)])
        result["verdict"] = verdict.to_dict()
        audit(run_id, "verdict", approved=False, reason=str(e))
        emit("verdict", **result["verdict"])
        _save(base, result)
        return result
    result["plan"] = clean
    emit("stage", stage="replay", text=f"Stage 2: replaying {len(clean)} arm commands in a fresh simulator (no model code)")
    audit(run_id, "plan", commands=len(clean), plan_sha256=hashlib.sha256(json.dumps(clean).encode()).hexdigest())

    # ---- stage 2: trusted replay, still sandboxed, records motion and video
    trusted = base / "trusted"
    trusted.mkdir()
    (trusted / "plan.json").write_text(json.dumps({"plan": clean}))
    s2 = sandbox.run(trusted, "replay")
    trajectory = s2.pop("output")
    result["stage2"] = _public(s2)
    if s2.get("video"):
        shutil.copy(trusted / "sim.mp4", base / "sim.mp4")
        result["video"] = f"/runs/{run_id}/sim.mp4"
    audit(run_id, "stage2", exit_code=s2["exit_code"], seconds=s2["wall_seconds"],
          frames=len((trajectory or {}).get("frames", [])))

    # ---- verdict: the untrusted program's own failure counts too
    verdict = safety.check(trajectory, sandbox=s2)
    if raw_plan and raw_plan.get("status") != "finished" or s1["timed_out"] or s1["oom_killed"]:
        rule = next(r for r in verdict.rules if r.id == "sandbox")
        if rule.passed:
            rule.passed = False
            rule.detail = (result["program_error"] or (hits[0]["label"] if hits else "program did not finish"))
            rule.count = 1
        verdict.approved = False
        broken = [r for r in verdict.rules if not r.passed]
        verdict.summary = "Blocked: " + "; ".join(f"{r.title.lower()} failed" for r in broken) + "."
    result["verdict"] = verdict.to_dict()
    result["final_positions"] = (trajectory or {}).get("final_positions")
    result["events"] = (trajectory or {}).get("events", [])
    audit(run_id, "verdict", approved=verdict.approved,
          failed=[r.id for r in verdict.rules if not r.passed])
    emit("verdict", **result["verdict"])
    _save(base, result)
    return result


def host_state() -> dict:
    """What the host looks like from outside the sandbox: its process count and any sandbox still running."""
    procs = sum(1 for p in Path("/proc").iterdir() if p.name.isdigit())
    out = subprocess.run(["docker", "ps", "-q", "--filter", "name=safehands-"], capture_output=True, text=True).stdout
    return {"processes": procs, "sandboxes": len(out.split())}


def _tail(text: str, lines: int = 25) -> str:
    """Last lines of what the sealed program printed (deduplicated: a fork bomb prints the same error 64 times)."""
    out, seen = [], set()
    for line in reversed([l for l in text.splitlines() if l.strip()]):
        if line in seen:
            continue
        seen.add(line)
        out.append(line[:300])
        if len(out) >= lines:
            break
    return "\n".join(reversed(out))


def _public(sb: dict) -> dict:
    return {k: sb[k] for k in ("runtime", "exit_code", "timed_out", "oom_killed", "wall_seconds", "limits") if k in sb}


def _save(base: Path, result: dict):
    (base / "result.json").write_text(json.dumps(result, indent=1))
    for d in ("untrusted", "trusted"):
        for f in ("program.py",):
            pass
