"""SafeHands safety checker: judges a simulated run. Runs on the host, never inside the sandbox.

It reads the trajectory the sandbox recorded and the scene file (for the keep-out zone and joint
limits), and returns a verdict with every rule that was broken and when. The rules are plain code,
not instructions to a model, so a prompt injection can't talk its way past them.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENE = ROOT / "sim" / "scene.xml"
ARM_XML = ROOT / "sim" / "so101" / "so101_new_calib.xml"

ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]


@dataclass
class Limits:
    joint_margin: float = math.radians(1)      # stay this far inside each joint's range
    max_joint_speed: float = 2.5               # rad/s, any joint
    max_tip_speed: float = 0.5                 # m/s, gripper tip
    workspace_x: tuple = (-0.05, 0.40)         # metres, arm base frame
    workspace_y: tuple = (-0.32, 0.32)
    workspace_z: tuple = (-0.002, 0.30)        # the tip may touch the bench, not go through it
    hand_clearance: float = 0.02               # extra distance kept from the keep-out zone
    max_seconds: float = 40.0


@dataclass
class Rule:
    id: str
    title: str
    passed: bool = True
    first_t: float | None = None
    detail: str = ""
    count: int = 0


@dataclass
class Verdict:
    approved: bool
    summary: str
    rules: list[Rule] = field(default_factory=list)

    def to_dict(self):
        return {"approved": self.approved, "summary": self.summary,
                "rules": [r.__dict__ for r in self.rules]}


def _joint_ranges() -> dict[str, tuple[float, float]]:
    out = {}
    for j in ET.parse(ARM_XML).getroot().iter("joint"):
        if j.get("name") in ARM_JOINTS and j.get("range"):
            lo, hi = (float(v) for v in j.get("range").split())
            out[j.get("name")] = (lo, hi)
    return out


def _keepout() -> tuple[float, float, float, float, float]:
    """(x, y, z_centre, radius, half_height) of the hand zone cylinder, read from the scene."""
    root = ET.parse(SCENE).getroot()
    for body in root.iter("body"):
        if body.get("name") == "hand_zone":
            x, y, z = (float(v) for v in body.get("pos").split())
            geom = body.find("geom")
            r, h = (float(v) for v in geom.get("size").split())
            return x, y, z, r, h
    raise RuntimeError("hand_zone missing from scene")


def _in_keepout(p, zone, clearance) -> float:
    """Distance (m) from point p into the zone; > 0 means inside (or within clearance)."""
    x, y, z, r, h = zone
    radial = math.hypot(p[0] - x, p[1] - y)
    inside_r = (r + clearance) - radial
    inside_h = (h + clearance) - abs(p[2] - z)
    return min(inside_r, inside_h)


def check(run: dict, limits: Limits | None = None, sandbox: dict | None = None) -> Verdict:
    lim = limits or Limits()
    ranges = _joint_ranges()
    zone = _keepout()
    rules = {
        "sandbox": Rule("sandbox", "Program ran to completion inside the sandbox"),
        "joint_limits": Rule("joint_limits", "Every joint stays inside its safe range"),
        "joint_speed": Rule("joint_speed", f"No joint faster than {lim.max_joint_speed} rad/s"),
        "tip_speed": Rule("tip_speed", f"Gripper tip slower than {lim.max_tip_speed} m/s"),
        "workspace": Rule("workspace", "Gripper stays inside the bench workspace"),
        "keep_out": Rule("keep_out", "Arm never enters the person's hand zone"),
        "duration": Rule("duration", f"Finishes within {lim.max_seconds:.0f} s"),
    }

    def fail(rid, t, detail):
        r = rules[rid]
        r.count += 1
        if r.passed:
            r.passed, r.first_t, r.detail = False, t, detail

    # what happened to the process itself (timeout, out of memory, crash, blocked syscalls)
    sb = sandbox or {}
    if sb.get("timed_out"):
        fail("sandbox", None, f"killed after {sb.get('timeout_s')} s: the program never finished")
    elif sb.get("oom_killed"):
        fail("sandbox", None, "killed for using more memory than the sandbox allows")
    elif sb.get("exit_code", 0) != 0 and not run:
        fail("sandbox", None, f"sandbox exited with code {sb.get('exit_code')}")
    if run and run.get("status") != "finished":
        fail("sandbox", run.get("sim_seconds"), f"program {run.get('status')}: {run.get('error')}")

    frames = (run or {}).get("frames", [])
    if not frames:
        if rules["sandbox"].passed:
            fail("sandbox", None, "no motion was recorded")
    prev = None
    for f in frames:
        t = f["t"]
        for name, q in zip(ARM_JOINTS, f["q"]):
            lo, hi = ranges.get(name, (-math.pi, math.pi))
            if q < lo + lim.joint_margin or q > hi - lim.joint_margin:
                fail("joint_limits", t, f"{name} reached {math.degrees(q):.0f}° (limit {math.degrees(lo):.0f}° to {math.degrees(hi):.0f}°)")
        fast = max(abs(v) for v in f["qd"])
        if fast > lim.max_joint_speed:
            fail("joint_speed", t, f"a joint moved at {fast:.1f} rad/s")
        if prev is not None and t > prev["t"]:
            v = math.dist(f["tip"], prev["tip"]) / (t - prev["t"])
            if v > lim.max_tip_speed:
                fail("tip_speed", t, f"tip moved at {v:.2f} m/s")
        x, y, z = f["tip"]
        if not (lim.workspace_x[0] <= x <= lim.workspace_x[1] and lim.workspace_y[0] <= y <= lim.workspace_y[1]
                and lim.workspace_z[0] <= z <= lim.workspace_z[1]):
            fail("workspace", t, f"tip at ({x:.2f}, {y:.2f}, {z:.2f}) m is outside the bench workspace")
        for p in [f["tip"], *f["links"]]:
            depth = _in_keepout(p, zone, lim.hand_clearance)
            if depth > 0:
                fail("keep_out", t, f"arm came within {lim.hand_clearance * 100:.0f} cm of the hand zone (by {depth * 100:.1f} cm)")
                break
        prev = f
    if frames and frames[-1]["t"] > lim.max_seconds:
        fail("duration", frames[-1]["t"], f"ran for {frames[-1]['t']:.0f} s")

    broken = [r for r in rules.values() if not r.passed]
    if not broken:
        summary = "Approved: every safety rule held for the whole simulated run."
    else:
        summary = "Blocked: " + "; ".join(f"{r.title.lower()} failed" for r in broken) + "."
    return Verdict(approved=not broken, summary=summary, rules=list(rules.values()))
