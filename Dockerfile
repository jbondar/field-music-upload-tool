FROM python:3.12-slim

# Fail fast and keep logs unbuffered so `docker logs` is useful.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe do the real work here: probing uploads, proving they decode,
# and writing tags. Without them the service can receive files and nothing
# else. git is here only so pip can resolve requirements.txt's
# git+https://.../web-services...#subdirectory=libs/grants_events line --
# pip shells out to a real `git clone` for a VCS requirement, it doesn't
# speak the protocol itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run unprivileged. The uid must be able to write MUSIC_DIR and STAGING_DIR on
# the NAS mount; the compose file passes the host's PUID/PGID for that reason.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# Two workers: uploads are long and I/O bound, and a single worker would let
# one large file block sign-ins and the admin page. Shared state lives on disk
# (manifests, allowlist), not in process memory, so this is safe to scale.
#
# --proxy-headers so X-Forwarded-Proto from Traefik is honoured, which keeps
# redirects on https instead of downgrading to http.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "120"]
