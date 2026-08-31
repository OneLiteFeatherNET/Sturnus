# syntax=docker/dockerfile:1
#
# The console: a Nuxt server, in an image of its own.
#
# Separate from the Python image rather than a stage inside it, and not
# because of tidiness. The two fail differently -- a rendering process that
# has crashed is an outage of the page, an API process that has crashed is
# an outage of the data -- and they scale differently. Keeping them apart
# also keeps a Node runtime out of the Python image, which is the larger of
# the two by a wide margin.

# ---------------------------------------------------------------------------
# Builder: install dependencies and produce the Nitro output. Nothing from
# this stage other than `.output` reaches the runtime image -- no
# node_modules, no sources, no build cache.
# ---------------------------------------------------------------------------
# Pinned by digest for the same reason the Python image is: a bare tag is
# a moving target, and a routine re-pull must not swap the base image out
# from under this build. Obtained on 2026-08-22 via `docker pull node:26-slim
# && docker inspect --format '{{index .RepoDigests 0}}' node:26-slim`;
# re-derive the same way to move to a newer base.
FROM node:26-slim@sha256:c0753125a3789977aefe869cbebccf70e3cfd7ea84ca48547458f02e4f1d7146 AS builder

WORKDIR /build

# Copied first and installed on their own, so a change to a component does
# not invalidate the dependency layer.
COPY console/package.json console/package-lock.json ./
RUN npm ci

COPY console/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Runtime: the Nitro bundle and a Node runtime, nothing else.
# ---------------------------------------------------------------------------
FROM node:26-slim@sha256:c0753125a3789977aefe869cbebccf70e3cfd7ea84ca48547458f02e4f1d7146 AS runtime

# The base image already ships an unprivileged `node` user at uid 1000.
# Creating a second one collides with it, and there is nothing a
# purpose-named account would buy here: this process writes nothing at all
# -- it renders and calls the API -- so it has no reason to be able to.

WORKDIR /app
COPY --from=builder --chown=node:node /build/.output ./.output

ENV NODE_ENV=production \
    NITRO_PORT=3000 \
    NITRO_HOST=0.0.0.0

USER node
EXPOSE 3000

CMD ["node", ".output/server/index.mjs"]
