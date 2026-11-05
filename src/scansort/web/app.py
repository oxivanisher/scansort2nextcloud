"""FastAPI application: review queue, document detail, status page, health check."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from scansort import __version__
from scansort.config import Settings
from scansort.db import Document, IngestAttempt, Status, utcnow
from scansort.filing import (
    FilingError,
    accept_document,
    create_folder,
    delete_document,
    file_document,
    label_options,
    recent_labels,
    reconcile_document,
    target_folder,
)
from scansort.labels import resolve
from scansort.services import Services
from scansort.textextract import ocr_available
from scansort.web.auth import (
    COOKIE_NAME,
    CsrfError,
    LoginRequired,
    LoginThrottle,
    SessionManager,
    check_credentials,
    load_or_create_secret,
)
from scansort.webdav import DavError

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.0f}%"


def _dt(value: datetime | str | None) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["pct"] = _pct
templates.env.filters["dt"] = _dt


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def create_app(settings: Settings, services: Services | None = None, background: bool = True) -> FastAPI:
    pw_hash = settings.webui.password_hash.get_secret_value()
    if not pw_hash:
        raise RuntimeError(
            "webui.password_hash is not set. Create one with `scansort hash-password` and set "
            "SCANSORT_WEBUI__PASSWORD_HASH."
        )
    svc = services or Services(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    sessions = SessionManager(
        load_or_create_secret(settings.webui.session_secret.get_secret_value(), settings.data_dir),
        settings.webui.session_hours,
        settings.webui.secure_cookies,
    )
    throttle = LoginThrottle()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.init_storage()
        if background:
            from scansort.ingest import IngestWorker
            from scansort.scheduler import Scheduler

            svc.worker = IngestWorker(svc)
            svc.worker.start()
            svc.scheduler = Scheduler(svc)
            svc.scheduler.start()
        log.info("scansort started", extra={"version": __version__})
        try:
            yield
        finally:
            if svc.scheduler is not None:
                svc.scheduler.shutdown()
            if svc.worker is not None:
                svc.worker.stop()
            svc.close()

    app = FastAPI(title="scansort", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.svc = svc
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # --- session / security middleware -----------------------------------------------------
    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        if request.url.path.startswith("/static") or request.url.path in ("/healthz", "/favicon.ico"):
            return await call_next(request)
        data, new = sessions.load(request)
        request.state.session = data
        request.state.session_dirty = new
        response = await call_next(request)
        if request.state.session_dirty:
            response.set_cookie(
                COOKIE_NAME, sessions.dump(request.state.session), max_age=sessions.max_age,
                httponly=True, samesite="lax", secure=sessions.secure(request), path="/",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": "login required"}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.exception_handler(CsrfError)
    async def _csrf_error(_request: Request, _exc: CsrfError):
        return Response("CSRF token missing or invalid. Reload the page and try again.", status_code=403)

    # --- dependencies ----------------------------------------------------------------------
    def current_user(request: Request) -> str:
        user = request.state.session.get("u")
        if not user:
            raise LoginRequired()
        return user

    async def csrf_check(request: Request) -> None:
        token = request.headers.get("X-CSRF-Token")
        if not token:
            form = await request.form()
            token = form.get("csrf_token")
        expected = request.state.session.get("csrf", "")
        if not token or not expected or not hmac.compare_digest(str(token), expected):
            raise CsrfError()

    def flash(request: Request, message: str, kind: str = "info") -> None:
        request.state.session.setdefault("flash", []).append([kind, message])
        request.state.session_dirty = True

    def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200) -> HTMLResponse:
        session = request.state.session
        flashes = session.pop("flash", [])
        if flashes:
            request.state.session_dirty = True
        context = {
            "csrf_token": session.get("csrf", ""),
            "user": session.get("u"),
            "flashes": flashes,
            "version": __version__,
            "settings": settings,
            **(ctx or {}),
        }
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def get_doc(doc_id: int) -> Document | None:
        with svc.db.session() as sess:
            return sess.scalar(
                select(Document).options(selectinload(Document.proposals)).where(Document.id == doc_id)
            )

    def pending_ids() -> list[int]:
        with svc.db.session() as sess:
            return list(
                sess.scalars(
                    select(Document.id).where(Document.status == Status.PENDING)
                    .order_by(Document.created_at, Document.id)
                )
            )

    def next_url(after_id: int, ids_before: list[int]) -> str:
        """After acting on ``after_id``: the next pending document in queue order, else the first one."""
        remaining = pending_ids()
        if not remaining:
            return "/"
        pos = ids_before.index(after_id) if after_id in ids_before else -1
        later = [i for i in ids_before[pos + 1:] if i in remaining]
        return f"/doc/{(later or remaining)[0]}"

    Auth = Depends(current_user)
    Csrf = Depends(csrf_check)

    # --- auth routes -----------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        if request.state.session.get("u"):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", {"next": next})

    @app.post("/login", dependencies=[Csrf])
    def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
        key = request.client.host if request.client else "?"
        wait = throttle.retry_after(key)
        if wait:
            return render(request, "login.html", {"next": next, "error": f"Too many attempts. Try again in {wait}s."},
                          status_code=429)
        if not check_credentials(username, password, settings.webui.username, pw_hash):
            throttle.failure(key)
            log.warning("failed login", extra={"client": key})
            return render(request, "login.html", {"next": next, "error": "Invalid username or password."},
                          status_code=401)
        throttle.success(key)
        request.state.session.clear()
        request.state.session.update({"u": username, "csrf": secrets.token_urlsafe(24)})
        request.state.session_dirty = True
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(target, status_code=303)

    @app.post("/logout", dependencies=[Csrf])
    def logout(request: Request):
        request.state.session.clear()
        request.state.session["csrf"] = secrets.token_urlsafe(24)
        request.state.session_dirty = True
        return RedirectResponse("/login", status_code=303)

    # --- queue -----------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request, _user: str = Auth):
        with svc.db.session() as sess:
            docs = sess.scalars(
                select(Document).options(selectinload(Document.proposals))
                .where(Document.status == Status.PENDING).order_by(Document.created_at, Document.id)
            ).all()
            failed = sess.scalar(select(func.count()).select_from(IngestAttempt)) or 0
        return render(request, "queue.html", {"docs": docs, "failed": failed, "resolve": resolve})

    @app.post("/doc/{doc_id}/accept", dependencies=[Csrf])
    def accept(request: Request, doc_id: int, _user: str = Auth):
        error = None
        doc = None
        try:
            doc = accept_document(svc, doc_id)
        except (FilingError, DavError) as exc:
            error = str(exc)
            log.warning("accept failed", extra={"doc_id": doc_id, "error": error})
        if request.headers.get("HX-Request"):
            fresh = get_doc(doc_id)  # with proposals eagerly loaded
            if fresh is None:
                return Response("", status_code=200)
            return render(request, "_row.html", {"doc": fresh, "error": error, "resolve": resolve})
        if error:
            flash(request, error, "error")
        else:
            flash(request, f"Filed to {doc.final_path}", "success")
        return RedirectResponse("/", status_code=303)

    # --- document detail --------------------------------------------------------------------
    @app.get("/doc/{doc_id}", response_class=HTMLResponse)
    def detail(request: Request, doc_id: int, _user: str = Auth):
        doc = get_doc(doc_id)
        if doc is None:
            flash(request, "Document not found.", "error")
            return RedirectResponse("/", status_code=303)
        nc_error = None
        if doc.status == Status.PENDING:
            try:
                status_before = doc.status
                reconcile_document(svc, doc_id)
                doc = get_doc(doc_id)
                if doc.status != status_before:
                    msg = (f"Document was moved in Nextcloud to {doc.nc_path}."
                           if doc.status == Status.HANDLED_EXTERNALLY else "Document was deleted in Nextcloud.")
                    flash(request, msg, "info")
            except DavError as exc:
                nc_error = f"Nextcloud not reachable: {exc}"
        ids = pending_ids()
        prev_id = next_id = None
        if doc_id in ids:
            i = ids.index(doc_id)
            prev_id = ids[i - 1] if i > 0 else None
            next_id = ids[i + 1] if i + 1 < len(ids) else None
        selected = doc.final_label or (doc.top_proposal.label if doc.top_proposal else "")
        return render(request, "doc.html", {
            "doc": doc,
            "prev_id": prev_id,
            "next_id": next_id,
            "position": (ids.index(doc_id) + 1) if doc_id in ids else None,
            "total": len(ids),
            "labels": label_options(svc),
            "recent": recent_labels(svc),
            "selected": selected,
            "target": target_folder(svc, selected, doc.doc_date) if selected else "",
            "nc_error": nc_error,
            "resolve": resolve,
        })

    @app.get("/doc/{doc_id}/pdf")
    def pdf(doc_id: int, _user: str = Auth):
        doc = get_doc(doc_id)
        if doc is None or not doc.nc_path:
            return Response("not found", status_code=404)
        try:
            data = svc.nc.download(doc.nc_path)
        except DavError as exc:
            return Response(f"Could not load the PDF from Nextcloud: {exc}", status_code=502)
        name = quote(Path(doc.nc_path).name)
        return Response(data, media_type="application/pdf", headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{name}",
            "Cache-Control": "private, no-store",
        })

    @app.get("/doc/{doc_id}/thumb")
    def thumb(doc_id: int, _user: str = Auth):
        doc = get_doc(doc_id)
        if doc is None or not doc.nc_fileid:
            return Response(status_code=404)
        result = svc.nc.preview(doc.nc_fileid)
        if result is None:
            return Response(status_code=404)
        content, ctype = result
        return Response(content, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/doc/{doc_id}/suggest")
    def suggest(doc_id: int, label: str = "", date: str = "", _user: str = Auth):
        doc = get_doc(doc_id)
        if doc is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        d = _parse_date(date) or doc.doc_date
        label = label.strip().strip("/")
        return {
            "filename": svc.filename_for(label or None, d, doc.sender),
            "target": target_folder(svc, label, d) if label else "",
        }

    @app.post("/doc/{doc_id}/file", dependencies=[Csrf])
    def file_action(
        request: Request,
        doc_id: int,
        label: str = Form(""),
        filename: str = Form(""),
        doc_date: str = Form(""),
        _user: str = Auth,
    ):
        ids = pending_ids()
        try:
            doc = file_document(svc, doc_id, label, filename or None, _parse_date(doc_date))
        except (FilingError, DavError) as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(f"/doc/{doc_id}", status_code=303)
        flash(request, f"Filed to {doc.final_path}", "success")
        return RedirectResponse(next_url(doc_id, ids), status_code=303)

    @app.post("/doc/{doc_id}/delete", dependencies=[Csrf])
    def delete_action(request: Request, doc_id: int, _user: str = Auth):
        ids = pending_ids()
        try:
            delete_document(svc, doc_id)
        except (FilingError, DavError) as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(f"/doc/{doc_id}", status_code=303)
        flash(request, "Document deleted.", "success")
        return RedirectResponse(next_url(doc_id, ids), status_code=303)

    @app.post("/folders/new", dependencies=[Csrf])
    def new_folder(parent: str = Form(""), name: str = Form(""), doc_date: str = Form(""), _user: str = Auth):
        try:
            label = create_folder(svc, parent or None, name, _parse_date(doc_date))
        except (FilingError, DavError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        d = _parse_date(doc_date)
        return {"label": label, "target": target_folder(svc, label, d)}

    # --- status ----------------------------------------------------------------------------
    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request, _user: str = Auth):
        kv = svc.db.all_kv()
        with svc.db.session() as sess:
            attempts = sess.scalars(select(IngestAttempt).order_by(IngestAttempt.next_try_at)).all()
            counts = dict(sess.execute(select(Document.status, func.count()).group_by(Document.status)).all())
        try:
            sync_stats = json.loads(kv.get("last_archive_sync_stats", "") or "null")
        except ValueError:
            sync_stats = None
        sched = svc.scheduler
        return render(request, "status.html", {
            "model": svc.model.info,
            "model_status": kv.get("model_status"),
            "kv": kv,
            "sync_stats": sync_stats,
            "attempts": attempts,
            "counts": counts,
            "watcher_running": bool(svc.worker and svc.worker.running),
            "ocr_available": ocr_available(),
            "nc_ok": svc.nextcloud_reachable(max_age=30),
            "next_sync": sched.next_run("nightly_sync") if sched else None,
            "job_running": kv.get("job_running") or "",
        })

    def _run_or_schedule(trigger: str, inline) -> None:
        if svc.scheduler is not None:
            getattr(svc.scheduler, trigger)()
        else:
            inline(svc)

    @app.post("/status/sync", dependencies=[Csrf])
    def status_sync(request: Request, _user: str = Auth):
        from scansort.archive import sync_and_train

        _run_or_schedule("trigger_sync", sync_and_train)
        flash(request, "Archive sync & retraining started. This can take a while.", "info")
        return RedirectResponse("/status", status_code=303)

    @app.post("/status/refresh-folders", dependencies=[Csrf])
    def status_refresh(request: Request, _user: str = Auth):
        from scansort.archive import refresh_folders

        _run_or_schedule("trigger_refresh_folders", refresh_folders)
        flash(request, "Folder refresh started.", "info")
        return RedirectResponse("/status", status_code=303)

    @app.post("/status/reconcile", dependencies=[Csrf])
    def status_reconcile(request: Request, _user: str = Auth):
        from scansort.filing import reconcile_all

        _run_or_schedule("trigger_reconcile", reconcile_all)
        flash(request, "Reconciliation started.", "info")
        return RedirectResponse("/status", status_code=303)

    @app.post("/status/retry/{attempt_id}", dependencies=[Csrf])
    def status_retry(request: Request, attempt_id: int, _user: str = Auth):
        with svc.db.session() as sess:
            att = sess.get(IngestAttempt, attempt_id)
            if att is not None:
                att.next_try_at = utcnow()
        if svc.worker is not None:
            svc.worker.wake()
        flash(request, "Retry scheduled.", "info")
        return RedirectResponse("/status", status_code=303)

    # --- health ----------------------------------------------------------------------------
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(HERE / "static" / "favicon.ico", media_type="image/x-icon")

    @app.get("/healthz")
    def healthz():
        db_ok = svc.db.ping()
        watcher_ok = (svc.worker is not None and svc.worker.running) if background else True
        nc_ok = svc.nextcloud_reachable()
        healthy = db_ok and watcher_ok
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "db": db_ok, "nextcloud": nc_ok,
             "watcher": watcher_ok, "version": __version__},
            status_code=200 if healthy else 503,
        )

    return app
