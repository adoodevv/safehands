"""SafeHands sandbox runner: runs ONE robot program against a simulated SO-101 and records what happened.

This file is the only thing executed inside the sandbox container. It:
  1. loads the bench scene,
  2. runs the model-written program (job/program.py) with a small robot API (`arm`, `scene`),
  3. records every simulated step (joint angles, speeds, gripper position, every link's position),
  4. writes job/trajectory.json and job/sim.mp4.

It does NOT decide whether the program is safe. The host reads trajectory.json and judges it, so a
program that tampers with anything in here cannot change its own verdict.

Usage (inside the container):  python runner.py /job
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np

os.environ.setdefault("MUJOCO_GL", "osmesa")
import mujoco  # noqa: E402

HERE = Path(__file__).resolve().parent
SCENE = Path(os.environ.get("SAFEHANDS_SCENE", HERE.parent / "sim" / "scene.xml"))

ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
BLOCKS = ["blue_block", "red_block", "yellow_block"]
RECORD_HZ = 30                         # trajectory samples per simulated second
VIDEO_HZ = 8                           # video frames per simulated second (software rendering is slow)
VIDEO_W, VIDEO_H = 432, 324
GRIP_OPEN, GRIP_CLOSED = 1.2, 0.0      # gripper joint angle (rad)
MAX_SIM_SECONDS = 40.0                 # the container's own timeout is the real limit
MAX_COMMANDS = 200


class StopProgram(Exception):
    pass


class Sim:
    def __init__(self, scene: Path, record_video: bool = True, video_path: Path | None = None):
        self.m = mujoco.MjModel.from_xml_path(str(scene))
        self.d = mujoco.MjData(self.m)
        self.qadr = [self.m.jnt_qposadr[self.m.joint(j).id] for j in ARM_JOINTS]
        self.dadr = [self.m.jnt_dofadr[self.m.joint(j).id] for j in ARM_JOINTS]
        self.grip_q = self.m.jnt_qposadr[self.m.joint("gripper").id]
        self.act = {self.m.actuator(i).name: i for i in range(self.m.nu)}
        self.site = self.m.site("gripperframe").id
        self.links = [self.m.body(n).id for n in ("upper_arm", "lower_arm", "wrist", "gripper", "moving_jaw_so101_v1")]
        self.held: str | None = None
        self.held_offset = np.zeros(3)
        self.frames: list[dict] = []
        self.video_frames = 0
        self.renderer = mujoco.Renderer(self.m, VIDEO_H, VIDEO_W) if record_video else None
        self.writer = None
        if record_video and video_path is not None:
            import imageio.v2 as imageio
            # frames go straight to the encoder, so memory use stays flat however long the run is
            self.writer = imageio.get_writer(str(video_path), fps=VIDEO_HZ, codec="libx264", quality=7,
                                             macro_block_size=8, ffmpeg_log_level="error")
        self.cam = self.m.camera("overview").id
        self.events: list[dict] = []
        self.plan: list[dict] = []
        self._next_record = 0.0
        self._next_frame = 0.0
        # start in a folded, safe pose with the gripper open
        self.home_q = np.array([0.0, -1.2, 0.9, 1.4, 0.0])
        for a, q in zip(self.qadr, self.home_q):
            self.d.qpos[a] = q
        self.d.qpos[self.grip_q] = GRIP_OPEN
        self._set_ctrl(self.home_q, GRIP_OPEN)
        mujoco.mj_forward(self.m, self.d)
        self._record(force=True)

    # ------------------------------------------------------------------ low level
    def _set_ctrl(self, q, grip):
        for name, v in zip(ARM_JOINTS, q):
            self.d.ctrl[self.act[name]] = v
        self.d.ctrl[self.act["gripper"]] = grip

    def arm_q(self) -> np.ndarray:
        return np.array([self.d.qpos[a] for a in self.qadr])

    def tip(self) -> np.ndarray:
        return self.d.site_xpos[self.site].copy()

    def _carry(self):
        if self.held:
            b = self.m.body(self.held)
            adr = self.m.jnt_qposadr[b.jntadr[0]]
            self.d.qpos[adr:adr + 3] = self.tip() + self.held_offset
            self.d.qvel[self.m.jnt_dofadr[b.jntadr[0]]:self.m.jnt_dofadr[b.jntadr[0]] + 6] = 0

    def _record(self, force=False):
        if not force and self.d.time < self._next_record:
            return
        self._next_record = self.d.time + 1.0 / RECORD_HZ
        self.frames.append({
            "t": round(self.d.time, 4),
            "q": [round(float(x), 5) for x in self.arm_q()],
            "qd": [round(float(self.d.qvel[a]), 4) for a in self.dadr],
            "grip": round(float(self.d.qpos[self.grip_q]), 4),
            "tip": [round(float(x), 5) for x in self.tip()],
            "links": [[round(float(x), 5) for x in self.d.xpos[b]] for b in self.links],
            "held": self.held,
        })
        if self.renderer is not None and (force or self.d.time >= self._next_frame):
            self._next_frame = self.d.time + 1.0 / VIDEO_HZ
            self.renderer.update_scene(self.d, camera=self.cam)
            if self.writer is not None:
                self.writer.append_data(self.renderer.render())
                self.video_frames += 1

    def step_for(self, seconds: float):
        end = self.d.time + seconds
        while self.d.time < end:
            if self.d.time > MAX_SIM_SECONDS:
                raise StopProgram("simulated time limit reached")
            mujoco.mj_step(self.m, self.d)
            self._carry()
            self._record()

    # ------------------------------------------------------------------ IK
    def solve(self, target: np.ndarray, seed: np.ndarray) -> tuple[np.ndarray, float]:
        """Joint angles that put the gripper tip at `target` pointing straight down (damped least squares)."""
        d = mujoco.MjData(self.m)
        d.qpos[:] = self.d.qpos
        q = seed.copy()
        room = np.radians(2.0)  # never ask a joint to sit on its end stop
        lo = np.array([self.m.jnt_range[self.m.joint(j).id][0] for j in ARM_JOINTS]) + room
        hi = np.array([self.m.jnt_range[self.m.joint(j).id][1] for j in ARM_JOINTS]) - room
        jacp = np.zeros((3, self.m.nv))
        jacr = np.zeros((3, self.m.nv))
        err = 1.0
        for _ in range(300):
            for a, v in zip(self.qadr, q):
                d.qpos[a] = v
            mujoco.mj_kinematics(self.m, d)
            mujoco.mj_comPos(self.m, d)
            pos = d.site_xpos[self.site]
            rot = d.site_xmat[self.site].reshape(3, 3)
            approach = rot[:, 0]                       # tool axis out of the jaws (site x)
            e_pos = target - pos
            e_ang = np.cross(approach, np.array([0.0, 0.0, -1.0]))  # tilt toward straight down
            err = float(np.linalg.norm(e_pos))
            if err < 1e-4 and np.linalg.norm(e_ang) < 1e-3:
                break
            mujoco.mj_jacSite(self.m, d, jacp, jacr, self.site)
            J = np.vstack([jacp[:, self.dadr], 0.3 * jacr[:2, self.dadr]])
            e = np.concatenate([e_pos, 0.3 * e_ang[:2]])
            dq = J.T @ np.linalg.solve(J @ J.T + 1e-4 * np.eye(5), e)
            q = np.clip(q + 0.8 * dq, lo, hi)
        return q, err

    def move_joints(self, q_target: np.ndarray, speed: float, grip: float | None = None):
        """Move in a straight line in joint space at `speed` rad/s (fastest joint)."""
        q0 = self.arm_q()
        g = self.d.ctrl[self.act["gripper"]] if grip is None else grip
        span = float(np.max(np.abs(q_target - q0)))
        duration = max(span / max(speed, 1e-3), 0.05)
        steps = max(int(duration / 0.02), 1)
        for i in range(1, steps + 1):
            s = i / steps
            s = s * s * (3 - 2 * s)                    # ease in/out
            self._set_ctrl(q0 + (q_target - q0) * s, g)
            self.step_for(0.02)
        self.step_for(0.15)                            # settle


class Arm:
    """The robot API a program can use. Distances in metres, in the arm's base frame (x forward, y left)."""

    def __init__(self, sim: Sim):
        self._sim = sim
        self.speed = 1.0                               # rad/s for the fastest joint

    def _log(self, kind, **kw):
        self._sim.events.append({"t": round(self._sim.d.time, 3), "cmd": kind, **kw})

    def _plan(self, cmd, **args):
        if len(self._sim.plan) >= MAX_COMMANDS:
            raise StopProgram(f"more than {MAX_COMMANDS} arm commands")
        self._sim.plan.append({"cmd": cmd, **args})

    def move_to(self, x: float, y: float, z: float, speed: float | None = None):
        """Move the gripper tip to (x, y, z), pointing straight down."""
        self._plan("move_to", x=float(x), y=float(y), z=float(z), speed=float(speed or self.speed))
        target = np.array([float(x), float(y), float(z)])
        q, err = self._sim.solve(target, self._sim.arm_q())
        self._log("move_to", x=x, y=y, z=z, speed=speed or self.speed, ik_error_mm=round(err * 1000, 1))
        self._sim.move_joints(q, speed or self.speed)

    def open(self):
        self._plan("open")
        self._log("open")
        if self._sim.held:
            self._sim.held = None
        self._sim.move_joints(self._sim.arm_q(), 1.0, GRIP_OPEN)
        self._sim.step_for(0.3)

    def close(self):
        """Close the gripper; picks up a block if one is between the jaws."""
        self._plan("close")
        self._log("close")
        tip = self._sim.tip()
        best, best_d = None, 0.03
        for name in BLOCKS:
            p = self._sim.d.xpos[self._sim.m.body(name).id]
            dist = float(np.linalg.norm(p - (tip + np.array([0, 0, -0.005]))))
            if dist < best_d:
                best, best_d = name, dist
        self._sim.move_joints(self._sim.arm_q(), 1.0, 0.35 if best else GRIP_CLOSED)
        if best:
            self._sim.held = best
            self._sim.held_offset = self._sim.d.xpos[self._sim.m.body(best).id] - self._sim.tip()
            self._log("grasped", object=best)
        self._sim.step_for(0.2)

    def home(self):
        self._plan("home", speed=float(self.speed))
        self._log("home")
        self._sim.move_joints(self._sim.home_q, self.speed)

    def wait(self, seconds: float):
        self._plan("wait", seconds=float(seconds))
        self._log("wait", seconds=seconds)
        self._sim.step_for(min(float(seconds), 5.0))

    def joints(self, shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, speed=None):
        """Move straight to joint angles (radians). For advanced programs."""
        q = np.array([shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll], dtype=float)
        self._plan("joints", q=[float(v) for v in q], speed=float(speed or self.speed))
        self._log("joints", q=[round(float(v), 3) for v in q], speed=speed or self.speed)
        self._sim.move_joints(q, speed or self.speed)


