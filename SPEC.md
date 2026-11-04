# SPEC: scansort — local document intake & sorting for Nextcloud

> Working title: **scansort** (rename freely).
> Status: draft v0.1 — planning document for implementation.

## 1. Purpose

Digitize paper documents (invoices, letters, contracts, insurance, tax, …) and file them into an existing Nextcloud folder structure with as little manual work as possible — **without sending any data to a cloud service**.

A network scanner uploads PDFs via FTP. scansort picks them up, extracts text, proposes a target folder and filename, and immediately stores the document in a Nextcloud review folder. A small web UI lets the user confirm the proposal with one click (or pick/create another folder), after which the file is moved to its final location inside Nextcloud.

## 2. Goals and non-goals

### Goals

- Nextcloud is the **single source of truth** for all documents. scansort never keeps the only copy of a document longer than necessary.
- Every document goes through **manual review**. There is no automatic filing.
- Confirming a good proposal takes **one click**.
- Fully local processing: OCR and classification run on the host's CPU. No GPU required.
- The classifier **learns from the existing Nextcloud folder structure** and from the user's decisions.
- Configuration via YAML file, overridable by environment variables.
- Runs as a single Docker container next to an FTP container.

### Non-goals (for now)

- Multi-user support (single incoming folder, single UI user).
- A document management system (no tagging, full-text search UI, or metadata database for filed documents beyond what's needed for training).
- Editing PDFs (splitting, merging, rotating). May come later.
- LLM usage. The design leaves a hook for an optional local LLM later (see §14).
- Preserving the scanner's original filename. It carries no information and is discarded.

## 3. Architecture overview

```
┌──────────┐  FTP   ┌──────────────┐  shared volume   ┌───────────────────────────────┐
│ Scanner  │ ─────▶ │ FTP container│ ───────────────▶ │ scansort container            │
└──────────┘        └──────────────┘   /data/inbox    │                               │
                                                      │  watcher ─▶ ingest worker     │
                                                      │               │               │
                                                      │   OCR · extract · classify    │
                                                      │               │               │
                                                      │  SQLite  ◀────┤               │
                                                      │               ▼               │
                                                      │  WebDAV client ───────────────┼──▶ Nextcloud
                                                      │               ▲               │    (machine account)
                                                      │  Web UI (FastAPI + HTMX)      │
                                                      │  scheduler (retrain, sync)    │
                                                      └───────────────────────────────┘
```

### Components (all in one process / container)

| Component | Responsibility |
|---|---|
| **Watcher** | Detects new, fully written files in the watch folder. |
| **Ingest worker** | Hashing, OCR, field extraction, classification, upload to the review folder, DB record. |
| **Nextcloud client** | WebDAV operations: PROPFIND, GET, PUT, MOVE, MKCOL. |
| **Classifier** | Trains on the existing archive and predicts the target label with a confidence. |
| **Web UI** | Login, review queue, preview, accept/override, create folder, rename. |
| **Scheduler** | Nightly archive sync + retrain, periodic reconciliation of the review folder. |
| **Store** | SQLite database for state, text cache, and folder cache. |

### Tech stack

- Python 3.12+
- FastAPI + Uvicorn; Jinja2 templates + HTMX (no JS build step)
- SQLModel (or SQLAlchemy 2.x) on SQLite, WAL mode
- `ocrmypdf` (Tesseract, languages `deu`, `eng`, configurable) and `pypdf` / `pdfminer.six` for text extraction
- `scikit-learn` (TF-IDF + linear classifier), `joblib` for model persistence
- `dateparser` + regex for field extraction
- `httpx` for WebDAV (thin custom client; only a few verbs are needed)
- `pydantic-settings` for configuration
- `APScheduler` (or a simple asyncio loop) for scheduled jobs
- `argon2-cffi` for the UI password hash

## 4. Processing pipeline

### 4.1 File detection

- Watch `watch_dir` (default `/data/inbox`) using inotify (`watchdog`), with a **polling fallback** (`watch.poll_interval`, default 10 s). Also do a full scan on startup.
- Only accept files matching `watch.patterns` (default `*.pdf`, case-insensitive). Ignore hidden files and known temp patterns (`*.part`, `*.tmp`, `.~*`).
- **Stability check:** a file is "ready" when its size and mtime haven't changed for `watch.stable_seconds` (default 5 s) and it can be opened for reading.

### 4.2 Ingest steps

For each ready file:

1. **Hash.** Compute SHA-256. If the hash already exists in `documents`, record it as a duplicate. The file is still uploaded to review, with the duplicate flagged in the UI, so nothing is silently dropped.
2. **Text extraction.**
   - If the PDF has a text layer with at least `ocr.min_chars` characters (default 50), use it.
   - Otherwise run `ocrmypdf --skip-text -l <langs> --output-type pdf` into a temp file. The **OCR'd PDF replaces the original** for upload, so the archived file is searchable.
   - Store the extracted text in the DB.
3. **Field extraction** (§6): document date, optional sender, optional amount.
4. **Classification** (§5): top-N labels with confidences (`classifier.top_n`, default 3).
5. **Proposed filename** (§6.2).
6. **Upload to review folder.** PUT to `<review_folder>/<proposed filename>`, adding a suffix on collision (§7.4). Read back the Nextcloud `fileid` and `etag` via PROPFIND.
7. **Persist.** Write the `documents` row with status `pending`.
8. **Clean up.** Delete the local file only after steps 6 and 7 succeeded.

### 4.3 Failure handling

- On any failure, the file **stays in the watch folder** and is retried with backoff (1 min, 5 min, 30 min, then hourly). The failure is tracked in `ingest_attempts` and shown in the UI.
- If OCR fails, upload anyway with empty text and status `pending`. The user can still sort it manually.
- If the upload fails (Nextcloud unreachable), retry. Never delete the local file.
- Process one file at a time (`ingest.concurrency`, default 1). OCR is CPU-heavy, and the host runs other services.

## 5. Classification

### 5.1 Labels

- A label is a **folder path relative to `nextcloud.root`**, e.g. `Versicherung/Krankenkasse`.
- **Year normalization:** if the last path segment matches `classifier.year_folder_pattern` (default `^\d{4}$`), it is replaced by the placeholder `{year}`. So `Steuern/2024` and `Steuern/2025` become one label, `Steuern/{year}`.
  - When filing, `{year}` is resolved from the document date, falling back to the current year. The year folder is created via MKCOL if it doesn't exist.
- Excluded from training and proposals: the review folder, and any paths matching `classifier.exclude` (glob list, e.g. `_*`, `Archiv/Alt/**`).

### 5.2 Training data

- **Archive sync job** (nightly, `classifier.retrain_cron`, plus manually from the UI):
  1. Walk the folder tree under `nextcloud.root` with `PROPFIND Depth: 1`, recursively. `Depth: infinity` is often disabled on Nextcloud servers.
  2. For every PDF, compare `fileid` + `etag` with the `archive_files` cache. Download and extract text only for new or changed files. Use the PDF text layer only; no OCR on archive files by default (`classifier.ocr_archive: false`), because that would be very slow on the first run.
  3. Remove cache rows for files that no longer exist.
  4. Refresh the `folders` cache.
- Documents filed through scansort are part of the archive and are picked up automatically.

### 5.3 Model

- Features:
  - word TF-IDF (1–2-grams)
  - char TF-IDF (3–5-grams, `char_wb`), which is robust against OCR noise
  - optionally the extracted sender as an extra token
- Model: `LogisticRegression` (multinomial) or `LinearSVC` + `CalibratedClassifierCV`, so calibrated probabilities are available. Pick whichever validates better; start with logistic regression.
- Labels with fewer than `classifier.min_samples` documents (default 2) are excluded from training. They can still be chosen manually.
- Persist the model with `joblib` to `data_dir/model.joblib`, together with metadata (training date, sample count, label count, cross-validated accuracy).
- Training must not block ingest. Train in a background thread or process and swap the model atomically.

### 5.4 Proposals

- Return the top-N labels with probability.
- **Fallbacks:**
  - If no model exists yet (cold start), propose nothing. The UI shows the folder picker with recently used folders.
  - Optional keyword rules (`classifier.rules`, regex → label) are evaluated first. If a rule matches, it becomes the first proposal, marked "rule".
- The UI shows confidence as a visual hint only. Nothing is filed automatically, whatever the confidence.

## 6. Field extraction & naming

### 6.1 Fields

| Field | Method | Notes |
|---|---|---|
| `doc_date` | Regex candidates (`dd.mm.yyyy`, `d. Monat yyyy`, ISO), parsed with `dateparser` (languages from config). Prefer dates near keywords like `Datum`, `Rechnungsdatum`, `Date`. Reject future dates and dates before `extraction.min_year`. | Fallback: ingest date, flagged as "guessed". |
| `sender` | Optional: match against `extraction.known_senders` (name + aliases/regex) from config. Later: learned from the label path. | Empty if unknown. |
| `amount` | Optional: regex for `CHF`/`EUR` amounts near `Total`, `Betrag`, `zu bezahlen`. | Display only. |

### 6.2 Filename

- Template from `naming.template`, e.g. `{date:%Y-%m-%d}_{sender}_{title}`.
- `title` defaults to the last non-year segment of the proposed label (e.g. `Krankenkasse`). The user can edit it in the UI.
- Empty placeholders and their adjacent separators are dropped.
- Sanitize: no `/ \ : * ? " < > |`, collapse whitespace, max length `naming.max_length` (default 120), always ends in `.pdf`.
- The scanner's original filename is discarded.

## 7. Nextcloud integration

### 7.1 Machine account

- A dedicated Nextcloud user (e.g. `scanbot`), authenticated with an **app password**, never the main password.
- It gets access to the documents tree either through a **share with edit permission** from the user's account, or through a **Group Folder**.
- `nextcloud.root` is the path as seen *by the machine account*, e.g. `/Dokumente`.

### 7.2 WebDAV endpoints

- Base: `<url>/remote.php/dav/files/<user>/`
- Operations used:
  - `PROPFIND` (Depth 0/1), requesting `oc:fileid`, `d:getetag`, `d:getcontentlength`, `d:getlastmodified`, `d:resourcetype`
  - `GET` (preview, archive sync)
  - `PUT` (upload)
  - `MOVE` with `Overwrite: F`
  - `MKCOL`
- **Filing uses a server-side MOVE.** That keeps the file ID, versions and shares.
- Timeouts and retries are configurable. TLS verification is on by default (`nextcloud.verify_tls`).

### 7.3 Review folder

- `nextcloud.review_folder`, e.g. `/Dokumente/_Review`. It is created on startup if missing.
- Documents stay there until filed in the UI or moved by the user directly in Nextcloud.

### 7.4 Name collisions

- Before PUT or MOVE, check whether the target exists. If it does, append ` (2)`, ` (3)`, … before `.pdf`.
- Always use `Overwrite: F` as a second safety net.

### 7.5 Reconciliation

A periodic job (`nextcloud.reconcile_interval`, default 10 min) plus a check before every UI action looks up each `pending` document by `fileid`:

| Situation | Action |
|---|---|
| Still in the review folder | Nothing. |
| Moved elsewhere under `root` | Status `handled_externally`; store the new path. It becomes training data through the archive sync. |
| Deleted (not found) | Status `deleted_externally`; hide from the queue. |

Lookup by file ID uses a Nextcloud `SEARCH` on `oc:fileid` or the `/f/<fileid>` redirect. Fallback: PROPFIND on the stored path, then a review-folder listing.

## 8. Data model (SQLite)

```text
documents
  id               INTEGER PK
  sha256           TEXT  (indexed, not unique — duplicates allowed but flagged)
  duplicate_of     INTEGER NULL → documents.id
  status           TEXT  pending | filed | handled_externally | deleted_externally | error
  nc_fileid        TEXT  NULL
  nc_path          TEXT  NULL        -- current known path
  nc_etag          TEXT  NULL
  text             TEXT
  text_source      TEXT  layer | ocr | none
  page_count       INTEGER
  doc_date         DATE NULL
  doc_date_guessed BOOLEAN
  sender           TEXT NULL
  amount           TEXT NULL
  proposed_name    TEXT
  final_label      TEXT NULL         -- label the user chose
  final_path       TEXT NULL
  created_at       DATETIME
  filed_at         DATETIME NULL

proposals
  id            INTEGER PK
  document_id   INTEGER → documents.id
  rank          INTEGER
  label         TEXT
  probability   REAL
  source        TEXT  model | rule
  model_version TEXT

ingest_attempts
  id          INTEGER PK
  local_path  TEXT
  sha256      TEXT NULL
  attempts    INTEGER
  last_error  TEXT
  next_try_at DATETIME

archive_files            -- training cache
  nc_fileid   TEXT PK
  nc_path     TEXT
  etag        TEXT
  label       TEXT       -- normalized label
  text        TEXT
  synced_at   DATETIME

folders                  -- folder cache for the picker
  path        TEXT PK
  label       TEXT
  doc_count   INTEGER
  synced_at   DATETIME

settings_kv              -- small runtime state (model version, last sync, …)
  key TEXT PK
  value TEXT
```

- Use Alembic (or simple versioned migration scripts) from the start.
- Run SQLite in WAL mode and use a single writer.

## 9. Web UI

### 9.1 Authentication

- Single user. Username from config. Password stored as an **argon2 hash** in config or env (`SCANSORT_WEBUI_PASSWORD_HASH`). Provide a CLI helper: `scansort hash-password`.
- Session cookie: signed, `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS. Session lifetime is configurable.
- Login rate limiting (simple in-memory backoff).
- CSRF protection on all state-changing requests (token in forms or HTMX headers).
- Meant to run behind the user's reverse proxy; it also works on plain LAN HTTP.

### 9.2 Pages

**Review queue (`/`)**
- Lists pending documents, oldest first: thumbnail or first-page preview, date, proposed name, top proposal with confidence bar, and a duplicate badge.
- Each row has a **one-click "Accept"** button that files with the top proposal and the proposed name.
- Clicking a row opens the detail view.

**Document detail (`/doc/{id}`)**
- Left: PDF preview, streamed from Nextcloud through the app (`/doc/{id}/pdf`); the browser's built-in PDF viewer is sufficient.
- Right:
  - Top-N proposals as buttons. The resolved path is shown, e.g. `Steuern/2026`.
  - A **folder picker**: searchable list or tree from the `folders` cache, with recently used folders on top.
  - **"New folder"**: create a subfolder under the selected folder (MKCOL), then select it.
  - Editable fields: filename and date. Changing the date re-resolves `{year}`.
  - Extracted text, collapsible, for debugging.
  - Actions: **File**, **Skip** (next document), **Delete**.
    - Delete moves the file to `nextcloud.trash_folder` if configured, otherwise uses a WebDAV DELETE, which goes to the Nextcloud trash bin. It always requires a confirmation.
- Keyboard shortcuts: `Enter` = file with the current selection, `1`–`3` = choose proposal, `j`/`k` = next/previous document.

**Status (`/status`)**
- Model info: training date, number of samples and labels, validation accuracy.
- Last archive sync, last reconciliation.
- Failed ingests with error messages and a "retry now" button.
- Buttons: "Sync archive & retrain now", "Refresh folders".

### 9.3 Filing action (server side)

1. Reconcile the document (§7.5). Abort with a message if it was moved or deleted externally.
2. Resolve the target path (label + `{year}`), and MKCOL any missing folders.
3. Resolve name collisions (§7.4).
4. MOVE from the review folder to the target.
5. Update the DB: status `filed`, `final_label`, `final_path`, `filed_at`.
6. Optionally add the document to `archive_files` immediately, so the next retrain doesn't need to download it.

## 10. Configuration

- File: `/config/config.yaml`. Path overridable with `SCANSORT_CONFIG`.
- **Every key can be overridden by environment variables:** prefix `SCANSORT_`, nested keys separated by `__`, e.g. `SCANSORT_NEXTCLOUD__PASSWORD`.
- Secrets (Nextcloud app password, UI password hash, session secret) should come from environment variables or Docker secrets (`*_FILE` variants supported).
- Validate on startup with pydantic and fail fast with clear messages.

### 10.1 Example `config.yaml`

```yaml
data_dir: /data                 # SQLite DB, model, temp files
watch:
  dir: /data/inbox
  patterns: ["*.pdf"]
  stable_seconds: 5
  poll_interval: 10

nextcloud:
  url: https://cloud.example.ch
  user: scanbot
  password: ""                  # set via SCANSORT_NEXTCLOUD__PASSWORD
  root: /Dokumente
  review_folder: /Dokumente/_Review
  trash_folder: null            # null = use Nextcloud trash bin via DELETE
  verify_tls: true
  timeout: 30
  reconcile_interval: 600       # seconds

ocr:
  enabled: true
  languages: [deu, eng]
  min_chars: 50

extraction:
  date_languages: [de, en]
  min_year: 1990
  known_senders:
    - name: Swisscom
      patterns: ["swisscom"]
    - name: CSS
      patterns: ["css versicherung", "css kranken"]

classifier:
  retrain_cron: "0 3 * * *"
  top_n: 3
  min_samples: 2
  year_folder_pattern: '^\d{4}$'
  exclude: ["_*"]
  ocr_archive: false
  rules:
    - pattern: "(?i)steuererkl(ä|ae)rung"
      label: "Steuern/{year}"

naming:
  template: "{date:%Y-%m-%d}_{sender}_{title}"
  max_length: 120

webui:
  host: 0.0.0.0
  port: 8080
  username: admin
  password_hash: ""             # set via SCANSORT_WEBUI__PASSWORD_HASH
  session_secret: ""            # set via SCANSORT_WEBUI__SESSION_SECRET
  session_hours: 168

logging:
  level: INFO
  format: text                  # text | json
```

## 11. Deployment

### 11.1 Container

- Base image `python:3.12-slim`, plus apt packages: `ocrmypdf`, `tesseract-ocr`, `tesseract-ocr-deu`, `tesseract-ocr-eng`, `ghostscript`, `qpdf`, `unpaper` (optional).
- Run as a non-root user with configurable UID/GID (`PUID`/`PGID`), so shared-volume permissions match the FTP container.
- Healthcheck endpoint: `GET /healthz`. It is unauthenticated and reports DB OK, Nextcloud reachable, and watcher running.
- Volumes: `/config` (read-only), `/data` (DB, model, temp), and `/data/inbox` (shared with FTP; may be a separate mount).

### 11.2 Example `docker-compose.yml`

```yaml
services:
  ftp:
    image: delfer/alpine-ftp-server     # or any FTP server image
    restart: unless-stopped
    environment:
      USERS: "scanner|CHANGE_ME|/home/scanner|1000"
    volumes:
      - ./inbox:/home/scanner
    ports:
      - "21:21"
      - "21000-21010:21000-21010"

  scansort:
    build: .
    restart: unless-stopped
    environment:
      PUID: 1000
      PGID: 1000
      SCANSORT_NEXTCLOUD__PASSWORD: ${NC_APP_PASSWORD}
      SCANSORT_WEBUI__PASSWORD_HASH: ${WEBUI_PASSWORD_HASH}
      SCANSORT_WEBUI__SESSION_SECRET: ${SESSION_SECRET}
      TZ: Europe/Zurich
    volumes:
      - ./config:/config:ro
      - ./data:/data
      - ./inbox:/data/inbox
    ports:
      - "8080:8080"
```

## 12. Logging, observability, security

- Structured logging. Every ingest and filing action is logged with the document ID. **Document text is never logged.**
- The Nextcloud password and session secret must never appear in logs or error pages.
- The `/status` page covers basic monitoring. Optional later: a Home Assistant notification or webhook when new documents are waiting (`notify.webhook_url`).
- The app talks only to Nextcloud. No other outbound connections, no telemetry.
- Temp files from OCR live under `data_dir/tmp` and are deleted after each ingest; leftovers are removed on startup.

## 13. Testing

- **Unit tests:** date extraction (German/English formats, edge cases), filename sanitizing and templating, label normalization and year resolution, collision suffixing.
- **Classifier tests:** a small fixture corpus (synthetic texts) checking that training and prediction work and that labels below `min_samples` are excluded.
- **WebDAV client:** tests against a mocked server (`respx`), and optionally an integration test against a disposable Nextcloud container (`nextcloud:latest`) in CI.
- **Pipeline test:** drop a sample PDF (with and without a text layer) into a temp watch folder and assert the upload and DB state, with a mocked Nextcloud.
- **UI:** a smoke test with FastAPI's `TestClient` for login, queue, file action and CSRF rejection.

## 14. Milestones

1. **M1 – Skeleton:** config loading, SQLite and migrations, WebDAV client (PROPFIND, PUT, MOVE, MKCOL), `/healthz`.
2. **M2 – Ingest:** watcher with stability check, hashing, text layer and OCR, upload to review, retries.
3. **M3 – Web UI v1:** login, review queue, detail view with preview, manual folder picker, file, create folder, rename. *Usable end to end without the classifier.*
4. **M4 – Classifier:** archive sync with text cache, training, proposals, one-click accept, status page.
5. **M5 – Polish:** field extraction (date, sender), filename template, reconciliation, keyboard shortcuts, duplicate detection UI.
6. **M6 – Optional:** keyword rules UI, webhook notifications, page rotate/split, optional local LLM for titles or cold-start proposals (e.g. a small model via Ollama with structured output constrained to known labels; disabled by default).

## 15. Open points

- Final project name.
- Exact folder layout and filename convention, to be derived from the user's `tree` output of the documents folder. That determines `year_folder_pattern`, `exclude` and `naming.template` defaults.
- Whether the scanner produces PDFs with a text layer (this affects OCR load).
- Whether the archive contains many image-only PDFs. If so, consider a one-time batch OCR of the archive text for training (`classifier.ocr_archive: true`, run once overnight).
