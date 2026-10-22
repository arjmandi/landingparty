FROM python:3.12.14-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_NO_CACHE=1
RUN --mount=type=secret,id=environment_ca \
    if [ -f /run/secrets/environment_ca ]; then \
      cp /run/secrets/environment_ca /usr/local/share/ca-certificates/environment-proxy.crt; \
      update-ca-certificates; \
    fi \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl iptables iproute2 util-linux libnss3-tools chromium-sandbox \
    && rm -rf /var/lib/apt/lists/*
ENV UV_SYSTEM_CERTS=1 SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    PATH="/opt/landingparty/.venv/bin:${PATH}"
RUN python -m pip install --no-cache-dir uv==0.12.23
WORKDIR /opt/landingparty

FROM base AS build
COPY . .
RUN chmod -R a+rX /opt/landingparty && uv sync --frozen --extra full --no-dev

FROM base AS world
RUN apt-get update && apt-get install -y --no-install-recommends chromium ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/landingparty /opt/landingparty
RUN groupadd --gid 10002 harness && useradd --uid 10001 --gid 10002 --create-home world \
    && mkdir -p /data && chown world:harness /data
USER 10001:10002
ENV WORLD_DATA_DIR=/data WORLD_CHROMIUM_EXECUTABLE=/usr/bin/chromium
EXPOSE 7777 7778 7779 8080 8081
ENTRYPOINT ["worldd"]
CMD ["--host", "0.0.0.0", "--port", "7777"]

FROM build AS agent
RUN python - <<'PY'
from pathlib import Path
import shutil

root = Path('/opt/landingparty')
def remove(path):
    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()

for path in root.iterdir():
    if path.name not in {'.venv', 'worldd', 'adapters', 'deploy'}:
        remove(path)
for path in (root / 'worldd').iterdir():
    if path.name not in {'__init__.py', 'cli.py', 'mcp.py', 'models.py', 'util.py'}:
        remove(path)
for path in (root / 'deploy').iterdir():
    if path.name != 'init-fence.sh':
        remove(path)
remove(root / 'adapters' / 'baselines')
for path in (root / 'adapters').rglob('*.md'):
    path.unlink()
PY
RUN useradd --uid 10002 --create-home agent && mkdir -p /work /world && chown agent:agent /work
WORKDIR /work
USER 10002:10002
CMD ["sleep", "infinity"]

# Optional diagnostic target; production images do not include tracing tools.
FROM world AS diagnostics
USER root
RUN apt-get update && apt-get install -y --no-install-recommends strace && rm -rf /var/lib/apt/lists/*
USER 10001:10002
