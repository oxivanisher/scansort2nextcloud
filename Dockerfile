# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[ocr]"

FROM python:3.12-slim-bookworm

ARG VERSION=dev
LABEL org.opencontainers.image.title="scansort" \
      org.opencontainers.image.description="Local OCR + self-learning sorting of scanned documents into Nextcloud" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/oxivanisher/scansort2nextcloud" \
      org.opencontainers.image.version="${VERSION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
      ghostscript qpdf unpaper pngquant gosu tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 scansort \
 && useradd -u 1000 -g scansort -d /data -s /usr/sbin/nologin scansort \
 && mkdir -p /config /data/inbox \
 # world-writable (sticky) so a fresh named volume also works with `--user <uid>:<gid>`
 && chmod 1777 /data

COPY --from=build /opt/venv /opt/venv
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCANSORT_CONFIG=/config/config.yaml \
    PUID=1000 \
    PGID=1000

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=os.environ.get('SCANSORT_WEBUI__PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=8).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["scansort", "serve"]
