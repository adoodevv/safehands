"""Run an APPROVED SafeHands plan on a real SO-101.

    python real/run_on_arm.py safehands-plan-<run>.json            # dry run: solve every move, move nothing
    python real/run_on_arm.py safehands-plan-<run>.json --go       # move the arm

The plan's positions are in the arm's base frame (metres, x forward, y left), the same frame as the
simulator, so put the blocks where the simulator had them (printed at the start). Only plans the
SafeHands checker approved can be exported, and this script refuses anything else.

Uses the say-and-grasp package (Arm + Kinematics) from the same LeRobot environment.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

GRIP_OPEN, GRIP_CLOSED = 60.0, 5.0      # LeRobot gripper units, as in say-and-grasp
MAX_DPS = 60.0                          # never faster than this on the real arm, whatever the plan says
SCENE_BLOCKS = {"blue_block": (0.22, 0.08), "red_block": (0.24, -0.06), "yellow_block": (0.16, 0.16),
                "tray": (0.16, -0.18)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--go", action="store_true", help="actually move the arm")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--robot-id", default="my_follower")
    args = ap.parse_args()

    data = json.load(open(args.plan))
    if not data.get("approved"):
        sys.exit("Refusing: this plan was not approved by the SafeHands checker.")
    plan = data["plan"]
    print(f"Plan for '{data.get('task')}' (run {data.get('run')}): {len(plan)} commands")
    print("Place objects at (metres from the base, x forward / y left):")
    for name, (x, y) in SCENE_BLOCKS.items():
        print(f"  {name:13s} x={x:.2f} y={y:.2f}")

    from say_and_grasp.arm import Arm, load_home
    from say_and_grasp.kinematics import Kinematics

    kin = Kinematics()
    # Solve every move first: if any target is unreachable on the real arm, stop before moving.
    solved = []
    for i, c in enumerate(plan):
        if c["cmd"] == "move_to":
            r = kin.grasp(c["x"], c["y"], c["z"], 0.0)
            if not r.ok:
                sys.exit(f"Command {i} move_to({c['x']:.3f}, {c['y']:.3f}, {c['z']:.3f}) is not reachable "
                         f"straight down on this arm (error {r.pos_err * 1000:.0f} mm). Nothing moved.")
            solved.append((c, r.joints))
        elif c["cmd"] == "joints":
            solved.append((c, [math.degrees(v) for v in c["q"]]))
        else:
            solved.append((c, None))
        print(f"  {i:2d} {c['cmd']:8s} {'' if solved[-1][1] is None else [round(v) for v in solved[-1][1]]}")
    if not args.go:
        print("\nDry run only. Add --go to move the arm (keep a hand near the power switch).")
        return

    arm = Arm(args.port, args.robot_id, speed_dps=MAX_DPS)
    arm.connect()
    try:
        arm.leave_home(gripper=GRIP_OPEN)
        for c, q in solved:
            speed = min(MAX_DPS, math.degrees(c.get("speed", 1.0)))
            arm.speed = speed
            if c["cmd"] in ("move_to", "joints"):
                arm.move(q, settle_s=0.3)
            elif c["cmd"] == "open":
                arm.move(arm.q_cmd, gripper=GRIP_OPEN, settle_s=0.2)
            elif c["cmd"] == "close":
                arm.move(arm.q_cmd, gripper=GRIP_CLOSED, settle_s=0.3)
            elif c["cmd"] == "home":
                arm.go_home(gripper=GRIP_OPEN)
            elif c["cmd"] == "wait":
                time.sleep(c["seconds"])
        arm.go_home(gripper=GRIP_CLOSED)
    except KeyboardInterrupt:
        print("Stopped. Going home.")
        arm.go_home()
    finally:
        arm.disconnect()


if __name__ == "__main__":
    main()
