"""Validate the command list an untrusted program produced, before anything replays it.

The untrusted stage can write anything into plan.json. Only what passes here reaches the trusted
replay: known commands, finite numbers in sane ranges, a bounded length.
"""
from __future__ import annotations

import math

MAX_COMMANDS = 200
ALLOWED = {
    "move_to": {"x": (-1.0, 1.0), "y": (-1.0, 1.0), "z": (-0.5, 1.0), "speed": (0.05, 20.0)},
    "open": {},
    "close": {},
    "home": {"speed": (0.05, 20.0)},
    "wait": {"seconds": (0.0, 5.0)},
    "joints": {"speed": (0.05, 20.0)},
}


class PlanError(ValueError):
    pass


def _num(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise PlanError(f"{what} is not a finite number")
    if not lo <= v <= hi:
        raise PlanError(f"{what}={v} is outside {lo}..{hi}")
    return float(v)


def validate(raw) -> list[dict]:
    """Return a clean copy of the plan, or raise PlanError. Speeds and positions are allowed to be
    unsafe here: judging them is the safety checker's job, after the trusted replay."""
    if not isinstance(raw, dict) or not isinstance(raw.get("plan"), list):
        raise PlanError("plan.json is missing or malformed")
    plan = raw["plan"]
    if len(plan) > MAX_COMMANDS:
        raise PlanError(f"{len(plan)} commands (max {MAX_COMMANDS})")
    clean = []
    for i, c in enumerate(plan):
        if not isinstance(c, dict) or c.get("cmd") not in ALLOWED:
            raise PlanError(f"command {i} is not one of {sorted(ALLOWED)}")
        cmd = c["cmd"]
        out = {"cmd": cmd}
        for key, (lo, hi) in ALLOWED[cmd].items():
            if key in c and c[key] is not None:
                out[key] = _num(c[key], lo, hi, f"command {i} {key}")
        if cmd == "move_to" and not all(k in out for k in ("x", "y", "z")):
            raise PlanError(f"command {i} move_to needs x, y and z")
        if cmd == "wait" and "seconds" not in out:
            raise PlanError(f"command {i} wait needs seconds")
        if cmd == "joints":
            q = c.get("q")
            if not isinstance(q, list) or len(q) != 5:
                raise PlanError(f"command {i} joints needs 5 angles")
            out["q"] = [_num(v, -math.pi, math.pi, f"command {i} joint") for v in q]
        clean.append(out)
    return clean
