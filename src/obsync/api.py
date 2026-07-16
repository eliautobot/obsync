from __future__ import annotations

import ipaddress
import json
import os
import socket
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .service import LoginRateLimitedError, ObsyncService, PipelinePausedError

SESSION_COOKIE = "obsync_session"
CSRF_COOKIE = "obsync_csrf"


class RegistrationRequest(BaseModel):
    code: str
    name: str = ""
    hostname: str = ""
    os_name: str = ""
    os_version: str = ""
    agent_version: str = ""
    agent_token: str = Field(default="", max_length=256)


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


class InventoryItem(BaseModel):
    source_path: str
    source_mtime_ns: int = Field(ge=0)
    source_size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class InventoryRequest(BaseModel):
    root_id: str
    scan_id: str
    items: list[InventoryItem] = Field(default_factory=list, max_length=500)
    complete: bool = False


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


class AccountUpdateRequest(BaseModel):
    username: str
    current_password: str
    new_password: str = ""


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
    if service.verify_admin(token):
        return {"legacy": True}
    if not service.has_admin_account() and _is_local_admin_request(request, service.settings):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin_request(request):
            raise HTTPException(
                status_code=403, detail="Cross-site temporary admin request blocked"
            )
        return {"temporary": True, "username": "Admin"}
    raise HTTPException(status_code=401, detail="Sign in required")


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


@lru_cache(maxsize=1)
def _container_gateway_ips() -> frozenset[str]:
    """Return container-host gateway addresses without trusting a normal host's router."""
    if not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()):
        return frozenset()
    route_file = Path("/proc/net/route")
    if not route_file.is_file():
        return frozenset()
    gateways: set[str] = set()
    for line in route_file.read_text(encoding="utf-8", errors="ignore").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            if int(fields[3], 16) & 2:
                gateways.add(socket.inet_ntoa(bytes.fromhex(fields[2])[::-1]))
        except (OSError, ValueError):
            continue
    return frozenset(gateways)


def _is_local_admin_request(request: Request, settings: Settings) -> bool:
    client_value = _client_ip(request)
    try:
        client_ip = ipaddress.ip_address(client_value)
    except ValueError:
        return False
    if client_value in settings.local_setup_ips:
        return True
    if not (client_ip.is_loopback or client_value in _container_gateway_ips()):
        return False
    target = (request.url.hostname or "").strip().casefold().rstrip(".")
    if target == "localhost" or target.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(target).is_loopback
    except ValueError:
        return False


