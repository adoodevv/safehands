# The sandbox image: just enough to simulate the arm. No network tools, no secrets, no shell tricks needed.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libosmesa6 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "mujoco==3.3.*" "numpy<3" imageio imageio-ffmpeg

# Unprivileged user that every run uses (uid 1000, no home, no sudo)
RUN groupadd -g 1000 sandbox && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin sandbox

ENV MUJOCO_GL=osmesa PYOPENGL_PLATFORM=osmesa PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# runner.py and the scene are mounted read-only at run time, so the image never holds program code.
USER 1000:1000
