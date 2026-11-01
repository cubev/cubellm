import hmac
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from ipaddress import ip_address
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

from pydantic import SecretStr

SESSION_COOKIE: Final = "cube_control_session"
SESSION_LIFETIME: Final = timedelta(hours=8)
MINIMUM_TOKEN_LENGTH: Final = 32
MAX_SESSIONS: Final = 32
FAILED_LOGIN_LIMIT: Final = 10
FAILED_LOGIN_WINDOW: Final = timedelta(minutes=15)
SESSION_ID: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")
OPERATOR: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}$")


@dataclass(frozen=True, slots=True)
class BrowserSession:
    session_id: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementOrigin:
    origin: str
    host: str
    secure: bool


def management_origin(raw: str) -> ManagementOrigin | None:
    try:
        parsed: Final = urlsplit(raw)
        hostname: Final = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ("http", "https")
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    if parsed.scheme == "http" and not _loopback(hostname):
        return None
    return ManagementOrigin(f"{parsed.scheme}://{parsed.netloc}", parsed.netloc, parsed.scheme == "https")


def _loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(slots=True)
class SessionAuthority:
    token: SecretStr
    origin: ManagementOrigin
    operator: str
    sessions: MappingProxyType[str, BrowserSession] = field(default_factory=lambda: MappingProxyType({}))
    failures: tuple[datetime, ...] = ()
    lock: threading.Lock = field(default_factory=threading.Lock)

    def login(self, candidate: str, now: datetime) -> BrowserSession | str:
        with self.lock:
            self.failures = tuple(at for at in self.failures if now - at < FAILED_LOGIN_WINDOW)
            if len(self.failures) >= FAILED_LOGIN_LIMIT:
                return "locked"
            if not hmac.compare_digest(candidate.encode(), self.token.get_secret_value().encode()):
                self.failures = (*self.failures, now)
                return "rejected"
            live: Final = {key: value for key, value in self.sessions.items() if value.expires_at > now}
            if len(live) >= MAX_SESSIONS:
                self.sessions = MappingProxyType(live)
                return "full"
            session: Final = BrowserSession(
                secrets.token_urlsafe(32), secrets.token_urlsafe(32), now + SESSION_LIFETIME
            )
            self.sessions = MappingProxyType({**live, session.session_id: session})
            self.failures = ()
            return session

    def verify(self, cookie: str | None, now: datetime) -> BrowserSession | None:
        if cookie is None or not SESSION_ID.fullmatch(cookie):
            return None
        session: Final = self.sessions.get(cookie)
        return session if session is not None and session.expires_at > now else None

    def csrf_matches(self, session: BrowserSession, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate.encode(), session.csrf_token.encode())

    def logout(self, session: BrowserSession) -> None:
        with self.lock:
            self.sessions = MappingProxyType(
                {key: value for key, value in self.sessions.items() if key != session.session_id}
            )


def private_authority(token: SecretStr, origin: str, operator: str) -> SessionAuthority | None:
    configured: Final = management_origin(origin)
    if configured is None or len(token.get_secret_value()) < MINIMUM_TOKEN_LENGTH or not OPERATOR.fullmatch(operator):
        return None
    return SessionAuthority(token, configured, operator)
