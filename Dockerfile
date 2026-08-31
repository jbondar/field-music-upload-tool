FROM python:3.12-slim

# Fail fast and keep logs unbuffered so `docker logs` is useful.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe do the real work here: probing uploads, proving they decode,
# and writing tags. Without them the service can receive files and nothing
# else. git is here only so pip can clone libs/grants_events below -- pip
# shells out to a real `git clone` for a VCS requirement, it doesn't speak
# the protocol itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# grants_events lives in web-services, a private repo, so this needs a
# token -- kept out of requirements.txt entirely (a plain pip install has
# nowhere to put a credential that isn't either baked into the image layer
# or committed to this file) and out of this step's own layer history too:
# --mount=type=secret exists only for this one RUN, never lands in the
# image or its history. `required=false` means a build with no token
# simply skips this and ships without it -- app/main.py's import is
# defensive specifically so that degrades quietly rather than failing the
# build. compose wires the secret from GRANTS_EVENTS_REPO_TOKEN (see
# apps/docker-compose.yml's top-level `secrets:` and upload's
# `build.secrets`); a plain `docker build` without BuildKit's --secret
# flag gets the same graceful skip.
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
      TOKEN="$(cat /run/secrets/github_token)" && \
      pip install --no-cache-dir "git+https://x-access-token:${TOKEN}@github.com/jbondar/web-services.git@grants-events-v1#subdirectory=libs/grants_events"; \
    else \
      echo "no github_token secret -- grants_events not installed, app will run without it"; \
    fi

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
