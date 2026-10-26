FROM node:24.22.0-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5
# Freeze Debian and security packages together with the formal Chromium version.
RUN printf '%s\n' \
    'Types: deb' \
    'URIs: http://snapshot.debian.org/archive/debian/20261024T000000Z/' \
    'Suites: bookworm bookworm-updates' \
    'Components: main' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
    'Check-Valid-Until: no' \
    '' \
    'Types: deb' \
    'URIs: http://snapshot.debian.org/archive/debian-security/20261024T000000Z/' \
    'Suites: bookworm-security' \
    'Components: main' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
    'Check-Valid-Until: no' > /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv chromium=156.0.8078.47-1~deb12u1 \
    chromium-common=156.0.8078.47-1~deb12u1 chromium-sandbox=156.0.8078.47-1~deb12u1 \
    bubblewrap ca-certificates curl git procps \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir httpx==0.28.1 Pillow==12.2.0 PyYAML==6.0.3
WORKDIR /opt/runtime
COPY containers/agents/package.json containers/agents/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY defipi ./defipi
COPY containers/agents/entry.py ./entry.py
RUN useradd --uid 1001 --create-home runner \
    && mkdir -p /work/sessions && chmod 1777 /work/sessions
ENV PATH="/opt/venv/bin:${PATH}" PYTHONPATH=/opt/runtime PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/work/sessions/cache XDG_CONFIG_HOME=/work/sessions/config
USER runner
WORKDIR /work/sessions
ENTRYPOINT ["python", "/opt/runtime/entry.py"]
