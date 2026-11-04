"""Command line interface: ``scansort serve | hash-password | check-config | migrate | sync``."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from pydantic import ValidationError

from scansort import __version__

log = logging.getLogger("scansort")


def _load(args):
    from scansort.config import load_settings
    from scansort.logging_setup import setup_logging

    try:
        settings = load_settings(args.config)
    except ValidationError as exc:
        print("Invalid configuration:", file=sys.stderr)
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            print(f"  - {loc}: {err['msg']}", file=sys.stderr)
        print("\nSettings come from the YAML config file and SCANSORT_* environment variables "
              "(nested keys separated by __, e.g. SCANSORT_NEXTCLOUD__PASSWORD).", file=sys.stderr)
        raise SystemExit(2) from None
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    setup_logging(settings.logging.level, settings.logging.format)
    return settings


def cmd_serve(args) -> int:
    import uvicorn

    from scansort.web.app import create_app

    settings = _load(args)
    try:
        app = create_app(settings)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=settings.webui.host,
        port=settings.webui.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.webui.forwarded_allow_ips,
        log_config=None,
        access_log=False,
    )
    return 0


def cmd_hash_password(args) -> int:
    from scansort.web.auth import hash_password

    if args.stdin or not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Use at least 8 characters.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


def cmd_check_config(args) -> int:
    settings = _load(args)
    from scansort.webdav import NextcloudClient

    nc_cfg = settings.nextcloud
    print(f"Config OK. Nextcloud: {nc_cfg.url} as {nc_cfg.user}, root {nc_cfg.root}")
    if not settings.webui.password_hash.get_secret_value():
        print("WARNING: webui.password_hash is empty; `scansort serve` will refuse to start.")
    if args.connect:
        nc = NextcloudClient(
            settings.nextcloud.url, settings.nextcloud.user, settings.nextcloud.password.get_secret_value(),
            verify_tls=settings.nextcloud.verify_tls, timeout=settings.nextcloud.timeout,
        )
        root = nc.stat(settings.nextcloud.root)
        if root is None or not root.is_dir:
            print(f"ERROR: root folder {settings.nextcloud.root} not found for user {settings.nextcloud.user}")
            return 1
        print(f"Nextcloud OK: {settings.nextcloud.root} is accessible.")
    return 0


def cmd_migrate(args) -> int:
    from scansort.db import Database

    settings = _load(args)
    version = Database(settings.db_path).migrate()
    print(f"Database at schema v{version}")
    return 0


def cmd_sync(args) -> int:
    from scansort.archive import sync_and_train
    from scansort.services import Services

    settings = _load(args)
    svc = Services(settings)
    svc.init_storage()
    try:
        sync_and_train(svc)
    finally:
        svc.close()
    info = svc.model.info
    print(f"Model: {info.samples} samples, {info.labels} labels" if info else "No model trained.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scansort", description="Document intake & sorting for Nextcloud")
    parser.add_argument("--version", action="version", version=f"scansort {__version__}")
    parser.add_argument("-c", "--config", help="config file (default: $SCANSORT_CONFIG or /config/config.yaml)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the web UI, watcher and scheduler (default)")
    p = sub.add_parser("hash-password", help="create an argon2 hash for webui.password_hash")
    p.add_argument("--stdin", action="store_true", help="read the password from stdin")
    p = sub.add_parser("check-config", help="validate the configuration")
    p.add_argument("--connect", action="store_true", help="also test the Nextcloud connection")
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("sync", help="run an archive sync + retrain once and exit")

    args = parser.parse_args(argv)
    handlers = {
        None: cmd_serve, "serve": cmd_serve, "hash-password": cmd_hash_password,
        "check-config": cmd_check_config, "migrate": cmd_migrate, "sync": cmd_sync,
    }
    return handlers[args.command](args)
