#!/bin/sh
# Two ways to choose the user scansort runs as:
#
#  * started as root (default): drop to the built-in "scansort" user, remapped to PUID/PGID.
#  * started as any other user (`docker run --user 1234:1234`, compose `user:`): run as that
#    user directly. Nothing is chowned; the data directory must already be writable.
#
# Either way the user must be able to read *and delete* files the FTP server writes to the inbox.
set -e

DATA_DIR="${SCANSORT_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"
  if [ "$(id -g scansort)" != "$PGID" ]; then
    groupmod -o -g "$PGID" scansort
  fi
  if [ "$(id -u scansort)" != "$PUID" ]; then
    usermod -o -u "$PUID" scansort
  fi
  mkdir -p "$DATA_DIR"
  chown scansort:scansort "$DATA_DIR"
  # own the database/model files, but never recurse into the (shared) inbox
  find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name inbox -exec chown -R scansort:scansort {} +
  exec gosu scansort:scansort "$@"
fi

# --- non-root ------------------------------------------------------------------------------
# an arbitrary UID has no passwd entry, so HOME would be "/"; give tools a writable home
if [ -z "$HOME" ] || [ "$HOME" = "/" ] || [ ! -w "$HOME" ]; then
  export HOME=/tmp
fi

case "$1 $2" in
  "scansort serve"|"scansort migrate"|"scansort sync")
    if [ ! -w "$DATA_DIR" ]; then
      echo "scansort: data directory $DATA_DIR is not writable by uid $(id -u) gid $(id -g)." >&2
      echo "          chown it on the host, e.g.: chown -R $(id -u):$(id -g) ./data" >&2
      exit 1
    fi
    ;;
esac

exec "$@"
