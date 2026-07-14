from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .service import LoginRateLimitedError, ObsyncService

SESSION_COOKIE = "obsync_session"
CSRF_COOKIE = "obsync_csrf"


class RegistrationRequest(BaseModel):
    code: str
    name: str = ""
    hostname: str = ""
    os_name: str = ""
    os_version: str = ""
    agent_version: str = ""


class RootRequest(BaseModel):
    root_key: str
    name: str
    path: str
    destination: str = "Obsync"
    include_patterns: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude_patterns: list[str] = Field(default_factory=list)
    enabled: bool = True


class MissingRequest(BaseModel):
    root_id: str
    source_path: str


class CommandCompleteRequest(BaseModel):
    ok: bool
    result: str = ""


class SetupRequest(BaseModel):
    username: str
    password: str
    legacy_token: str = ""
    remember: bool = True


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


def _bearer(value: str | None) -> str:
    if not value:
        return ""
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def get_service(request: Request) -> ObsyncService:
    return request.app.state.service


ServiceDependency = Annotated[ObsyncService, Depends(get_service)]


def require_admin(
    request: Request,
    service: ServiceDependency,
    authorization: Annotated[str | None, Header()] = None,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, Any]:
    session_token = request.cookies.get(SESSION_COOKIE, "")
    session = service.authenticate_admin_session(session_token)
    if session:
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not service.verify_admin_csrf(
            session, x_csrf_token or ""
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        session["_raw_token"] = session_token
        return session
    token = _bearer(authorization)
    if not service.verify_admin(token):
        raise HTTPException(status_code=401, detail="Sign in required")
    return {"legacy": True}


def require_agent(
    service: ServiceDependency,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    agent = service.authenticate_agent(_bearer(authorization))
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid agent token")
    return agent


AdminDependency = Annotated[dict[str, Any], Depends(require_admin)]
AgentDependency = Annotated[dict[str, Any], Depends(require_agent)]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _set_session_cookies(
    response: Response,
    session: dict[str, Any],
    *,
    secure: bool,
) -> None:
    max_age = int(session["max_age"])
    response.set_cookie(
        SESSION_COOKIE,
        session["token"],
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        session["csrf_token"],
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


def _clear_session_cookies(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        SESSION_COOKIE, httponly=True, secure=secure, samesite="strict", path="/"
    )
    response.delete_cookie(CSRF_COOKIE, httponly=False, secure=secure, samesite="strict", path="/")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = ObsyncService(settings)
    app = FastAPI(
        title="Obsync API",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.service = service

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "vault_ready": settings.vault_path.exists() and os.access(settings.vault_path, os.W_OK),
        }

    @app.get("/api/v1/meta")
    async def meta() -> dict[str, Any]:
        return {"name": "Obsync", "version": __version__, "authentication": "session"}

    @app.get("/api/v1/auth/status")
    async def auth_status() -> dict[str, bool]:
        return service.setup_status()

    @app.post("/api/v1/auth/setup")
    def auth_setup(payload: SetupRequest, request: Request, response: Response) -> dict[str, Any]:
        try:
            user = service.create_admin_account(
                payload.username,
                payload.password,
                legacy_token=payload.legacy_token,
            )
        except ValueError as exc:
            status = 401 if "token is incorrect" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        session = service.create_admin_session(
            user,
            remember=payload.remember,
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
        _set_session_cookies(
            response,
            session,
            secure=settings.secure_cookies or request.url.scheme == "https",
        )
        return {"authenticated": True, "username": user["username"]}

    @app.post("/api/v1/auth/login")
    def auth_login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        if not service.has_admin_account():
            raise HTTPException(status_code=409, detail="Complete admin account setup first")
        try:
            session = service.login_admin(
                payload.username,
                payload.password,
                remember=payload.remember,
                client_ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        except LoginRateLimitedError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": "900"},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        _set_session_cookies(
            response,
            session,
            secure=settings.secure_cookies or request.url.scheme == "https",
        )
        return {"authenticated": True, "username": session["username"]}

    @app.post("/api/v1/auth/logout")
    async def auth_logout(
        request: Request,
        response: Response,
        session: AdminDependency,
    ) -> dict[str, bool]:
        service.logout_admin(str(session.get("_raw_token", "")))
        _clear_session_cookies(
            response,
            secure=settings.secure_cookies or request.url.scheme == "https",
        )
        return {"ok": True}

    @app.get("/api/v1/admin/session")
    async def admin_session(session: AdminDependency) -> dict[str, Any]:
        return {
            "authenticated": True,
            "username": session.get("username", "admin"),
            "legacy": bool(session.get("legacy")),
        }

    @app.get("/api/v1/admin/overview")
    async def overview(_token: AdminDependency) -> dict[str, Any]:
        return service.overview()

    @app.get("/api/v1/admin/agents")
    async def agents(_token: AdminDependency) -> dict[str, Any]:
        return {"items": service.list_agents()}

    @app.get("/api/v1/admin/roots")
    async def roots(_token: AdminDependency) -> dict[str, Any]:
        return {"items": service.list_roots()}

    @app.post("/api/v1/admin/enrollments")
    async def create_enrollment(
        _token: AdminDependency, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        return service.create_enrollment(
            label=str(payload.get("label", "")), minutes=int(payload.get("minutes", 20))
        )

    @app.post("/api/v1/admin/agents/{agent_id}/scan")
    async def scan_agent(agent_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.queue_command(agent_id, "scan")

    @app.get("/api/v1/admin/documents")
    async def documents(
        _token: AdminDependency,
        status: str = "",
        search: str = "",
        review: bool | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return service.list_documents(
            status=status, search=search, review=review, limit=limit, offset=offset
        )

    @app.post("/api/v1/admin/documents/{document_id}/approve")
    async def approve_document(document_id: str, _token: AdminDependency) -> dict[str, bool]:
        service.approve_document(document_id)
        return {"ok": True}

    @app.post("/api/v1/admin/documents/{document_id}/retry")
    async def retry_document(document_id: str, _token: AdminDependency) -> dict[str, Any]:
        document = service.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        root = service.db.query_one(
            "SELECT root_key FROM roots WHERE id = ?", (document["root_id"],)
        )
        assert root is not None
        return service.queue_command(
            document["agent_id"],
            "resync",
            {"root_key": root["root_key"], "source_path": document["source_path"]},
        )

    @app.get("/api/v1/admin/events")
    async def events(
        _token: AdminDependency, limit: int = Query(100, ge=1, le=500)
    ) -> dict[str, Any]:
        rows = service.db.query_all(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return {"items": rows}

    @app.get("/api/v1/admin/settings")
    async def get_settings(_token: AdminDependency) -> dict[str, Any]:
        return service.settings_for_ui()

    @app.put("/api/v1/admin/settings")
    async def put_settings(payload: dict[str, Any], _token: AdminDependency) -> dict[str, Any]:
        return service.update_settings(payload)

    @app.post("/api/v1/admin/settings/test-llm")
    async def test_llm(
        _token: AdminDependency, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await service.test_llm(payload or {})

    @app.post("/api/v1/agents/register")
    async def register(payload: RegistrationRequest) -> dict[str, Any]:
        if not settings.allow_registration:
            raise HTTPException(status_code=403, detail="Agent enrollment is disabled")
        return service.register_agent(payload.code, payload.model_dump(exclude={"code"}))

    @app.post("/api/v1/agent/heartbeat")
    async def heartbeat(payload: dict[str, str], agent: AgentDependency) -> dict[str, bool]:
        service.heartbeat(agent["id"], payload.get("agent_version", ""))
        return {"ok": True}

    @app.post("/api/v1/agent/roots")
    async def upsert_root(payload: RootRequest, agent: AgentDependency) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        return service.upsert_root(agent["id"], payload.model_dump())

    @app.post("/api/v1/agent/documents/sync")
    async def sync_document(
        root_id: Annotated[str, Form()],
        source_path: Annotated[str, Form()],
        source_mtime_ns: Annotated[int, Form()],
        source_size: Annotated[int, Form()],
        file: Annotated[UploadFile, File()],
        agent: AgentDependency,
        sha256: Annotated[str, Form()] = "",
        previous_path: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        suffix = Path(source_path).suffix[:20]
        staged = settings.data_dir / "tmp" / f"{uuid.uuid4().hex}{suffix}"
        total = 0
        try:
            with staged.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File exceeds the {settings.max_upload_mb} MB upload limit",
                        )
                    handle.write(chunk)
            if source_size != total:
                raise ValueError("Upload size does not match the agent manifest")
            service.heartbeat(agent["id"])
            return await service.process_file(
                agent=agent,
                root_id=root_id,
                source_path=source_path,
                source_mtime_ns=source_mtime_ns,
                source_size=source_size,
                staged_file=staged,
                claimed_hash=sha256,
                previous_path=previous_path,
            )
        finally:
            await file.close()
            staged.unlink(missing_ok=True)

    @app.post("/api/v1/agent/documents/missing")
    async def missing_document(payload: MissingRequest, agent: AgentDependency) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        return service.mark_missing(agent["id"], payload.root_id, payload.source_path)

    @app.get("/api/v1/agent/commands")
    async def commands(agent: AgentDependency) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        return {"items": service.pending_commands(agent["id"])}

    @app.post("/api/v1/agent/commands/{command_id}/complete")
    async def command_complete(
        command_id: str,
        payload: CommandCompleteRequest,
        agent: AgentDependency,
    ) -> dict[str, bool]:
        service.complete_command(agent["id"], command_id, payload.result, payload.ok)
        return {"ok": True}

    static_dir = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
