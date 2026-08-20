# syntax=docker/dockerfile:1
#
# One image, three entry points -- sturnus-bot, sturnus-link, sturnus-worker
# (Spec 13.2). Building three images from the same codebase would triple
# build time, scanning, and registry storage without separating anything
# the deployment does not already separate.

# ---------------------------------------------------------------------------
# Builder: resolve and install dependencies with uv. Nothing from this stage
# other than the resulting virtual environment and the source tree reaches
# the runtime image.
# ---------------------------------------------------------------------------
# Pinned by digest, not just tag: `python:3.12-slim` is a moving tag, and a
# digest makes the build reproducible and stops a routine re-pull from
# silently swapping the base image out from under this Dockerfile. Obtained
# via `docker pull python:3.12-slim && docker inspect --format
# '{{index .RepoDigests 0}}' python:3.12-slim` on 2026-08-19; re-derive the
# same way to move to a newer base.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

# Pin the same uv release the lockfile was produced with (see
# [build-system] in pyproject.toml).
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --frozen: install exactly what uv.lock records, never re-resolve.
# --no-default-groups: only [project.dependencies] -- the `lint` and `test`
# dependency-groups (mypy, ruff, pytest, testcontainers, ...) never reach
# the image.
RUN uv sync --frozen --no-default-groups

# ---------------------------------------------------------------------------
# Runtime: a slim image with the built virtual environment and source only.
# No compiler, no uv, no build tools.
# ---------------------------------------------------------------------------
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS runtime

# libopus0 is a runtime library, not a build tool: discord.py's voice-receive
# path (discord.ext.voice_recv -> discord.opus.Decoder) loads Opus via
# ctypes.util.find_library() against the system library. PyAV ships its own
# copy for its own use, but the decoder above never looks there.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

# Non-root: the bot only ever writes to the mounted volume below, so it
# never needs root to do its job.
RUN groupadd --gid 1000 sturnus \
    && useradd --uid 1000 --gid sturnus --home-dir /home/sturnus --create-home sturnus

WORKDIR /app

COPY --from=builder --chown=sturnus:sturnus /app/.venv /app/.venv
COPY --chown=sturnus:sturnus src ./src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/data/cache/huggingface

# The Whisper model is deliberately not part of this image -- it is
# downloaded on first use and cached here instead, the same pattern the
# cluster's Ollama installation already uses. A baked-in model would add
# well over a gigabyte to every pull. `HF_HOME` is what `faster-whisper`
# actually honours: it never sets `download_root` itself
# (src/sturnus/infrastructure/whisper.py), so `WhisperModel` falls through
# to `huggingface_hub`, whose cache directory resolves from `HF_HOME`
# (huggingface_hub.constants.HF_HOME / HF_HUB_CACHE).
#
# `/data` is the mounted volume: recordings under `STURNUS_RECORDING_DIR`
# and the model cache above both live under it, so neither is lost on
# restart and neither needs baking into the image (Spec 13.2).
RUN mkdir -p /data && chown sturnus:sturnus /data
VOLUME ["/data"]

USER sturnus

# No default ENTRYPOINT/CMD: the three console scripts installed by
# `uv sync` above (sturnus-bot, sturnus-link, sturnus-worker) are the
# process a deployment picks by naming one as the container command, e.g.
# `docker run sturnus sturnus-bot`.
