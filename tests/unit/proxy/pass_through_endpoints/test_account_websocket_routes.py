import asyncio
import time
from collections.abc import Callable, Iterator, Mapping
from hashlib import sha256
from typing import Final, Self

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.exceptions import WebSocketException
from starlette.websockets import WebSocketDisconnect

from litellm.models.credentials import CredentialItem
from litellm.proxy._types import PassThroughGenericEndpoint, UserAPIKeyAuth
from litellm.proxy.pass_through_endpoints import pass_through_endpoints as module
from litellm.proxy.pass_through_endpoints.account_credentials import (
    AccountHeaderCredential,
    resolve_account_credential,
    validate_account_route,
)

KEY: Final = "sk-fixture-application-key"
KEY_HASH: Final = sha256(KEY.encode()).hexdigest()
PATH: Final = "/cube_api/fixture/realtime"
POLICY: Final = AccountHeaderCredential.model_validate(
    {
        "account_id": "fixture-account",
        "credential_name": "fixture credential",
        "headers": [{"name": "Authorization", "field": "api key", "prefix": "Bearer "}],
        "allowed_query_parameters": ["model"],
        "application_grants": [
            {
                "application_id": "fixture-app",
                "key_hash": KEY_HASH,
                "account_id": "fixture-account",
                "path": PATH,
                "methods": ["WEBSOCKET"],
            }
        ],
    }
)
CREDENTIAL: Final = CredentialItem(
    credential_name="fixture credential",
    credential_values={"api key": "inert-provider-canary"},
    credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": ["https://vendor.example"]},
)


@pytest.mark.parametrize(
    ("target", "methods", "changes", "message"),
    (
        ("https://vendor.example/v1/realtime", ("WEBSOCKET",), {}, "ws or wss target"),
        ("wss://vendor.example/v1/realtime", ("GET",), {}, "ws or wss target"),
        ("wss://vendor.example/v1/realtime", ("WEBSOCKET", "GET"), {}, "only the WEBSOCKET method"),
        ("ws://vendor.example/v1/realtime", ("WEBSOCKET",), {}, "HTTPS outside literal loopback"),
        ("wss://vendor.example/v1/realtime", ("WEBSOCKET",), {"query": {"name": "key", "field": "api key"}}, "header"),
        (
            "wss://vendor.example/v1/realtime",
            ("WEBSOCKET",),
            {"mtls": {"certificate_field": "a", "private_key_field": "b"}},
            "header",
        ),
    ),
)
def test_websocket_routes_are_restricted_to_header_credentials_over_websocket_targets(
    target: str, methods: tuple[str, ...], changes: Mapping[str, object], message: str
) -> None:
    policy: Final = AccountHeaderCredential.model_validate(POLICY.model_dump() | dict(changes))
    with pytest.raises(ValueError, match=message):
        validate_account_route(target, True, False, {}, None, policy, methods)


def test_valid_websocket_routes_resolve_against_the_https_origin_of_the_credential() -> None:
    validate_account_route("wss://vendor.example/v1/realtime", True, False, {}, None, POLICY, ("WEBSOCKET",))
    validate_account_route("ws://127.0.0.1:9/v1/realtime", True, False, {}, None, POLICY, ("WEBSOCKET",))
    resolved: Final = resolve_account_credential(POLICY, CREDENTIAL, "wss://vendor.example/v1/realtime")
    assert resolved.headers == {"Authorization": "Bearer inert-provider-canary"}
    assert resolved.binding.origin == "https://vendor.example"
    with pytest.raises(ValueError, match="not allowed for this origin"):
        resolve_account_credential(POLICY, CREDENTIAL, "wss://other.example/v1/realtime")


def test_stored_websocket_routes_cannot_forward_subpaths() -> None:
    definition: Final = {
        "path": PATH,
        "target": "wss://vendor.example/v1/realtime",
        "auth": True,
        "methods": ["WEBSOCKET"],
        "account_credential": POLICY.model_dump(),
    }
    assert PassThroughGenericEndpoint.model_validate(definition).methods == ["WEBSOCKET"]
    with pytest.raises(ValidationError, match="cannot forward subpaths"):
        PassThroughGenericEndpoint.model_validate(definition | {"include_subpath": True})


class Relay:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []  # mutable-ok: test recorder

    async def __call__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        websocket: Final = kwargs["websocket"]
        assert isinstance(websocket, WebSocket)
        await websocket.accept()
        await websocket.send_json({"relayed": True})
        await websocket.close()


ROUTE_KEY: Final = f"fixture:exact:{PATH}:WEBSOCKET"


