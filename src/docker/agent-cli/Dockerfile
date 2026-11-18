
# docker build -t ai-scientist-agent:v3.0.0 -f src/docker/agent-cli/Dockerfile .

# cd AI-Scientist-v3
# uv sync
# source .venv/bin/activate
# export OPENAI_API_KEY="你的API_KEY"
# agsci doctor examples/circpack
# uv run agsci run examples/circpack container.enabled=true container.image=ai-scientist-agent:v3.0.0 container.network=bridge paths.results_root=./results

FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim

ARG TARGETARCH

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG DEBIAN_FALLBACK_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_FALLBACK_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
ENV NPM_CONFIG_REGISTRY=https://registry.npmmirror.com

# BuildKit retains downloads between builds without adding them to the image.
# APT cache mounts need locks so concurrent builds do not modify them together.
RUN --mount=type=cache,id=agent-cli-apt-cache-${TARGETARCH},target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=agent-cli-apt-lists-${TARGETARCH},target=/var/lib/apt/lists,sharing=locked \
    set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    printf 'APT::Keep-Downloaded-Packages "true";\n' > /etc/apt/apt.conf.d/keep-cache; \
    . /etc/os-release; \
    codename="${VERSION_CODENAME:-bookworm}"; \
    rm -f /etc/apt/sources.list; \
    find /etc/apt/sources.list.d -type f \( -name '*.list' -o -name '*.sources' \) -delete; \
    write_apt_sources() { \
      mirror="$1"; \
      security_mirror="$2"; \
      printf 'deb %s %s main\n' "$mirror" "$codename" > /etc/apt/sources.list; \
      printf 'deb %s %s-updates main\n' "$mirror" "$codename" >> /etc/apt/sources.list; \
      printf 'deb %s %s-security main\n' "$security_mirror" "$codename" >> /etc/apt/sources.list; \
    }; \
    apt_update_with_retries() { \
      apt-get update \
        -o APT::Update::Error-Mode=any \
        -o Acquire::ForceIPv4=true \
        -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30; \
    }; \
    write_apt_sources "$DEBIAN_MIRROR" "$DEBIAN_SECURITY_MIRROR"; \
    if ! apt_update_with_retries; then \
      echo "Primary APT mirrors unreachable; retrying with fallback mirrors" >&2; \
      write_apt_sources "$DEBIAN_FALLBACK_MIRROR" "$DEBIAN_FALLBACK_SECURITY_MIRROR"; \
      apt_update_with_retries; \
    fi; \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      chromium \
      curl \
      git \
      jq \
      procps \
      texlive-bibtex-extra \
      texlive-fonts-recommended \
      texlive-latex-base \
      texlive-latex-extra \
      texlive-latex-recommended

RUN --mount=type=cache,id=agent-cli-npm-${TARGETARCH},target=/root/.npm \
    npm install -g \
      @openai/codex \
      @anthropic-ai/claude-code \
      @google/gemini-cli \
      @earendil-works/pi-coding-agent \
      opencode-ai

ARG HEADLESS_COMMIT=93cd9b06b85f848af1308c41e018991b33907c5e
RUN --mount=type=cache,id=agent-cli-npm-${TARGETARCH},target=/root/.npm \
    git clone --filter=blob:none https://github.com/RobertTLange/headless-cli.git /opt/headless \
    && git -C /opt/headless checkout --detach "$HEADLESS_COMMIT" \
    && test "$(git -C /opt/headless rev-parse HEAD)" = "$HEADLESS_COMMIT" \
    && npm ci --ignore-scripts --prefix /opt/headless \
    && npm run build --prefix /opt/headless \
    && npm prune --ignore-scripts --omit=dev --prefix /opt/headless \
    && ln -s /opt/headless/dist/cli.js /usr/local/bin/headless \
    && headless --version \
    && for agent in codex claude opencode pi; do \
      headless "$agent" --debug --usage --allow yolo --print-command \
        --prompt runtime-contract-smoke >/dev/null; \
    done
ENV HEADLESS_BIN=/usr/local/bin/headless \
    AUTOILLUSTRATE_HEADLESS_BIN=/usr/local/bin/headless

