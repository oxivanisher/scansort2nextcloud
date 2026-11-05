# scansort2nextcloud

**Scan → OCR → one-click filing into Nextcloud. Fully local, no cloud AI.**

[![CI](https://github.com/oxivanisher/scansort2nextcloud/actions/workflows/ci.yml/badge.svg)](https://github.com/oxivanisher/scansort2nextcloud/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Container](https://img.shields.io/badge/container-ghcr.io-blue?logo=docker)](https://github.com/oxivanisher/scansort2nextcloud/pkgs/container/scansort2nextcloud)

scansort turns a network scanner and a Nextcloud instance into a lightweight paperless office. Your
scanner uploads PDFs via FTP; scansort OCRs them, **learns from your existing Nextcloud folder
structure** which folder a document belongs in, proposes a filename, and puts the scan in a review
folder. In a small web UI you confirm the proposal with **one click**. The file is then moved
server-side into its final Nextcloud folder, keeping its file ID, versions and shares.

Nothing leaves your network: OCR (Tesseract via OCRmyPDF) and classification (scikit-learn) run
on your own CPU. No GPU, LLM or external API is needed.

> If you want Nextcloud to stay the single source of truth for your documents and don't want to
> run a full document management system like paperless-ngx, this is for you.

---

## Features

- 📠 **Scanner-friendly intake.** Watches a folder shared with an FTP container (inotify plus
  polling fallback), and waits until uploads have finished.
- 🔍 **OCR only when needed.** PDFs that already have a text layer are used as is. Image-only scans
  go through `ocrmypdf` (German and English by default), and the archived file is then searchable.
- 🧠 **Self-learning classifier.** TF-IDF (words + character n-grams, robust against OCR noise)
  with logistic regression, trained on the PDFs already in your Nextcloud folders. It retrains
  nightly and picks up every document you file.
- 📅 **Year folders handled automatically.** `Steuern/2024` and `Steuern/2025` are learned as one
  label, `Steuern/{year}`. The year comes from the document date and the folder is created if
  needed.
- 🏷️ **Smart filenames**, e.g. `2026-01-05_CSS_Krankenkasse.pdf`, from the extracted document date,
  a known sender and the target folder. The template is configurable.
- ✅ **Always a human in the loop.** Nothing is filed automatically. You get a review queue with
  one-click *Accept*, a detail view with PDF preview, top-3 proposals, a searchable folder picker,
  "new folder", and keyboard shortcuts.
- 🔁 **Stays in sync with Nextcloud.** If you move or delete a scan from the review folder directly
  in Nextcloud, scansort notices.
- 🧾 **Extras:** keyword rules (regex → folder), duplicate detection (SHA-256), extraction of
  amounts (CHF/EUR), a retry queue with backoff, and a status page.
- 🔐 **Secure by default:** a dedicated Nextcloud machine account with an app password, an
  argon2-hashed UI password, CSRF protection, a signed HttpOnly session cookie and login
  throttling.
- 🐳 **One container** (amd64 and arm64), configured through YAML and/or environment variables,
  with Docker secrets supported.

## How it works

```
┌─────────┐  FTP  ┌──────────────┐ shared volume ┌──────────────────────────────┐   WebDAV   ┌───────────┐
│ Scanner │ ────▶ │ FTP container│ ────────────▶ │ scansort                     │ ─────────▶ │ Nextcloud │
└─────────┘       └──────────────┘  /data/inbox  │ watch → OCR → extract →      │            │           │
                                                 │ classify → upload to _Review │            │ _Review/  │
                                                 │ Web UI: review & file (MOVE) │            │ Steuern/… │
                                                 └──────────────────────────────┘            └───────────┘
```

1. A new PDF appears in the inbox. scansort hashes it, extracts the text (with OCR if needed), and
   detects the date, sender and amount.
2. The classifier proposes the top 3 target folders, and a filename is generated.
3. The PDF is uploaded to `<root>/_Review/` in Nextcloud and the local copy is deleted. Nextcloud
   is the single source of truth.
4. In the web UI you click **Accept**, or choose another folder, edit the name or date, and click
   **File**. scansort performs a server-side `MOVE` to the final folder.
5. Every night scansort syncs the archive's text (only new or changed files are downloaded) and
   retrains the model.

## Quick start (Docker Compose)

### 1. Prepare Nextcloud

1. Create a dedicated user, e.g. `scanbot`.
2. Give it **edit** access to your documents tree: share the folder from your account with
   `scanbot`, or use a Group Folder.
3. Log in as `scanbot` → *Settings → Security → Devices & sessions*, and create an **app
   password**.
4. Note the path of the documents tree **as seen by `scanbot`**, e.g. `/Dokumente`.

### 2. Configure and run

```bash
git clone https://github.com/oxivanisher/scansort2nextcloud.git
cd scansort2nextcloud
cp config/config.example.yaml config/config.yaml   # set nextcloud.url / root
cp .env.example .env                               # set NC_USER (e.g. scanbot) + NC_APP_PASSWORD

# create the UI password hash and paste it into .env (keep the single quotes!)
docker compose run --rm --no-deps scansort scansort hash-password

# optional: verify the config and the Nextcloud connection
docker compose run --rm --no-deps scansort scansort check-config --connect

docker compose up -d
```

Open `http://<host>:8080`, log in, and point your scanner's *scan to FTP* at the host (user
`scanner`, the password from `.env`).

On first start scansort syncs your archive and trains the model. The *Status* page shows progress.
Until a model exists, pick folders manually; everything you file becomes training data.

> **Permissions:** the FTP server and scansort share `./inbox`, so scansort must be able to read
> **and delete** the scans. The example compose file runs both as `STACK_UID`/`STACK_GID` from
> `.env`. The bundled `delfer/alpine-ftp-server` image has to start as root, but it writes
> uploads as that UID/GID into an inbox it owns and that is not group-writable. scansort must
> therefore use the same UID. See [Running as a different user](#running-as-a-different-user).

### Running as a different user

The container never runs scansort as root. There are two ways to choose the user:

**`PUID`/`PGID` (default).** The container starts as root, remaps its built-in `scansort` user to
`PUID:PGID` (default `1000:1000`), takes ownership of the data directory (not the inbox) and drops
privileges with `gosu`.

```yaml
environment:
  PUID: 1500
  PGID: 1500
```

**`user:` / `--user` (one user per stack).** The container runs as that UID:GID from the first
process on. It needs no root, no passwd entry and no capabilities. Nothing is chowned for you, so
the data directory must be writable by that user:

```bash
sudo chown -R 1500:1500 ./data ./inbox
```

```yaml
services:
  scansort:
    image: ghcr.io/oxivanisher/scansort2nextcloud:latest
    user: "1500:1500"
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
```

A fresh named volume for `/data` also works, because the image's `/data` is world-writable with
the sticky bit set. If the data directory is not writable, the container exits with a message that
gives the `chown` command to run. `/config` only needs to be readable.

### Container image

```
ghcr.io/oxivanisher/scansort2nextcloud:latest     # main branch
ghcr.io/oxivanisher/scansort2nextcloud:0.1.0      # releases (git tag v0.1.0)
```

Images are built for `linux/amd64` and `linux/arm64` (e.g. Raspberry Pi 4/5) by GitHub Actions.

| Volume        | Purpose                                        |
|---------------|------------------------------------------------|
| `/config`     | `config.yaml` (can be read-only)               |
| `/data`       | SQLite DB, model, temp files                   |
| `/data/inbox` | Watch folder, shared with the FTP container    |

The health check is `GET /healthz` (unauthenticated). It reports the DB, Nextcloud and watcher state.

## Configuration

All settings live in `config/config.yaml` (see [`config/config.example.yaml`](config/config.example.yaml)).
**Every key can be overridden by an environment variable**: add the prefix `SCANSORT_` and join
nested keys with `__`.

| YAML key                        | Environment variable                   | Default                 |
|---------------------------------|----------------------------------------|-------------------------|
| `nextcloud.url`                 | `SCANSORT_NEXTCLOUD__URL`              | —                       |
| `nextcloud.user`                | `SCANSORT_NEXTCLOUD__USER`             | —                       |
| `nextcloud.password`            | `SCANSORT_NEXTCLOUD__PASSWORD`         | —                       |
| `nextcloud.root`                | `SCANSORT_NEXTCLOUD__ROOT`             | `/Dokumente`            |
| `nextcloud.review_folder`       | `SCANSORT_NEXTCLOUD__REVIEW_FOLDER`    | `/Dokumente/_Review`    |
| `nextcloud.trash_folder`        | `SCANSORT_NEXTCLOUD__TRASH_FOLDER`     | none (Nextcloud trash bin) |
| `ocr.languages`                 | `SCANSORT_OCR__LANGUAGES='["deu","eng"]'` | `[deu, eng]`         |
| `classifier.retrain_cron`       | `SCANSORT_CLASSIFIER__RETRAIN_CRON`    | `0 3 * * *`             |
| `classifier.exclude`            | `SCANSORT_CLASSIFIER__EXCLUDE`         | `["_*"]`                |
| `naming.template`               | `SCANSORT_NAMING__TEMPLATE`            | `{date:%Y-%m-%d}_{sender}_{title}` |
| `webui.password_hash`           | `SCANSORT_WEBUI__PASSWORD_HASH`        | — (required)            |
| `webui.session_secret`          | `SCANSORT_WEBUI__SESSION_SECRET`       | generated and stored in `/data` |
| `logging.format`                | `SCANSORT_LOGGING__FORMAT`             | `text` (`json` available) |

Notes:

- **Secrets as files:** any variable also works with a `_FILE` suffix, e.g.
  `SCANSORT_NEXTCLOUD__PASSWORD_FILE=/run/secrets/nc_app_password`.
- List values passed through the environment must be JSON.
- The configuration is validated on startup. Typos and invalid values stop scansort with a clear
  message.
- `naming.template` placeholders: `{date:<strftime>}`, `{sender}`, `{title}` (last non-year
  folder of the target, editable in the UI) and `{year}`. An empty placeholder is dropped
  together with its separator.
- `classifier.exclude` globs are matched against paths relative to `root`. Folders that start
  with `_` are excluded by default (this also covers `_Review`). Use `**/_*` to exclude them at
  any depth.
- `classifier.rules` are regex → label rules that are checked before the model, e.g. to always
  propose `Steuern/{year}` for "Steuererklärung".
- `extraction.known_senders` maps regex patterns to a sender name used in the filename.

## Using the web UI

- **Review queue (`/`):** all pending scans, oldest first, with thumbnail, date, proposed name,
  top proposal and confidence. Click **Accept** to file a document with its top proposal.
- **Detail view (`/doc/<id>`):** PDF preview, proposals, folder search, new folder, editable date
  and filename, and the extracted text.

| Key      | Action                         |
|----------|--------------------------------|
| `Enter`  | File with the current selection |
| `1`–`3`  | Choose proposal 1–3            |
| `j`/`k`  | Next / previous document       |
| `/`      | Search folders                 |

- **Status (`/status`):** model statistics (samples, labels, cross-validated accuracy), the last
  archive sync and reconciliation, failed ingests with *Retry now*, and buttons for *Sync archive
  & retrain now* and *Refresh folders*.

scansort is meant to run behind your reverse proxy (TLS). If it does, set
`webui.forwarded_allow_ips` to the proxy's IP address so that HTTPS is detected and the cookie
gets the `Secure` flag. It also works over plain HTTP on the LAN.

## CLI

```
scansort serve                     # web UI + watcher + scheduler (default)
scansort hash-password [--stdin]   # create an argon2 hash for webui.password_hash
scansort check-config [--connect]  # validate config, optionally test Nextcloud access
scansort sync                      # archive sync + retrain once, then exit
scansort migrate                   # apply database migrations
```

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,ocr]"        # OCR also needs tesseract + ghostscript from your OS
ruff check src tests
pytest
```

To run it locally against your Nextcloud:

```bash
export SCANSORT_CONFIG=./config/config.yaml SCANSORT_DATA_DIR=./data SCANSORT_WATCH__DIR=./inbox
export SCANSORT_WEBUI__PASSWORD_HASH="$(echo 'dev-password' | scansort hash-password --stdin)"
scansort serve
```

Project layout:

```
src/scansort/
  config.py       settings (YAML + env, pydantic-settings)
  db.py           SQLite models + versioned migrations
  webdav.py       minimal Nextcloud WebDAV client
  ingest.py       watcher, stability check, OCR, upload to review
  extraction.py   date / sender / amount extraction
  naming.py       filename template + sanitizing
  labels.py       folder ↔ label normalization ({year})
  classifier.py   TF-IDF + logistic regression, rules
  archive.py      archive sync, folder cache, retraining
  filing.py       file / delete / reconcile / create folder
  scheduler.py    nightly retrain, periodic reconciliation
  web/            FastAPI + Jinja2 + HTMX UI
```

The full design document is in [`SPEC.md`](SPEC.md).

## Privacy

- scansort talks to your Nextcloud and nothing else: no telemetry and no external services. The
  UI's JavaScript (htmx) is bundled.
- Document text is never written to the logs. Passwords and secrets never appear in logs or
  error pages.
- Local copies are deleted only after the upload to Nextcloud has succeeded.

## Roadmap

- [x] M1–M5: ingest, OCR, review UI, classifier, field extraction, reconciliation
- [ ] Rules editor in the UI, webhook / Home Assistant notification for new documents
- [ ] Page rotate / split / merge
- [ ] Optional local LLM (e.g. via Ollama) for titles and cold-start proposals, disabled by default

## License

[MIT](LICENSE) © Marc Urben
