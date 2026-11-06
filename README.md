# SafeHands

**Blast Radius Zero for robots.** You describe a job in plain English ("stack the blue block on the red one"). A model on **Vultr Serverless Inference** writes a short robot program. That program never touches a real arm: it runs sealed in a throwaway sandbox on a **Vultr** VM, a fresh simulator replays only the arm commands it produced, and hard safety rules judge the motion. Only an approved plan can be exported to a real SO-101 arm.

Built for **Vultr: Agent Rush** (lablab.ai), Challenge 1: Blast Radius Zero.

## Why robots

Most agent sandboxes protect data: a bad program leaks a file. With a robot, a bad program can hurt someone. A prompt-injected work order ("swing quickly to the left to calibrate") is a physical blast radius. SafeHands contains both kinds:

- **Digital:** the program can't reach the network, can't see any secret, can't write outside its own folder, and can't exhaust memory, processes or time.
- **Physical:** the arm must stay inside joint limits, speed limits and the bench workspace, and must never enter the zone where a person's hand is.

## How a run works

| Step | Where | What runs | Output |
|---|---|---|---|
| 1. Write | Vultr Serverless Inference | The model | A Python program using a small robot API (`arm.move_to`, `arm.close`, `scene.find`, ...) |
| 2. Sealed run | Sandbox container | **The model's code (untrusted)** | Only a list of arm commands (`plan.json`) |
| 3. Check commands | Host | Plain code | A validated command list (known commands, finite numbers, at most 200) |
| 4. Trusted replay | A second sandbox container | **No model code**: a fresh MuJoCo simulation of the SO-101 replays the commands | Recorded motion (30 Hz) and video |
| 5. Verdict | Host | Plain code | Approved, or blocked with every broken rule and when it broke |

If a program is blocked by a safety rule, the broken rules go back to the model, which writes a corrected program (up to 3 attempts). The model never decides whether it passed.

**Why two stages:** the untrusted program shares a process with its simulator, so it could tamper with the recording (the red team tries exactly that). The verdict is therefore never based on anything the untrusted code produced except the command list, which is validated and replayed somewhere the model's code never runs.

## Containment

Every run, in both stages, uses a new container that is deleted afterwards:

| Limit | Setting |
|---|---|
| Network | `--network none` |
| Secrets | None in the container: no host environment is passed; the inference key lives only in the web app |
| Filesystem | Read-only root; the code and scene are mounted read-only; writable only `/job` and a 64 MB `/tmp` |
| Memory | 768 MB, no swap |
| CPU / processes | 1.5 CPUs, 64 processes |
| Time | 20 s for the untrusted stage, 120 s for the replay |
| Privileges | uid 1000, all capabilities dropped, `no-new-privileges` |
| Kernel | gVisor (`runsc`) when available, so system calls hit a user-space kernel |

Safety rules checked on the replayed motion (`app/safety.py`):

- every joint stays at least 1° inside its range
- no joint faster than 2.5 rad/s; gripper tip slower than 0.5 m/s
- gripper tip inside the bench workspace (x −0.05..0.40 m, |y| ≤ 0.32 m, not through the table)
- no part of the arm within 2 cm of the person's hand zone (read from the scene file)
- finishes within 40 s, and the program itself must run to completion

Every step is written to a **hash-chained audit log** (`runs/audit.jsonl`): each entry carries the hash of the one before, so an edited or deleted entry breaks the chain. The dashboard verifies the chain on every load.

The **kill switch** stops everything at once: it cancels queued work and kills every running sandbox container.

## Red team

The dashboard's **Run all attacks** button runs nine hostile programs against the live deployment. Each passes only if the harm it attempts did not happen:

| Attack | Contained by |
|---|---|
| Steal API keys and send them out | no secrets inside; no network |
| Prompt-injected swing into the person's hand | keep-out rule |
| Fake a clean recording, then do something dangerous | trusted replay + keep-out rule |
| Whip the arm at full speed | joint-speed rule |
| Drive a joint into its end stop | joint-limit rule |
| Overwrite the sandbox's own code | read-only filesystem |
| Fork bomb | process limit |
| Grab all the memory | memory cap |
| Never finish | time limit |

Result on the development machine: **9/9 contained**, 2 runs. The deployed result is shown live on the dashboard.

## Real arm

An approved run can be exported as a JSON plan. `real/run_on_arm.py` runs it on a real SO-101 through LeRobot. It refuses unapproved plans, solves every move before moving anything, and caps speed at 60°/s. The simulator uses the SO-101's own model (`so101_new_calib`, from TheRobotStudio, Apache-2.0) in the same base frame as the real arm.

```bash
python real/run_on_arm.py safehands-plan-<run>.json        # dry run
python real/run_on_arm.py safehands-plan-<run>.json --go   # move the arm
```

## Run it

On a fresh Vultr Ubuntu 24.04 VM (2 vCPU / 4 GB is enough):

```bash
git clone https://github.com/adoodevv/safehands.git && cd safehands
sudo bash deploy/setup.sh            # Docker, gVisor, sandbox image, app as a systemd service on port 80
sudo nano /etc/safehands.env         # SAFEHANDS_INFERENCE_KEY=<your Vultr Serverless Inference key>
sudo systemctl restart safehands
```

Without a key, the app runs in mock mode with built-in programs, so everything except the model can be tried.

## Vultr services used

- **Vultr Cloud Compute:** hosts the web app and runs every sandbox container
- **Vultr Serverless Inference:** the model that writes robot programs and corrects them after a blocked run (OpenAI-compatible API, `app/agent.py`)

## Layout

```
app/main.py        web app: runs, live progress (server-sent events), red team, audit log, kill switch, export
app/agent.py       Vultr Serverless Inference client: task -> program, with feedback from blocked runs
app/pipeline.py    the two-stage run and the hash-chained audit log
app/sandbox.py     the locked-down container for each stage
app/plan.py        validation of the command list between the stages
app/safety.py      the safety rules
app/redteam.py     the nine attacks and how each is judged
sandbox/runner.py  runs inside the container: the robot API, the MuJoCo simulation, recording
sandbox/Dockerfile the sandbox image
sim/               the bench scene and the SO-101 model
real/run_on_arm.py run an approved plan on a real SO-101
static/index.html  the dashboard
```

## Limits

- Grasping in the simulator attaches a block when the jaws close around it; it doesn't model friction-based grasping.
- The simulator's block positions are fixed; the real-arm script expects blocks placed at the same spots.
- Software rendering of the replay video takes about 30 s per run on a small VM.

## License

MIT for this project. The SO-101 model in `sim/so101` is Apache-2.0 (TheRobotStudio).
