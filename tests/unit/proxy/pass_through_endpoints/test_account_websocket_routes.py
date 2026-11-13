from collections.abc import Iterator, Mapping
from hashlib import sha256
from typing import Final

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
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


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[Relay]:
    recorder: Final = Relay()

    async def authenticate(websocket: WebSocket) -> UserAPIKeyAuth:
        token: Final = websocket.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in (KEY, "sk-other-key"):
            await websocket.close(code=1008)
            raise HTTPException(status_code=403, detail="No API key provided")
        return UserAPIKeyAuth(
            token=sha256(token.encode()).hexdigest(),
            metadata={"cube_application_id": "fixture-app" if token == KEY else "other-app"},
        )

    monkeypatch.setattr(module, "websocket_passthrough_request", recorder)
    monkeypatch.setattr("litellm.proxy.auth.user_api_key_auth.user_api_key_auth_websocket", authenticate)
    monkeypatch.setattr(module.CredentialAccessor, "find_credential", staticmethod(lambda name: CREDENTIAL))
    module._registered_pass_through_routes.clear()
    module._registered_pass_through_routes[f"fixture:exact:{PATH}:WEBSOCKET"] = {
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
    yield recorder
    module._registered_pass_through_routes.clear()


def client() -> TestClient:
    app: Final = FastAPI()
    _ = module.SafeRouteAdder.add_websocket_route_if_not_exists(app, PATH, module.create_account_websocket_route(PATH))
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
    module._registered_pass_through_routes[f"fixture:exact:{PATH}:WEBSOCKET"]["passthrough_params"][
        "account_credential"
    ] = revoked
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