class Scene:
    """Read-only view of the bench."""

    def __init__(self, sim: Sim):
        self._sim = sim

    def find(self, name: str) -> tuple[float, float, float]:
        """Position (x, y, z) of an object's centre: 'blue_block', 'red_block', 'yellow_block' or 'tray'."""
        key = name.strip().lower().replace(" ", "_")
        if key in ("blue", "red", "yellow"):
            key += "_block"
        p = self._sim.d.xpos[self._sim.m.body(key).id]
        return (round(float(p[0]), 4), round(float(p[1]), 4), round(float(p[2]), 4))

    def objects(self) -> list[str]:
        return BLOCKS + ["tray"]


def replay(arm: "Arm", plan: list[dict]):
    """Run a validated command list. No model-written code is involved."""
    for c in plan:
        cmd = c["cmd"]
        if cmd == "move_to":
            arm.move_to(c["x"], c["y"], c["z"], c.get("speed"))
        elif cmd == "open":
            arm.open()
        elif cmd == "close":
            arm.close()
        elif cmd == "home":
            arm.speed = c.get("speed", arm.speed)
            arm.home()
        elif cmd == "wait":
            arm.wait(c["seconds"])
        elif cmd == "joints":
            arm.joints(*c["q"], speed=c.get("speed"))
        else:
            raise StopProgram(f"unknown command {cmd!r}")


