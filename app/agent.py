"""The program writer: turns a plain-English task into a short robot program, using a model on
Vultr Serverless Inference (OpenAI-compatible API). If the safety checker blocks a program, the
writer gets the broken rules back and tries again.

Without SAFEHANDS_INFERENCE_KEY it runs in mock mode with canned programs, so the app works offline.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

BASE = os.environ.get("SAFEHANDS_INFERENCE_URL", "https://api.vultrinference.com/v1")
KEY = os.environ.get("SAFEHANDS_INFERENCE_KEY", "")
MODEL = os.environ.get("SAFEHANDS_MODEL", "")

SYSTEM = """You write short Python programs that control a small robot arm (SO-101) on a workbench.
Reply with ONE ```python code block and nothing else.

Available objects (already defined, do not import anything):
  scene.find(name) -> (x, y, z)   centre of an object in metres. Names: "blue_block", "red_block",
                                  "yellow_block", "tray". Blocks are 25 mm cubes resting on the bench (z = 0.0125).
  scene.objects() -> list of names
  arm.move_to(x, y, z, speed=None)  move the gripper tip to (x, y, z), pointing straight down.
                                    speed is the fastest joint's speed in rad/s (default 1.0; keep it at or below 1.5).
  arm.open()     open the gripper (releases anything held)
  arm.close()    close the gripper (picks up a block if the tip is at its centre)
  arm.home()     return to the folded rest pose
  arm.wait(seconds)
  math           the math module

Frame: x points forward from the arm's base, y to the left, z up. Units are metres.
To pick a block at (x, y, z): move to (x, y, z + 0.06), then to (x, y, z), close, then lift to z + 0.08.
To place on another block at (x2, y2, z2): move above it (z2 + 0.08), lower to z2 + 0.028, open, lift.
To place in the tray at (tx, ty, tz): move above (tz + 0.10), lower to tz + 0.03, open, lift.
Finish with arm.home().
Safety rules your program will be checked against: never enter the person's hand zone (a cylinder of
radius 7 cm around x=0.10, y=0.24 on the bench), keep joints slow (<= 1.5), stay on the bench
(x 0..0.38, |y| <= 0.30), finish within 40 s."""

MOCK = {
    "stack": '''x, y, z = scene.find("blue_block")
arm.move_to(x, y, z + 0.06)
arm.move_to(x, y, z)
arm.close()
arm.move_to(x, y, z + 0.08)
rx, ry, rz = scene.find("red_block")
arm.move_to(rx, ry, rz + 0.08)
arm.move_to(rx, ry, rz + 0.028)
arm.open()
arm.move_to(rx, ry, rz + 0.08)
arm.home()
''',
    "tray": '''tx, ty, tz = scene.find("tray")
for name in ["blue_block", "red_block", "yellow_block"]:
    x, y, z = scene.find(name)
    arm.move_to(x, y, z + 0.06)
    arm.move_to(x, y, z)
    arm.close()
    arm.move_to(x, y, z + 0.10)
    arm.move_to(tx, ty, tz + 0.10)
    arm.move_to(tx, ty, tz + 0.03)
    arm.open()
    arm.move_to(tx, ty, tz + 0.10)
arm.home()
''',
}


def mock_program(task: str) -> str:
    t = task.lower()
    if "tray" in t or "tidy" in t or "clean" in t:
        return MOCK["tray"]
    return MOCK["stack"]


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def _get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


_model_cache: str | None = None


def model_id() -> str:
    """The configured model, or the best code-capable one the account offers."""
    global _model_cache
    if MODEL:
        return MODEL
    if _model_cache:
        return _model_cache
    ids = [m["id"] for m in _get("/models").get("data", [])]
    # skip models that make images, audio, speech, embeddings or safety labels: we need a text/code model
    not_text = ("image", "tts", "asr", "speech", "whisper", "voxtral", "music", "embedding", "reranker",
                "safety", "chatterbox", "ace-step")
    text_ids = sorted((i for i in ids if not any(w in i.lower() for w in not_text)), reverse=True)  # newest version first
    for pref in ("coder", "glm-5", "deepseek", "qwen3", "kimi", "minimax-m", "llama", "mistral", "gpt"):
        for i in text_ids:
            if pref in i.lower():
                _model_cache = i
                return i
    _model_cache = (text_ids or ids)[0]
    return _model_cache


def extract(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def write_program(task: str, feedback: list[dict] | None = None) -> dict:
    """Returns {'program', 'model', 'mock'}. `feedback` = earlier attempts and the rules they broke."""
    if not KEY:
        return {"program": mock_program(task), "model": "mock (no inference key set)", "mock": True}
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
    for fb in feedback or []:
        messages.append({"role": "assistant", "content": f"```python\n{fb['program']}```"})
        messages.append({"role": "user", "content":
                         "The safety checker BLOCKED that program. Broken rules:\n- " +
                         "\n- ".join(fb["broken"]) +
                         "\nWrite a corrected program that does the original task and breaks none of the rules."})
    mid = model_id()
    out = _post("/chat/completions", {"model": mid, "messages": messages, "max_tokens": 4000, "temperature": 0.2})
    msg = out["choices"][0]["message"]
    # reasoning models may put their thinking in a separate field; the program is in `content`
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return {"program": extract(text), "model": mid, "mock": False}
