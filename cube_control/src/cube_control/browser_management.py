from collections.abc import Callable
from datetime import datetime
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from cube_control.browser_session import SESSION_COOKIE, SESSION_LIFETIME, BrowserSession, SessionAuthority
from cube_control.credential_binding import CredentialBindingCommand
from cube_control.models import ManualCommand
from cube_control.store import Repository

MAX_BODY: Final = 65536
COOKIE_PATH: Final = "/api/v1"
STATUS_CODES: Final = {"applied": 200, "duplicate": 200, "conflict": 409, "rejected": 422, "unavailable": 503}
LOGIN_FAILURES: Final = {
    "locked": (429, "Zu viele Fehlversuche; später erneut versuchen"),
    "rejected": (401, "Anmeldung abgelehnt"),
    "full": (503, "Zu viele aktive Sitzungen; bestehende Sitzung abmelden"),
}


class _Login(BaseModel):
    token: str


def _denied(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def _bounded_body(request: Request) -> bytes | None:
    declared: Final = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or len(declared) > 9 or int(declared) > MAX_BODY):
        return None
    content: Final = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_BODY:
            return None
        content.extend(chunk)
    return bytes(content)


def add_browser_management(
    app: FastAPI, repository: Repository, authority: SessionAuthority, clock: Callable[[], datetime]
) -> None:
    def trusted(request: Request) -> JSONResponse | None:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return _denied(415, "Nur JSON-Anfragen werden angenommen")
        if (
            request.headers.get("origin") != authority.origin.origin
            or request.headers.get("host") != authority.origin.host
            or request.headers.get("sec-fetch-site") not in (None, "same-origin")
        ):
            return _denied(403, "Anfrage stammt nicht von dieser Oberfläche")
        return None

    def guard(request: Request) -> BrowserSession | JSONResponse:
        rejected: Final = trusted(request)
        if rejected is not None:
            return rejected
        session: Final = authority.verify(request.cookies.get(SESSION_COOKIE), clock())
        if session is None:
            return _denied(401, "Anmeldung erforderlich")
        if not authority.csrf_matches(session, request.headers.get("x-csrf-token")):
            return _denied(403, "Sicherheitsprüfung fehlgeschlagen; bitte neu laden")
        return session

    def granted(session: BrowserSession) -> dict[str, str]:
        return {
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at.isoformat(),
            "actor": authority.operator,
        }

    async def login(request: Request) -> JSONResponse:
        rejected: Final = trusted(request)
        if rejected is not None:
            return rejected
        raw: Final = await _bounded_body(request)
        if raw is None:
            return _denied(413, "Anfrage zu groß")
        try:
            candidate: Final = _Login.model_validate_json(raw).token
        except ValidationError:
            return _denied(422, "Anmeldung abgelehnt")
        outcome: Final = authority.login(candidate, clock())
        if isinstance(outcome, str):
            return _denied(*LOGIN_FAILURES[outcome])
        response: Final = JSONResponse(granted(outcome))
        response.set_cookie(
            SESSION_COOKIE,
            outcome.session_id,
            max_age=int(SESSION_LIFETIME.total_seconds()),
            path=COOKIE_PATH,
            httponly=True,
            samesite="strict",
            secure=authority.origin.secure,
        )
        return response

    async def current(request: Request) -> JSONResponse:
        session: Final = authority.verify(request.cookies.get(SESSION_COOKIE), clock())
        return _denied(401, "Anmeldung erforderlich") if session is None else JSONResponse(granted(session))

    async def logout(request: Request) -> JSONResponse:
        checked: Final = guard(request)
        if isinstance(checked, JSONResponse):
            return checked
        authority.logout(checked)
        response: Final = JSONResponse({"status": "signed_out"})
        response.delete_cookie(
            SESSION_COOKIE, path=COOKIE_PATH, httponly=True, samesite="strict", secure=authority.origin.secure
        )
        return response

    async def command(request: Request, account_id: str) -> JSONResponse:
        checked: Final = guard(request)
        if isinstance(checked, JSONResponse):
            return checked
        raw: Final = await _bounded_body(request)
        if raw is None:
            return _denied(413, "Anfrage zu groß")
        try:
            manual: Final = ManualCommand.model_validate_json(raw)
        except ValidationError:
            return _denied(422, "Befehl abgelehnt; Eingaben werden nicht protokolliert")
        if manual.account_id != account_id:
            return _denied(422, "Befehl gehört zu einem anderen Konto")
        result: Final = repository.manage(manual, authority.operator, clock())
        return JSONResponse(
            {"status": result.status, "version": result.version, "message": result.message},
            status_code=STATUS_CODES[result.status],
        )

    async def binding(request: Request, reference: str) -> JSONResponse:
        checked: Final = guard(request)
        if isinstance(checked, JSONResponse):
            return checked
        raw: Final = await _bounded_body(request)
        if raw is None:
            return _denied(413, "Anfrage zu groß")
        try:
            command: Final = CredentialBindingCommand.model_validate_json(raw)
        except ValidationError:
            return _denied(422, "Befehl abgelehnt; Eingaben werden nicht protokolliert")
        if command.reference != reference:
            return _denied(422, "Befehl gehört zu einem anderen Fund")
        result: Final = repository.bind_credential(command, authority.operator, clock())
        return JSONResponse(
            {"status": result.status, "version": result.version, "message": result.message},
            status_code=STATUS_CODES[result.status],
        )

    app.add_api_route("/api/v1/session", login, methods=["POST"])
    app.add_api_route("/api/v1/session", current, methods=["GET"])
    app.add_api_route("/api/v1/session", logout, methods=["DELETE"])
    app.add_api_route("/api/v1/accounts/{account_id}/commands", command, methods=["POST"])
    app.add_api_route("/api/v1/credential-bindings/{reference}/commands", binding, methods=["POST"])