def main(job: Path, mode: str):
    """mode 'plan': run the untrusted program, write plan.json (its arm commands). No video.
       mode 'replay': run job/plan.json (validated by the host) in a fresh sim, write trajectory.json + sim.mp4."""
    t0 = time.time()
    sim = Sim(SCENE, record_video=(mode == "replay"), video_path=job / "sim.mp4")
    arm, scene = Arm(sim), Scene(sim)
    status, error = "finished", None
    try:
        if mode == "plan":
            source = (job / "program.py").read_text()
            namespace = {"arm": arm, "scene": scene, "math": math, "__name__": "__program__"}
            exec(compile(source, "program.py", "exec"), namespace)
        else:
            replay(arm, json.loads((job / "plan.json").read_text())["plan"])
    except StopProgram as e:
        status, error = "stopped", str(e)
    except Exception as e:  # the program's own bugs or blocked actions are results, not crashes
        status = "error"
        error = "".join(traceback.format_exception_only(type(e), e)).strip()
        traceback.print_exc()
    if mode == "plan":
        (job / "plan.json").write_text(json.dumps({"status": status, "error": error, "plan": sim.plan}))
        print(json.dumps({"status": status, "error": error, "commands": len(sim.plan)}))
        return
    result = {
        "status": status,
        "error": error,
        "sim_seconds": round(sim.d.time, 2),
        "wall_seconds": round(time.time() - t0, 2),
        "events": sim.events,
        "frames": sim.frames,
        "final_positions": {n: list(scene.find(n)) for n in BLOCKS},
        "joint_names": ARM_JOINTS,
    }
    if sim.writer is not None:
        sim.writer.close()
    (job / "trajectory.json").write_text(json.dumps(result))
    print(json.dumps({"status": status, "error": error, "frames": len(sim.frames)}))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/job"), sys.argv[2] if len(sys.argv) > 2 else "plan")
