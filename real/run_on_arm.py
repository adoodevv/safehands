"""Run an APPROVED SafeHands plan on a real SO-101.

    python real/run_on_arm.py safehands-plan-<run>.json            # dry run: solve every move, move nothing
    python real/run_on_arm.py safehands-plan-<run>.json --go       # move the arm

Real-arm tuning (the simulation and the approved plan stay the same):
    --open 90          open the gripper wider (e.g. a bigger gripper mod); --closed sets the grip
    --lower-mm 8       grasp and place this much lower than the plan says
Heights are measured from the table: the table height under each point comes from say-and-grasp's
calibration (config/calib.json), since a real table rarely sits exactly at z = 0 of the arm model.

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
ROLL_RANGE = (-150.0, 155.0)            # wrist roll, a few degrees inside its stops
MAX_DPS = 60.0                          # never faster than this on the real arm, whatever the plan says
SCENE_BLOCKS = {"blue_block": (0.22, 0.08), "red_block": (0.24, -0.06), "yellow_block": (0.19, 0.13),
                "tray": (0.16, -0.18)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--go", action="store_true", help="actually move the arm")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--robot-id", default="my_follower")
    ap.add_argument("--open", type=float, default=GRIP_OPEN, help="gripper value when open (0-100)")
    ap.add_argument("--closed", type=float, default=GRIP_CLOSED, help="gripper value when closed (0-100)")
    ap.add_argument("--lower-mm", type=float, default=0.0, help="grasp and place this many mm lower")
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

    try:
        from say_and_grasp.calibration import load_calib, table_z_at
        calib = load_calib()
        table = lambda x, y: table_z_at(x, y, calib)
        print("Table height from say-and-grasp calibration "
              f"(at the first block: {table(*SCENE_BLOCKS['blue_block']) * 1000:+.1f} mm)")
    except Exception as e:  # no calibration: assume the table is at z = 0, like the simulator
        table = lambda x, y: 0.0
        print(f"No table calibration ({type(e).__name__}); assuming the table is at z = 0")
    low = args.lower_mm / 1000.0
    print(f"Gripper open {args.open:.0f}, closed {args.closed:.0f}; grasp/place {args.lower_mm:.0f} mm lower than planned")

    kin = Kinematics()
    # Solve every move first: if any target is unreachable on the real arm, stop before moving.
    solved = []
    roll = None
    for i, c in enumerate(plan):
        if c["cmd"] == "move_to":
            # the plan's z is above the simulator's table (z = 0): put it above the real table instead.
            # The low moves (grasp and place, under 6 cm) also go --lower-mm further down.
            z = c["z"] + table(c["x"], c["y"]) - (low if c["z"] < 0.06 else 0.0)
            r = kin.grasp(c["x"], c["y"], z, 0.0)
            if not r.ok:
                sys.exit(f"Command {i} move_to({c['x']:.3f}, {c['y']:.3f}, {z:.3f}) is not reachable "
                         f"on this arm (error {r.pos_err * 1000:.0f} mm). Nothing moved.")
            q = [float(v) for v in r.joints]
            # The jaws are symmetric: turning the wrist half a turn grips the same way. Pick whichever
            # is closer to the last move, so the wrist doesn't spin round between steps.
            if roll is not None:
                for alt in (q[4] - 180.0, q[4] + 180.0):
                    if ROLL_RANGE[0] <= alt <= ROLL_RANGE[1] and abs(alt - roll) < abs(q[4] - roll):
                        q[4] = alt
            roll = q[4]
            solved.append((c, q))
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
        arm.leave_home(gripper=args.open)
        for c, q in solved:
            speed = min(MAX_DPS, math.degrees(c.get("speed", 1.0)))
            arm.speed = speed
            if c["cmd"] in ("move_to", "joints"):
                arm.move(q, settle_s=0.3)
            elif c["cmd"] == "open":
                arm.move(arm.q_cmd, gripper=args.open, settle_s=0.2)
            elif c["cmd"] == "close":
                arm.move(arm.q_cmd, gripper=args.closed, settle_s=0.3)
            elif c["cmd"] == "home":
                arm.go_home(gripper=args.open)
            elif c["cmd"] == "wait":
                time.sleep(c["seconds"])
        arm.go_home(gripper=args.closed)
    except KeyboardInterrupt:
        print("Stopped. Going home.")
        arm.go_home()
    finally:
        arm.disconnect()


if __name__ == "__main__":
    main()