RUN --mount=type=cache,id=agent-cli-pip-${TARGETARCH},target=/root/.cache/pip \
    pip install "scholarly==1.7.11" "httpx==0.27.2"

ARG DRAWIO_VERSION=32.7.4
RUN --mount=type=cache,id=agent-cli-apt-cache-${TARGETARCH},target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=agent-cli-apt-lists-${TARGETARCH},target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=agent-cli-drawio-${TARGETARCH},target=/var/cache/drawio,sharing=locked \
    set -eux; \
    drawio_arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$drawio_arch" in \
      amd64|arm64) ;; \
      *) echo "Unsupported Draw.io architecture: $drawio_arch" >&2; exit 1 ;; \
    esac; \
    apt-get update -o APT::Update::Error-Mode=any -o Acquire::ForceIPv4=true -o Acquire::Retries=5; \
    drawio_cache="/var/cache/drawio/drawio-${drawio_arch}-${DRAWIO_VERSION}.deb"; \
    if [ -s "$drawio_cache" ]; then \
      cp "$drawio_cache" /tmp/drawio.deb; \
    else \
      curl -fsSL --retry 5 -o /tmp/drawio.deb \
        "https://github.com/jgraph/drawio-desktop/releases/download/v${DRAWIO_VERSION}/drawio-${drawio_arch}-${DRAWIO_VERSION}.deb"; \
    fi; \
    cp /tmp/drawio.deb "$drawio_cache"; \
    apt-get install -y /tmp/drawio.deb xvfb xauth \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcups2 \
      libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
      libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 \
      libxshmfence1 libx11-xcb1 libgdk-pixbuf-2.0-0 \
      libcairo2; \
    rm -f /tmp/drawio.deb; \
    { \
      echo '#!/bin/sh'; \
      echo 'for d in /usr/bin/drawio /opt/drawio/drawio; do'; \
      echo '  [ -x "$d" ] && exec xvfb-run -a --server-args="-screen 0 5000x5000x24" "$d" --no-sandbox --disable-gpu "$@";'; \
      echo 'done'; \
      echo 'echo "drawio: desktop binary not found" >&2; exit 127'; \
    } > /usr/local/bin/drawio; \
    chmod 0755 /usr/local/bin/drawio

# Install the pinned Autoillustrate npm release and bake its hashed Python
# dependencies so paper runs need no runtime package installation. The toolkit
# (prompts, scripts, assets, requirements.txt) ships inside the package.
# The paper orchestrator selects the agent and model. Standalone calls can
# select the agent with --agent.
ARG AUTOILLUSTRATE_VERSION=0.4.8
ARG AUTOILLUSTRATE_HOME=/usr/local/lib/node_modules/autoillustrate
RUN --mount=type=cache,id=agent-cli-npm-${TARGETARCH},target=/root/.npm \
    --mount=type=cache,id=agent-cli-pip-${TARGETARCH},target=/root/.cache/pip \
    npm install -g "autoillustrate@${AUTOILLUSTRATE_VERSION}" \
    && test "$(autoillustrate --version)" = "$AUTOILLUSTRATE_VERSION" \
    && python -m venv /opt/autoillustrate-venv \
    && /opt/autoillustrate-venv/bin/pip install --require-hashes \
      -r "$AUTOILLUSTRATE_HOME/requirements.txt"
ENV AUTOILLUSTRATE_CHROME=/usr/bin/chromium
ENV VENV_PY=/opt/autoillustrate-venv/bin/python

RUN useradd -m -u 1000 agent \
    && chmod 755 /home/agent
USER agent
ENV PATH="/home/agent/.local/bin:${PATH}"
RUN test "$(autoillustrate --version)" = "$AUTOILLUSTRATE_VERSION" \
    && test -r "$AUTOILLUSTRATE_HOME/package.json" \
    && test -r "$AUTOILLUSTRATE_HOME/requirements.txt" \
    && test -r "$AUTOILLUSTRATE_HOME/prompts/phase1.md" \
    && test -r "$AUTOILLUSTRATE_HOME/scripts/compose.py" \
    && test -r "$AUTOILLUSTRATE_HOME/assets/panel_geometry.py"
RUN curl -fsSL https://cursor.com/install | bash
WORKDIR /workspace

CMD ["bash"]
