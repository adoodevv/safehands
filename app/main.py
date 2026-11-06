"""SafeHands web app.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, pipeline, redteam, sandbox

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="SafeHands")
pipeline.RUNS.mkdir(exist_ok=True)
app.mount("/runs", StaticFiles(directory=str(pipeline.RUNS)), name="runs")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

MAX_ATTEMPTS = 3
_run_lock = threading.Lock()          # one simulation at a time keeps a small VM responsive
_jobs: dict[str, dict] = {}
_stop = threading.Event()


class RunRequest(BaseModel):
    task: str = ""
    program: str | None = None        # run this exact program instead of asking the model


def _new_job(kind: str) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "q": queue.Queue(), "done": False, "result": None, "started": time.time()}
    _jobs[jid] = job
    return job


def _emitter(job):
    def emit(event, **data):
        job["q"].put({"event": event, **data})
    return emit


def _agent_job(job, req: RunRequest):
    emit = _emitter(job)
    _stop.clear()
    with _run_lock:
        try:
            feedback, result = [], None
            for attempt in range(1, (1 if req.program else MAX_ATTEMPTS) + 1):
                if _stop.is_set():
                    emit("stopped", text="Kill switch: run cancelled")
                    break
                if req.program:
                    prog = {"program": req.program, "model": "pasted program", "mock": False}
                else:
                    emit("stage", stage="write", text=f"Attempt {attempt}: asking the model for a program")
                    try:
                        prog = agent.write_program(req.task, feedback)
                    except agent.AgentError as e:
                        emit("error", text=f"The model didn't return a program: {e}")
                        pipeline.audit("system", "model_error", detail=str(e)[:500])
                        break
                emit("program", attempt=attempt, program=prog["program"], model=prog["model"],
                     finish_reason=prog.get("finish_reason"), usage=prog.get("usage"))
                result = pipeline.run(prog["program"], task=req.task, source=prog["model"], emit=emit)
                result["attempt"] = attempt
                emit("result", result=_slim(result))
                v = result["verdict"]
                if v["approved"] or req.program:
                    break
                failed = [r for r in v["rules"] if not r["passed"]]
                if not any(r["id"] != "sandbox" for r in failed):
                    break   # the program itself failed or was contained; a rewrite of the same task won't help
                broken = [f"{r['title']}: {r['detail']}{_during(result, r.get('first_t'))}" for r in failed]
                feedback.append({"program": prog["program"], "broken": broken})
                pipeline.audit(result["id"], "retry", broken=broken)
            job["result"] = result
        except Exception as e:  # report, never crash the server
            emit("error", text=f"{type(e).__name__}: {e}")
        finally:
            job["done"] = True
            emit("done")


def _redteam_job(job):
    emit = _emitter(job)
    _stop.clear()
    with _run_lock:
        results = []
        try:
            for a in redteam.ATTACKS:
                if _stop.is_set():
                    emit("stopped", text="Kill switch: red team cancelled")
                    break
                emit("attack", id=a["id"], title=a["title"], program=a["program"])
                r = pipeline.run(a["program"], task=a["title"], source="red team")
                j = redteam.judge(a, r)
                results.append(j)
                emit("attack_result", **j)
            passed = sum(r["contained"] for r in results)
            summary = {"passed": passed, "total": len(results), "results": results, "at": time.time()}
            (pipeline.RUNS / "redteam_latest.json").write_text(json.dumps(summary, indent=1))
            pipeline.audit("redteam", "summary", passed=passed, total=len(results))
            job["result"] = summary
            emit("redteam_done", passed=passed, total=len(results))
        except Exception as e:
            emit("error", text=f"{type(e).__name__}: {e}")
        finally:
            job["done"] = True
            emit("done")


def _during(result: dict, t) -> str:
    """Which arm command was running when a rule first broke, so the model knows what to change."""
    if t is None:
        return ""
    moves = [e for e in result.get("events") or [] if e.get("t", 0) <= t and e.get("cmd") != "grasped"]
    if not moves:
        return f" (at {t:.1f} s)"
    e = moves[-1]
    if e["cmd"] == "move_to":
        what = f"arm.move_to({e['x']:.3f}, {e['y']:.3f}, {e['z']:.3f})"
    else:
        what = f"arm.{e['cmd']}()"
    held = [g["object"] for g in result.get("events") or [] if g.get("cmd") == "grasped" and g["t"] <= t]
    return f" (first at {t:.1f} s, during {what}" + (f", after picking up {held[-1]}" if held else "") + ")"


def _slim(result: dict) -> dict:
    return {k: result.get(k) for k in ("id", "task", "source", "program", "verdict", "containment", "plan",
                                       "plan_error", "video", "final_positions", "events", "stage1", "stage2",
                                       "program_status", "program_error", "program_output", "attempt")}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/info")
def info():
    model = "mock (no inference key set)"
    if agent.KEY:
        try:
            model = agent.model_id()
        except Exception as e:
            model = f"unavailable ({type(e).__name__})"
    latest = pipeline.RUNS / "redteam_latest.json"
    return {"model": model, "inference": "Vultr Serverless Inference" if agent.KEY else "mock",
            "runtime": sandbox.runtime_available(), "image": sandbox.IMAGE,
            "limits": {"network": "none", "memory": sandbox.MEMORY, "replay_memory": sandbox.REPLAY_MEMORY, "cpus": sandbox.CPUS, "pids": sandbox.PIDS,
                       "untrusted_timeout_s": pipeline.PLAN_TIMEOUT_S, "replay_timeout_s": sandbox.TIMEOUT_S},
            "audit": pipeline.verify_audit(),
            "redteam": json.loads(latest.read_text()) if latest.exists() else None,
            "attacks": [{"id": a["id"], "title": a["title"]} for a in redteam.ATTACKS],
            "busy": _run_lock.locked()}


@app.post("/api/run")
def start_run(req: RunRequest):
    if not req.program and not req.task.strip():
        raise HTTPException(400, "Give a task or a program")
    if len(req.task) > 2000 or len(req.program or "") > 20000:
        raise HTTPException(413, "Too long")
    job = _new_job("run")
    threading.Thread(target=_agent_job, args=(job, req), daemon=True).start()
    return {"job": job["id"], "queued": _run_lock.locked()}


@app.post("/api/redteam")
def start_redteam():
    job = _new_job("redteam")
    threading.Thread(target=_redteam_job, args=(job,), daemon=True).start()
    return {"job": job["id"], "queued": _run_lock.locked()}


@app.get("/api/jobs/{jid}/events")
def events(jid: str):
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404)

    def stream():
        while True:
            try:
                ev = job["q"].get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(ev)}\n\n"
            if ev["event"] == "done":
                return
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/stop")
def kill_switch():
    """Stop everything now: cancel queued work and kill every running sandbox container."""
    _stop.set()
    ids = subprocess.run(["docker", "ps", "-q", "--filter", "name=safehands-"], capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "kill", *ids], capture_output=True)
    pipeline.audit("system", "kill_switch", killed=len(ids))
    return {"killed": len(ids)}


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    p = pipeline.RUNS / rid / "result.json"
    if not p.resolve().is_relative_to(pipeline.RUNS.resolve()) or not p.exists():
        raise HTTPException(404)
    return json.loads(p.read_text())


@app.get("/api/runs/{rid}/export")
def export(rid: str):
    """The approved command list, for the real arm. Refused unless the run was approved."""
    r = get_run(rid)
    if not r["verdict"]["approved"]:
        raise HTTPException(403, "This run was blocked; only approved plans can go to a real arm")
    return {"run": rid, "task": r["task"], "approved": True, "plan": r["plan"]}


@app.get("/api/audit")
def audit_log(limit: int = 60):
    lines = pipeline.AUDIT.read_text().strip().splitlines() if pipeline.AUDIT.exists() else []
    return {"verify": pipeline.verify_audit(), "entries": [json.loads(x) for x in lines[-limit:]][::-1]}
