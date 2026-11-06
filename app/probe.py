"""Which model on this Vultr Serverless Inference account writes SafeHands programs best?

    sudo bash -c 'set -a; . /etc/safehands.env; set +a; .venv/bin/python -m app.probe'

Tries each text model once on the same task and prints: did it return a program, how long it took,
and how many tokens it spent thinking. Pick the winner with SAFEHANDS_MODEL in /etc/safehands.env.
"""
from __future__ import annotations

import time
import urllib.error

from . import agent

TASK = "Stack the blue block on the red one"
NOT_TEXT = ("image", "tts", "asr", "speech", "whisper", "voxtral", "music", "embedding", "reranker",
            "safety", "chatterbox", "ace-step")


def main():
    if not agent.KEY:
        raise SystemExit("SAFEHANDS_INFERENCE_KEY is not set (source /etc/safehands.env first)")
    ids = [m["id"] for m in agent._get("/models").get("data", [])]
    models = [i for i in ids if not any(w in i.lower() for w in NOT_TEXT)]
    print(f"{len(models)} text models: {', '.join(models)}\n")
    messages = [{"role": "system", "content": agent.SYSTEM}, {"role": "user", "content": TASK}]
    for mid in models:
        agent._think_ok = None
        t0 = time.time()
        try:
            out = agent.chat(mid, messages, max_tokens=4000, timeout=120)
            dt = time.time() - t0
            choice = out["choices"][0]
            content = (choice.get("message") or {}).get("content") or ""
            prog = agent.extract(content) if content.strip() else ""
            usage = out.get("usage") or {}
            think = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", "?")
            ok = "arm.move_to" in prog and "arm.close" in prog
            print(f"{'OK  ' if ok else 'BAD '} {mid:42s} {dt:5.1f}s  thinking={think}  out={usage.get('completion_tokens')}  "
                  f"finish={choice.get('finish_reason')}  knobs={agent._think_ok}")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError) as e:
            print(f"ERR  {mid:42s} {time.time() - t0:5.1f}s  {type(e).__name__}: {str(e)[:120]}")


if __name__ == "__main__":
    main()