class Gateway:
    """The gateway state a handshake and every later recheck read: registered route, credential and valid keys."""

    def __init__(self) -> None:
        self.credential: CredentialItem = CREDENTIAL
        self.deleted_keys: Final[set[str]] = set()  # mutable-ok: test state
        self.application_of_key = "fixture-app"
        self.authentications = 0


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[Gateway]:
    state: Final = Gateway()

    async def authenticate(websocket: WebSocket) -> UserAPIKeyAuth:
        state.authentications += 1
        token: Final = websocket.headers.get("authorization", "").removeprefix("Bearer ")
        if token in state.deleted_keys:
            raise WebSocketException(code=1008)
        if token not in (KEY, "sk-other-key"):
            await websocket.close(code=1008)
            raise HTTPException(status_code=403, detail="No API key provided")
        return UserAPIKeyAuth(
            token=sha256(token.encode()).hexdigest(),
            metadata={"cube_application_id": state.application_of_key if token == KEY else "other-app"},
        )

    monkeypatch.setattr("litellm.proxy.auth.user_api_key_auth.user_api_key_auth_websocket", authenticate)
    monkeypatch.setattr(module.CredentialAccessor, "find_credential", staticmethod(lambda name: state.credential))
    module._registered_pass_through_routes.clear()
    module._registered_pass_through_routes[ROUTE_KEY] = {
        "endpoint_id": "fixture",
        "path": PATH,
        "type": "exact",
        "methods": ["WEBSOCKET"],
        "auth": True,
        "passthrough_params": {
            "target": "wss://vendor.example/v1/realtime",
            "custom_headers": {"OpenAI-Beta": "realtime=v1"},
            "forward_headers": False,
            "account_credential": POLICY,
        },
    }
    yield state
    module._registered_pass_through_routes.clear()


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch, gateway: Gateway) -> Relay:
    recorder: Final = Relay()
    monkeypatch.setattr(module, "websocket_passthrough_request", recorder)
    return recorder


def client(recheck_seconds: float = 5.0) -> TestClient:
    app: Final = FastAPI()
    _ = module.SafeRouteAdder.add_websocket_route_if_not_exists(
        app, PATH, module.create_account_websocket_route(PATH, recheck_seconds)
    )
    assert not module.SafeRouteAdder.add_websocket_route_if_not_exists(
        app, PATH, module.create_account_websocket_route(PATH)
    )
    return TestClient(app)


def test_the_granted_application_reaches_the_provider_with_only_the_account_credential(relay: Relay) -> None:
    with client().websocket_connect(
        PATH + "?model=fixture", headers={"Authorization": f"Bearer {KEY}", "X-Client": "dropped"}
    ) as socket:
        assert socket.receive_json() == {"relayed": True}
    (call,) = relay.calls
    assert call["target"] == "wss://vendor.example/v1/realtime?model=fixture"
    assert call["custom_headers"] == {"OpenAI-Beta": "realtime=v1", "Authorization": "Bearer inert-provider-canary"}
    assert call["forward_headers"] is False
    binding: Final = call["account_binding"]
    assert getattr(binding, "account_id", None) == "fixture-account"


@pytest.mark.parametrize(
    ("headers", "query"),
    (
        ({"Authorization": "Bearer sk-other-key"}, ""),
        ({}, ""),
        ({"Authorization": f"Bearer {KEY}"}, "?unknown=1"),
        ({"Authorization": f"Bearer {KEY}", "X-Pass-Authorization": "forged"}, ""),
    ),
)
def test_denied_handshakes_never_reach_the_provider(relay: Relay, headers: dict[str, str], query: str) -> None:
    with pytest.raises(WebSocketDisconnect), client().websocket_connect(PATH + query, headers=headers) as socket:
        _ = socket.receive_json()
    assert relay.calls == []


def test_a_deleted_route_or_revoked_grant_applies_to_the_next_handshake(relay: Relay) -> None:
    connection: Final = client()
    revoked: Final = POLICY.model_copy(update={"application_grants": ()})
    module._registered_pass_through_routes[ROUTE_KEY]["passthrough_params"]["account_credential"] = revoked
    with (
        pytest.raises(WebSocketDisconnect),
        connection.websocket_connect(PATH, headers={"Authorization": f"Bearer {KEY}"}) as socket,
    ):
        _ = socket.receive_json()
    module._registered_pass_through_routes.clear()
    with (
        pytest.raises(WebSocketDisconnect),
        connection.websocket_connect(PATH, headers={"Authorization": f"Bearer {KEY}"}) as socket,
    ):
        _ = socket.receive_json()
    assert relay.calls == []


