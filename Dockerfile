# syntax=docker/dockerfile:1.4
#
# Collabora Online (CODE) backend with a built-in file manager UI for OpenHost.
#
# Layout inside the container:
#   - coolwsd (collabora editor backend) on 127.0.0.1:9980  [loopback only]
#   - Quart UI + WOPI host on 0.0.0.0:8080                  [exposed]
#   - openhost-entrypoint.py starts both, forwards SIGTERM
#
# The Quart app reverse-proxies /browser/ /cool/ /lool/ /hosting/ to the
# loopback coolwsd, so the user-visible URL is a single origin.
#
# Since 26.04.2.2 the collabora/code image is built with Nix and has no shell
# and no package manager, so nothing can RUN in it.  Everything we add is
# built in the `python` stage (Debian trixie, the same glibc 2.41 the
# Collabora image ships) and COPYed into the unchanged upstream image:
#   - a standalone CPython (python-build-standalone, installed by uv, which
#     checks its sha256); it only needs glibc, which the Collabora image has;
#   - a venv with the Quart app's deps;
#   - the app, the blank-document templates, and the entrypoint.

# ----------------------------------------------------------------------
# Stage 1: Python runtime + app.  Pinned by tag and multi-arch index digest.
# ----------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.13.4-trixie-slim@sha256:2d5298090a57b670458ccae215742352f753f3d583db23f3cd35081d26859ada AS python

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy

# Exact versions; bump deliberately.
RUN uv python install 3.12.15 \
 && uv venv --python 3.12.15 /opt/openhost-venv \
 && uv pip install --python /opt/openhost-venv/bin/python \
        "quart==0.20.0" \
        "hypercorn==0.17.3" \
        "httpx==0.28.1" \
        "websockets==14.2"

# Build the blank-document seeds the UI ships as "+ New …" templates
# (static .odt/.ods/.odp written by a small Python script).
COPY scripts/generate-blank-templates.sh /tmp/generate-blank-templates.sh
RUN PATH="/opt/openhost-venv/bin:$PATH" bash /tmp/generate-blank-templates.sh

# The Quart app sources.  The blank_templates symlink points at the
# build-time output so the app finds them via a relative path.
COPY app /opt/openhost-app
RUN ln -s /opt/collabora-blank-templates /opt/openhost-app/blank_templates \
 && chown -R 1001:1001 /opt/openhost-app

# ----------------------------------------------------------------------
# Stage 2: the upstream image, unchanged, plus our files.
# Pinned by tag and multi-arch index digest; bump both together.
# ----------------------------------------------------------------------
FROM docker.io/collabora/code:26.04.5.1.1@sha256:bb8f3c9c5c9016045f25fccb5f0649be94469ebc7c7cc379f3576e470cccf33d

# Same paths as in the build stage: the venv's interpreter is a symlink
# into /opt/python.
COPY --from=python /opt/python /opt/python
COPY --from=python /opt/openhost-venv /opt/openhost-venv
COPY --from=python /opt/collabora-blank-templates /opt/collabora-blank-templates
COPY --from=python /opt/openhost-app /opt/openhost-app
COPY openhost-entrypoint.py /opt/openhost-entrypoint.py

# Upstream's "cool" user (uid 1001) owns /etc/coolwsd and /opt/cool; the
# upstream image already runs as it, and so do we.
USER 1001

ENV PATH="/opt/openhost-venv/bin:/usr/bin:/bin"

# Upstream's HEALTHCHECK runs `coolwsd --probe` with coolwsd's default
# config, not the settings our entrypoint passes, so drop it.  (podman's
# default OCI image format ignores HEALTHCHECK anyway.)
HEALTHCHECK NONE

# 8080 is the Quart UI / proxy.  Coolwsd's 9980 stays on loopback only.
EXPOSE 8080

ENTRYPOINT ["/opt/openhost-venv/bin/python", "/opt/openhost-entrypoint.py"]
