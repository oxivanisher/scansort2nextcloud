#!/bin/sh
# Runs scansort as an unprivileged user whose UID/GID match PUID/PGID, so files in the
# shared inbox volume (written by the FTP container) are readable and deletable.
set -e

if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"
  if [ "$(id -g scansort)" != "$PGID" ]; then
    groupmod -o -g "$PGID" scansort
  fi
  if [ "$(id -u scansort)" != "$PUID" ]; then
    usermod -o -u "$PUID" scansort
  fi
  DATA_DIR="${SCANSORT_DATA_DIR:-/data}"
  mkdir -p "$DATA_DIR"
  chown scansort:scansort "$DATA_DIR"
  # own the database/model files, but never recurse into the (shared) inbox
  find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name inbox -exec chown -R scansort:scansort {} +
  exec gosu scansort:scansort "$@"
fi

exec "$@"