def test_account_credentials_in_upstream_handshake_headers_are_masked_in_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.proxy.pass_through_endpoints.account_credentials import AccountRouteBinding

    logged: Final[list[object]] = []  # mutable-ok: test recorder
    upstream: Final[list[object]] = []  # mutable-ok: test recorder
    original: Final = Logging.pre_call

    def pre_call(
        self: Logging,
        input: object,
        api_key: object,
        additional_args: Mapping[str, object] | None = None,
        **rest: object,
    ) -> None:
        logged.append(dict(additional_args or {}))
        original(self, input, api_key, additional_args=additional_args, **rest)

    def refuse(target: str, additional_headers: Mapping[str, str]) -> object:
        upstream.append(dict(additional_headers))
        raise OSError("refused")

    monkeypatch.setattr(Logging, "pre_call", pre_call)
    monkeypatch.setattr(module, "connect", refuse)
    app: Final = FastAPI()

    async def endpoint(websocket: WebSocket) -> None:
        await module.websocket_passthrough_request(
            websocket=websocket,
            target="wss://vendor.example/v1/realtime",
            custom_headers={"Authorization": "Bearer inert-provider-canary"},
            user_api_key_dict=UserAPIKeyAuth(token=KEY_HASH),
            endpoint=PATH,
            account_binding=AccountRouteBinding("fixture-account", "fixture credential", "https://vendor.example"),
        )

    app.add_api_websocket_route(PATH, endpoint)
    with pytest.raises(WebSocketDisconnect), TestClient(app).websocket_connect(PATH) as socket:
        _ = socket.receive_json()
    assert upstream and upstream[0]["Authorization"] == "Bearer inert-provider-canary"
    headers: Final = [entry["headers"] for entry in logged if isinstance(entry, dict) and "headers" in entry]
    assert headers and all("inert-provider-canary" not in str(value) for value in headers), headers


class Provider:
    """Scripted provider socket: echoes every frame it is sent and records the handshake and its own closing."""

    def __init__(self) -> None:
        self._frames: Final[asyncio.Queue[str | bytes]] = asyncio.Queue()
        self.handshakes: Final[list[Mapping[str, str]]] = []  # mutable-ok: test recorder
        self.closed = False

    def connect(self, target: str, additional_headers: Mapping[str, str]) -> Self:
        self.handshakes.append(dict(additional_headers))
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def send(self, frame: str | bytes) -> None:
        await self._frames.put(frame)

    async def recv(self) -> str | bytes:
        return await self._frames.get()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, gateway: Gateway) -> Provider:
    scripted: Final = Provider()
    monkeypatch.setattr(module, "connect", scripted.connect)
    return scripted


def _revoke_grant(gateway: Gateway) -> None:
    module._registered_pass_through_routes[ROUTE_KEY]["passthrough_params"]["account_credential"] = POLICY.model_copy(
        update={"application_grants": ()}
    )


def _delete_route(gateway: Gateway) -> None:
    module._registered_pass_through_routes.clear()


def _rotate_credential(gateway: Gateway) -> None:
    gateway.credential = CREDENTIAL.model_copy(update={"credential_values": {"api key": "rotated-provider-canary"}})


def _delete_key(gateway: Gateway) -> None:
    gateway.deleted_keys.add(KEY)


def _rebind_key_to_another_application(gateway: Gateway) -> None:
    gateway.application_of_key = "other-app"


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "revoke", (_revoke_grant, _delete_route, _rotate_credential, _delete_key, _rebind_key_to_another_application)
)
def test_an_open_connection_is_cut_once_its_authorization_no_longer_holds(
    gateway: Gateway, provider: Provider, revoke: Callable[[Gateway], None]
) -> None:
    with client(recheck_seconds=0.01).websocket_connect(PATH, headers={"Authorization": f"Bearer {KEY}"}) as socket:
        socket.send_text("before")
        assert socket.receive_text() == "before"
        revoke(gateway)
        with pytest.raises(WebSocketDisconnect) as closed:
            _ = socket.receive_text()
    assert closed.value.code == 1008, closed.value
    assert provider.closed


def test_an_open_connection_survives_rechecks_while_its_authorization_holds(
    gateway: Gateway, provider: Provider
) -> None:
    with client(recheck_seconds=0.01).websocket_connect(PATH, headers={"Authorization": f"Bearer {KEY}"}) as socket:
        socket.send_text("first")
        assert socket.receive_text() == "first"
        deadline: Final = time.monotonic() + 10
        while gateway.authentications < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gateway.authentications >= 4, "the open connection was never rechecked"
        socket.send_text("second")
        assert socket.receive_text() == "second"
    assert len(provider.handshakes) == 1


def test_a_credential_echoed_by_the_provider_is_redacted_in_frames_and_in_the_logged_frames(
    monkeypatch: pytest.MonkeyPatch, gateway: Gateway, provider: Provider
) -> None:
    logged: Final[list[object]] = []  # mutable-ok: test recorder

    async def record(**kwargs: object) -> None:
        logged.append(kwargs["response_body"])

    monkeypatch.setattr(module.pass_through_endpoint_logging, "pass_through_async_success_handler", record)
    with client().websocket_connect(PATH, headers={"Authorization": f"Bearer {KEY}"}) as socket:
        socket.send_text('{"echo": "Bearer inert-provider-canary", "kept": "inert-provider"}')
        assert socket.receive_text() == '{"echo": "Bearer [redacted]", "kept": "inert-provider"}'
        socket.send_bytes(b"\x00inert-provider-canary\xff")
        assert socket.receive_bytes() == b"\x00[redacted]\xff"
    assert provider.handshakes == [{"OpenAI-Beta": "realtime=v1", "Authorization": "Bearer inert-provider-canary"}]
    assert logged == [[{"echo": "Bearer [redacted]", "kept": "inert-provider"}]]