def _same_origin_request(request: Request) -> bool:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return True
    parsed = urlsplit(origin)
    expected_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    actual_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (
        parsed.scheme.lower() == request.url.scheme.lower()
        and (parsed.hostname or "").lower() == (request.url.hostname or "").lower()
        and actual_port == expected_port
    )


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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        service.start_background_tasks()
        try:
            yield
        finally:
            await service.stop_background_tasks()

    app = FastAPI(
        title="Obsync API",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
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

    @app.exception_handler(PipelinePausedError)
    async def pipeline_paused_handler(_request: Request, exc: PipelinePausedError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

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

    @app.get("/api/v1/downloads/windows-desktop", include_in_schema=False)
    async def download_windows_desktop() -> Response:
        filename = "obsync-desktop-windows-x64.exe"
        bundled = Path(__file__).with_name("downloads") / filename
        if bundled.is_file():
            return FileResponse(
                bundled,
                filename=filename,
                media_type="application/vnd.microsoft.portable-executable",
            )
        return RedirectResponse(
            f"https://github.com/eliautobot/obsync/releases/download/v{__version__}/{filename}",
            status_code=307,
        )

    @app.get("/api/v1/downloads/windows-companion", include_in_schema=False)
    async def download_windows_companion() -> Response:
        return RedirectResponse("/api/v1/downloads/windows-desktop", status_code=307)

    @app.get("/api/v1/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        status = service.setup_status()
        local = _is_local_admin_request(request, settings)
        result: dict[str, Any] = {
            **status,
            "legacy_migration_required": status["legacy_migration_required"] and local,
            "account_registered": not status["setup_required"],
            "temporary_admin_available": status["setup_required"] and local,
        }
        session = service.authenticate_admin_session(request.cookies.get(SESSION_COOKIE, ""))
        if session:
            result.update(
                {
                    "authenticated": True,
                    "username": session["username"],
                    "legacy": False,
                    "temporary": False,
                }
            )
        return result

    @app.post("/api/v1/auth/setup")
    def auth_setup(payload: SetupRequest, request: Request, response: Response) -> dict[str, Any]:
        local = _is_local_admin_request(request, settings)
        if local and not _same_origin_request(request):
            raise HTTPException(status_code=403, detail="Cross-site account setup blocked")
        if not local and not service.has_admin_account():
            raise HTTPException(
                status_code=403,
                detail="Create the administrator account from the Obsync server itself",
            )
        try:
            user = service.create_admin_account(
                payload.username,
                payload.password,
                legacy_token=payload.legacy_token,
                trusted_local=local,
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
            if (
                _is_local_admin_request(request, settings)
                and payload.username.strip().casefold() == "admin"
                and not payload.password
            ):
                if not _same_origin_request(request):
                    raise HTTPException(
                        status_code=403, detail="Cross-site temporary admin login blocked"
                    )
                return {"authenticated": True, "username": "Admin", "temporary": True}
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
            "username": session.get("username", "Admin"),
            "legacy": bool(session.get("legacy")),
            "temporary": bool(session.get("temporary")),
            "account_registered": not bool(session.get("temporary") or session.get("legacy")),
        }

    @app.get("/api/v1/admin/overview")
    async def overview(_token: AdminDependency) -> dict[str, Any]:
        return service.overview()

    @app.get("/api/v1/admin/pipeline")
    async def pipeline_status(_token: AdminDependency) -> dict[str, Any]:
        return service.pipeline_status()

    @app.post("/api/v1/admin/pipeline/stop")
    async def stop_pipeline(_token: AdminDependency) -> dict[str, Any]:
        return service.pause_pipeline()

    @app.post("/api/v1/admin/pipeline/start")
    async def start_pipeline(_token: AdminDependency) -> dict[str, Any]:
        return service.resume_pipeline()

    @app.get("/api/v1/admin/ai/activity")
    async def ai_activity(_token: AdminDependency) -> dict[str, Any]:
        return service.ai_activity()

    @app.get("/api/v1/admin/ai/activity/stream")
    async def ai_activity_stream(_token: AdminDependency) -> StreamingResponse:
        async def events():
            stream = service.stream_ai_activity()
            yield "retry: 1000\n\n"
            try:
                async for activity in stream:
                    if activity is None:
                        yield ": keep-alive\n\n"
                        continue
                    payload = json.dumps(activity, ensure_ascii=False, separators=(",", ":"))
                    yield (f"id: {activity['revision']}\nevent: ai-activity\ndata: {payload}\n\n")
            finally:
                await stream.aclose()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/admin/ai/stop")
    async def stop_ai_inference(
        _token: AdminDependency, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return service.stop_inference(str((payload or {}).get("document_id", "")))

    @app.get("/api/v1/admin/server")
    async def server_info(_token: AdminDependency) -> dict[str, Any]:
        return service.server_info()

    @app.put("/api/v1/admin/account")
    async def update_account(
        payload: AccountUpdateRequest,
        session: AdminDependency,
    ) -> dict[str, Any]:
        if session.get("temporary") or session.get("legacy") or not session.get("user_id"):
            raise HTTPException(status_code=409, detail="Secure the administrator account first")
        try:
            return service.update_admin_account(
                int(session["user_id"]),
                current_password=payload.current_password,
                username=payload.username,
                new_password=payload.new_password,
                keep_session_token=str(session.get("_raw_token", "")),
            )
        except ValueError as exc:
            status = 401 if "Current password" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/api/v1/admin/agents")
    async def agents(_token: AdminDependency) -> dict[str, Any]:
        return {"items": service.list_agents()}

    @app.delete("/api/v1/admin/agents/{agent_id}")
    async def disconnect_agent(agent_id: str, _token: AdminDependency) -> dict[str, Any]:
        try:
            return service.disconnect_agent(agent_id)
        except ValueError as exc:
            status = 409 if "vault writer" in str(exc).lower() else 404
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/api/v1/admin/roots")
    async def roots(_token: AdminDependency) -> dict[str, Any]:
        return {"items": service.list_roots()}

    @app.delete("/api/v1/admin/roots/{root_id}")
    async def remove_root(root_id: str, _token: AdminDependency) -> dict[str, Any]:
        try:
            return service.remove_root(root_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/admin/enrollments")
    async def create_enrollment(
        _token: AdminDependency, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        return service.create_enrollment(
            label=str(payload.get("label", "")), minutes=int(payload.get("minutes", 20))
        )

    @app.get("/api/v1/admin/enrollments/{enrollment_id}")
    async def enrollment_status(enrollment_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.enrollment_status(enrollment_id)

    @app.post("/api/v1/admin/agents/{agent_id}/scan")
    async def scan_agent(agent_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.queue_command(agent_id, "scan")

    @app.post("/api/v1/admin/agents/{agent_id}/select-source")
    async def select_agent_source(
        agent_id: str,
        _token: AdminDependency,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        return service.queue_command(
            agent_id,
            "select_source",
            {
                "name": str(payload.get("name", ""))[:120],
                "destination": str(payload.get("destination", "Obsync"))[:500],
            },
        )

    @app.post("/api/v1/admin/roots/{root_id}/scan")
    async def scan_root(root_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.queue_root_command(root_id, "scan_root")

    @app.post("/api/v1/admin/roots/{root_id}/sync")
    async def sync_root(root_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.queue_root_command(root_id, "sync_root")

    @app.post("/api/v1/admin/roots/{root_id}/state")
    async def set_root_state(
        root_id: str, payload: dict[str, Any], _token: AdminDependency
    ) -> dict[str, Any]:
        return service.set_root_state(root_id, str(payload.get("sync_state", "")))

    @app.get("/api/v1/admin/commands/{command_id}")
    async def command_status(command_id: str, _token: AdminDependency) -> dict[str, Any]:
        command = service.db.query_one("SELECT * FROM commands WHERE id = ?", (command_id,))
        if not command:
            raise HTTPException(status_code=404, detail="Command not found")
        command["payload"] = json.loads(command.pop("payload_json") or "{}")
        return command

    @app.post("/api/v1/admin/agents/{agent_id}/select-vault")
    async def select_agent_vault(agent_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.queue_command(agent_id, "select_vault")

    @app.get("/api/v1/admin/vault/sweeps")
    async def vault_sweeps(_token: AdminDependency) -> dict[str, Any]:
        return service.vault_sweep_status()

    @app.post("/api/v1/admin/vault/sweeps/{sweep_type}/start")
    async def start_vault_sweep(
        sweep_type: str,
        _token: AdminDependency,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        return service.start_vault_sweep(
            sweep_type,
            change_mode=str(payload.get("change_mode", "")),
            full_rebuild=bool(payload.get("full_rebuild")),
        )

    @app.post("/api/v1/admin/vault/sweeps/{sweep_id}/stop")
    async def stop_vault_sweep(sweep_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.stop_vault_sweep(sweep_id)

    @app.post("/api/v1/admin/vault/sweeps/{sweep_id}/undo")
    async def undo_vault_sweep(sweep_id: str, _token: AdminDependency) -> dict[str, Any]:
        return await service.undo_vault_sweep(sweep_id)

    @app.get("/api/v1/admin/vault/changes")
    async def vault_changes(
        _token: AdminDependency,
        status: str = "pending",
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return service.list_vault_changes(status=status, limit=limit, offset=offset)

    @app.get("/api/v1/admin/vault/changes/{change_id}")
    async def vault_change(change_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.vault_change_diff(change_id)

    @app.post("/api/v1/admin/vault/changes/{change_id}/approve")
    async def approve_vault_change(change_id: str, _token: AdminDependency) -> dict[str, Any]:
        return await service.approve_vault_change(change_id)

    @app.post("/api/v1/admin/vault/changes/{change_id}/reject")
    async def reject_vault_change(change_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.reject_vault_change(change_id)

    @app.get("/api/v1/admin/documents")
    async def documents(
        _token: AdminDependency,
        status: str = "",
        comparison_status: str = "",
        root_id: str = "",
        search: str = "",
        review: bool | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return service.list_documents(
            status=status,
            comparison_status=comparison_status,
            root_id=root_id,
            search=search,
            review=review,
            limit=limit,
            offset=offset,
        )

    @app.post("/api/v1/admin/documents/{document_id}/approve")
    async def approve_document(document_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.approve_document(document_id)

    @app.post("/api/v1/admin/documents/{document_id}/disregard")
    async def disregard_document(document_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.disregard_document(document_id)

    @app.post("/api/v1/admin/documents/{document_id}/redo-review")
    async def redo_ai_review(
        document_id: str,
        _token: AdminDependency,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return service.redo_ai_review(document_id, str((payload or {}).get("feedback", "")))

    @app.post("/api/v1/admin/documents/{document_id}/allow-duplicate")
    async def allow_duplicate(document_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.allow_duplicate(document_id)

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

    @app.get("/api/v1/admin/ai/profiles")
    async def get_ai_profiles(_token: AdminDependency) -> dict[str, Any]:
        return service.ai_profiles_for_ui()

    @app.post("/api/v1/admin/ai/profiles")
    async def create_ai_profile(payload: dict[str, Any], _token: AdminDependency) -> dict[str, Any]:
        return service.create_ai_profile(payload)

    @app.put("/api/v1/admin/ai/profiles/{profile_id}")
    async def update_ai_profile(
        profile_id: str, payload: dict[str, Any], _token: AdminDependency
    ) -> dict[str, Any]:
        return service.update_ai_profile(profile_id, payload)

    @app.post("/api/v1/admin/ai/profiles/{profile_id}/activate")
    async def activate_ai_profile(profile_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.activate_ai_profile(profile_id)

    @app.delete("/api/v1/admin/ai/profiles/{profile_id}")
    async def delete_ai_profile(profile_id: str, _token: AdminDependency) -> dict[str, Any]:
        return service.delete_ai_profile(profile_id)

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

    @app.get("/api/v1/agent/status")
    async def agent_status(agent: AgentDependency) -> dict[str, Any]:
        return {
            "connected": True,
            "agent_id": agent["id"],
            "name": agent["name"],
            "server_version": __version__,
            "sync_enabled": service.pipeline_enabled(),
        }

    @app.post("/api/v1/agent/heartbeat")
    async def heartbeat(payload: dict[str, Any], agent: AgentDependency) -> dict[str, Any]:
        service.heartbeat(
            agent["id"],
            str(payload.get("agent_version", "")),
            vault_path=str(payload.get("vault_path", "")) if "vault_path" in payload else None,
            vault_ready=bool(payload.get("vault_ready")) if "vault_ready" in payload else None,
            vault_error=str(payload.get("vault_error", "")) if "vault_error" in payload else None,
        )
        return {"ok": True, "sync_enabled": service.pipeline_enabled()}

    @app.post("/api/v1/agent/sweeps/{sweep_id}/progress")
    async def sweep_progress(
        sweep_id: str, payload: dict[str, Any], agent: AgentDependency
    ) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        return service.agent_sweep_progress(
            agent["id"],
            sweep_id,
            processed=int(payload.get("processed", 0)),
            total=int(payload.get("total", 0)),
            current_note=str(payload.get("current_note", "")),
        )

    @app.post("/api/v1/agent/sweeps/{sweep_id}/notes")
    async def sweep_notes(
        sweep_id: str, payload: dict[str, Any], agent: AgentDependency
    ) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        raw_notes = payload.get("notes", [])
        if not isinstance(raw_notes, list):
            raise ValueError("Vault index batch is invalid")
        return service.agent_sweep_notes(agent["id"], sweep_id, raw_notes)

    @app.post("/api/v1/agent/roots")
    async def upsert_root(payload: RootRequest, agent: AgentDependency) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        return service.upsert_root(agent["id"], payload.model_dump())

    @app.get("/api/v1/agent/roots/{root_id}/pending")
    async def pending_root(root_id: str, agent: AgentDependency) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        return {"items": service.pending_root_documents(agent["id"], root_id)}

    @app.post("/api/v1/agent/inventory")
    async def inventory(payload: InventoryRequest, agent: AgentDependency) -> dict[str, Any]:
        service.heartbeat(agent["id"])
        return service.inventory_files(
            agent=agent,
            root_id=payload.root_id,
            scan_id=payload.scan_id,
            items=[item.model_dump() for item in payload.items],
            complete=payload.complete,
        )

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
        duplicate_path: Annotated[str, Form()] = "",
        duplicate_title: Annotated[str, Form()] = "",
        review_feedback: Annotated[str, Form()] = "",
        force_review: Annotated[bool, Form()] = False,
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
                duplicate_path=duplicate_path,
                duplicate_title=duplicate_title,
                review_feedback=review_feedback,
                force_review=force_review,
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
        return {
            "items": service.pending_commands(agent["id"]),
            "sync_enabled": service.pipeline_enabled(),
        }

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
