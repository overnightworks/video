# The service and the FlashHead Lite provider with its pinned weights, as one image.
# Both base images are held by digest, because a tag is a name its owner may move;
# Dependabot's docker entry moves the digest through review, and it reads only FROM
# lines, so uv is a stage of its own.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS python-base
ENV PATH=/opt/venv/bin:$PATH

# Both environments run the image's own interpreter, so uv never downloads another,
# and each is built at the path it runs from. Hash-based bytecode stays valid when
# the release's reproducible layers rewrite every file time. uv and git exist only in
# these stages: git because uv fetches the git-pinned webauth and logline.
FROM python-base AS build-base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1
RUN apt-get update \
 && apt-get install --yes --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The service without development dependencies, so without the fake provider.
# Third-party packages are wheel-only: --no-build refuses a source distribution,
# whose build script would run third-party code during the image build. The
# repository's own distributions and the git-pinned webauth and logline are sources and
# build in the next step, with the exact build tools their pyprojects pin.
FROM build-base AS service-environment
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY packages/client/pyproject.toml packages/client/pyproject.toml
COPY providers/provider-contract/pyproject.toml providers/provider-contract/pyproject.toml
COPY providers/fake/pyproject.toml providers/fake/pyproject.toml
RUN uv sync --locked --no-dev --no-editable --no-install-project --no-build \
    --no-install-package overnightworks-webauth \
    --no-install-package overnightworks-logline \
    --no-install-package overnightworks-video-client \
    --no-install-package overnightworks-video-provider-contract
COPY LICENSE LICENSE
COPY packages/client/src packages/client/src
COPY providers/provider-contract/src providers/provider-contract/src
COPY src src
RUN uv sync --locked --no-dev --no-editable \
 && python -m compileall -q -j 0 --invalidation-mode checked-hash /opt/venv/lib

# The provider is its own project on its own lock, in the environment the service
# looks for. The wheel packs upstream's flash_head tree and both licence texts.
# NVIDIA's libraries are the largest part of the environment, and a layer stays under
# 10 GB: they move out, so the final stage copies them as a layer of their own and the
# rest of the environment as another.
FROM build-base AS provider-environment
ENV UV_PROJECT_ENVIRONMENT=/opt/providers/flashhead-lite/.venv
WORKDIR /build/providers/flashhead-lite
COPY providers/flashhead-lite/pyproject.toml providers/flashhead-lite/uv.lock ./
COPY providers/provider-contract/pyproject.toml ../provider-contract/pyproject.toml
RUN uv sync --locked --no-dev --no-editable --no-install-project --no-build \
    --no-install-package overnightworks-video-provider-contract
COPY providers/provider-contract/src ../provider-contract/src
# The provider's LICENSE is a link to the root's.
COPY LICENSE ../../LICENSE
COPY providers/flashhead-lite/LICENSE LICENSE
COPY providers/flashhead-lite/soulx-flashhead/LICENSE soulx-flashhead/LICENSE
COPY providers/flashhead-lite/soulx-flashhead/flash_head soulx-flashhead/flash_head
COPY providers/flashhead-lite/src src
RUN uv sync --locked --no-dev --no-editable \
 && /opt/providers/flashhead-lite/.venv/bin/python -m compileall -q -j 0 --invalidation-mode checked-hash /opt/providers/flashhead-lite/.venv/lib \
 && mv /opt/providers/flashhead-lite/.venv/lib/python3.14/site-packages/nvidia /opt/flashhead-nvidia

# The pinned weights and notices, fetched next to the environment as the provider
# finds them. The Hub's own cache and the fetch's staging stay in this stage.
FROM provider-environment AS weights
RUN /opt/providers/flashhead-lite/.venv/bin/video-flashhead-lite-fetch-weights

FROM python-base
# Triton builds its CUDA utility module from C source at run time.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --no-create-home --uid 1001 video
COPY --from=weights /opt/providers/flashhead-lite/weights /opt/providers/flashhead-lite/weights
COPY --from=provider-environment /opt/flashhead-nvidia /opt/providers/flashhead-lite/.venv/lib/python3.14/site-packages/nvidia
COPY --from=provider-environment /opt/providers/flashhead-lite/.venv /opt/providers/flashhead-lite/.venv
COPY --from=service-environment /opt/venv /opt/venv
# The root is read-only at run time: the home and temporary directories are /tmp, where
# the compilers write their caches. The deployment sets the rest: the provider, the
# card memory cap, the caller file and the host.
ENV VIDEO_PROVIDER_ROOT=/opt/providers \
    HOME=/tmp \
    TMPDIR=/tmp
USER 1001

CMD ["video-service"]
