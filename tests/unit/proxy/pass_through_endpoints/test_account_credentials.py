from types import MappingProxyType
from typing import Final, cast

import pytest
from pydantic import SecretStr, ValidationError

from litellm.models.credentials import CredentialItem
from litellm.proxy._types import PassThroughGenericEndpoint
from litellm.proxy.pass_through_endpoints.account_credentials import (
    AccountApplicationGrant,
    AccountHeaderCredential,
    CredentialHeader,
    CredentialQuery,
    CredentialSigV4,
    account_application_allowed,
    credential_origin,
    resolve_account_credential,
    validate_account_request,
    validate_account_route,
)
from litellm.proxy.pass_through_endpoints.query_credentials import QueryCredentialAuth
from litellm.proxy.pass_through_endpoints.sigv4_credentials import SigV4CredentialAuth
from litellm.proxy.spend_tracking.spend_tracking_utils import (
    _get_spend_logs_metadata as stored_metadata,  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
)

POLICY: Final = AccountHeaderCredential(
    account_id="fixture-account",
    credential_name="fixture credential",
    headers=(CredentialHeader(name="X-Vendor-Credential", field="access value", prefix="Bearer "),),
    allowed_query_parameters=("page",),
)


GRANTED_POLICY: Final = AccountHeaderCredential.model_validate(
    POLICY.model_dump()
    | {
        "application_grants": [
            {
                "application_id": "fixture-app",
                "key_hash": "a" * 64,
                "account_id": "fixture-account",
                "path": "/reports/usage",
                "methods": ["GET"],
            }
        ]
    }
)


@pytest.mark.parametrize(
    ("application_id", "path", "method", "allowed"),
    (
        ("fixture-app", "/reports/usage", "GET", True),
        (None, "/reports/usage", "GET", False),
        ("other-app", "/reports/usage", "GET", False),
        ("fixture-app", "/reports/usage", "HEAD", False),
        ("fixture-app", "/reports/usage", "POST", False),
        ("fixture-app", "/reports/usage", "get", False),
        ("fixture-app", "/reports", "GET", False),
        ("fixture-app", "/reports/usage/detailed", "GET", False),
        ("fixture-app", "/reports/usage/", "GET", False),
        ("fixture-app", "/reports/other/../usage", "GET", False),
    ),
)
def test_account_application_grants_require_every_scope_dimension(
    application_id: str | None, path: str, method: str, allowed: bool
) -> None:
    assert account_application_allowed(GRANTED_POLICY, application_id, "a" * 64, path, method) is allowed


def test_account_application_grants_deny_empty_policy_and_cannot_survive_account_retargeting() -> None:
    assert not account_application_allowed(POLICY, "fixture-app", "a" * 64, "/reports/usage", "GET")
    with pytest.raises(ValidationError):
        AccountHeaderCredential.model_validate(GRANTED_POLICY.model_dump() | {"account_id": "another-account"})


@pytest.mark.parametrize("key_hash", (None, "b" * 64, "raw-key", "A" * 64))
def test_account_application_grants_require_the_authenticated_key_hash(key_hash: str | None) -> None:
    assert not account_application_allowed(GRANTED_POLICY, "fixture-app", key_hash, "/reports/usage", "GET")


@pytest.mark.parametrize(
    "path",
    ("//host", "/a//b", "/a/../b", "/a/./b", "/a?x=1", "/a#frag", "/a%2fb", "/a\\b", "/a/*", "/{rest:path}"),
)
def test_account_application_grants_reject_ambiguous_or_pattern_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        AccountApplicationGrant.model_validate(GRANTED_POLICY.application_grants[0].model_dump() | {"path": path})


@pytest.mark.parametrize("methods", (("get",), ("*",), ("GET", "GET"), (), ("GET\r\nHost",)))
def test_account_application_grants_require_unique_explicit_methods(methods: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        AccountApplicationGrant.model_validate(GRANTED_POLICY.application_grants[0].model_dump() | {"methods": methods})


def credential(
    value: str | SecretStr = "inert-provider-canary",
    account_id: str = "fixture-account",
    origin: str = "https://vendor.example",
    provider: str | None = None,
) -> CredentialItem:
    return CredentialItem(
        credential_name=POLICY.credential_name,
        credential_values={"access value": value},
        credential_info={"cube_account_id": account_id, "cube_allowed_origins": [origin]}
        | ({"cube_provider_id": provider} if provider is not None else {}),
    )


def stored_info(item: CredentialItem) -> dict[str, object]:
    return cast(dict[str, object], item.credential_info)  # cast-ok: the test mutates the stored mapping in place


def test_native_credential_resolution_is_private_and_has_an_immutable_snapshot() -> None:
    item: Final = credential(SecretStr("inert-provider-canary"))
    resolved: Final = resolve_account_credential(POLICY, item, "https://vendor.example/check")
    assert resolved.headers["X-Vendor-Credential"] == "Bearer inert-provider-canary"
    assert "inert-provider-canary" not in repr(resolved)
    stored_info(item)["cube_account_id"] = "later-account"
    assert resolved.binding.account_id == "fixture-account"
    assert resolved.binding.origin == "https://vendor.example"
    assert isinstance(resolved.headers, MappingProxyType)


@pytest.mark.parametrize("item", (None, credential(account_id="another-account"), credential("invalid\r\nheader")))
def test_missing_mismatched_or_invalid_credentials_fail_without_echoing_values(item: CredentialItem | None) -> None:
    with pytest.raises(ValueError) as error:
        resolve_account_credential(POLICY, item, "https://vendor.example/check")
    assert "invalid\r\nheader" not in str(error.value)
    assert "inert-provider-canary" not in str(error.value)


def test_native_environment_references_are_resolved_at_request_time(monkeypatch: pytest.MonkeyPatch) -> None:
    item: Final = credential("os.environ/CUBE_TEST_ACCOUNT_TOKEN")
    monkeypatch.setenv("CUBE_TEST_ACCOUNT_TOKEN", "first-canary")
    first: Final = resolve_account_credential(POLICY, item, "https://vendor.example/check")
    monkeypatch.setenv("CUBE_TEST_ACCOUNT_TOKEN", "rotated-canary")
    second: Final = resolve_account_credential(POLICY, item, "https://vendor.example/check")
    assert first.headers["X-Vendor-Credential"] == "Bearer first-canary"
    assert second.headers["X-Vendor-Credential"] == "Bearer rotated-canary"
    assert first.binding == second.binding


@pytest.mark.parametrize(
    "target",
    (
        "http://vendor.example/check",
        "http://192.168.1.10/check",
        "https://user:password@vendor.example/check",
        "https://@vendor.example/check",
        "https://vendor.example:invalid/check",
        "https://vendor.example/check?key=x",
    ),
)
def test_account_route_rejects_insecure_or_ambiguous_targets(target: str) -> None:
    with pytest.raises(ValueError):
        validate_account_route(target, True, False, {}, None, POLICY, ("GET",))


def test_route_policy_is_checked_before_crud_can_persist_it() -> None:
    with pytest.raises(ValidationError):
        PassThroughGenericEndpoint(path="/fixture", target="http://remote.example", account_credential=POLICY)
    with pytest.raises(ValidationError):
        PassThroughGenericEndpoint(
            path="/fixture", target="https://vendor.example", auth=False, account_credential=POLICY
        )
    with pytest.raises(ValueError):
        validate_account_route("https://vendor.example", True, True, {}, None, POLICY)
    with pytest.raises(ValueError):
        validate_account_route("https://vendor.example", True, False, {"x-vendor-credential": "shadow"}, None, POLICY)
    with pytest.raises(ValueError):
        validate_account_request(POLICY, {"x-pass-vendor-account": "forged"}, ())
    with pytest.raises(ValueError):
        validate_account_request(POLICY, {}, ("api_key",))


@pytest.mark.parametrize("supplied", (None, {"account_id": "forged-account"}))
def test_spend_account_provenance_rejects_client_scalars_and_json_imitations(supplied: object) -> None:
    stored: Final = stored_metadata(
        {
            "provider_account_provider": "forged-provider",
            "provider_origin": "https://forged.example",
            "provider_account_id": "forged-account",
            "provider_account_source": "configured_credential",
            "_cube_account_binding": supplied,
        }
    )
    assert "provider_account_id" in stored
    assert "provider_account_source" in stored
    assert "provider_account_provider" in stored
    assert stored["provider_account_provider"] is None
    assert "provider_origin" in stored
    assert stored["provider_origin"] is None
    assert stored["provider_account_id"] is None
    assert stored["provider_account_source"] is None


@pytest.mark.parametrize(
    "origin", ("https://other.example", "https://vendor.example:444", "https://vendor.example/path")
)
def test_credential_cannot_be_sent_to_an_unapproved_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        resolve_account_credential(POLICY, credential(origin=origin), "https://vendor.example/check")


def test_credential_requires_an_explicit_origin_allowlist() -> None:
    item: Final = CredentialItem(
        credential_name=POLICY.credential_name,
        credential_values={"access value": "inert-provider-canary"},
        credential_info={"cube_account_id": POLICY.account_id},
    )
    with pytest.raises(ValueError):
        resolve_account_credential(POLICY, item, "https://vendor.example/check")


def test_origin_normalization_preserves_authority_and_ignores_default_ports() -> None:
    assert credential_origin("https://VENDOR.example:443/check") == "https://vendor.example"
    assert credential_origin("https://vendor.example:444/check") == "https://vendor.example:444"
    assert credential_origin("http://[::1]:8080/check") == "http://[::1]:8080"
    assert (
        resolve_account_credential(
            POLICY, credential(origin="https://vendor.example:443"), "https://vendor.example/check"
        ).binding.origin
        == "https://vendor.example"
    )


@pytest.mark.parametrize("name", ("Host", "Content-Length", "Transfer-Encoding", "Connection", "Proxy-Authorization"))
def test_static_headers_cannot_override_the_bound_origin_or_http_framing(name: str) -> None:
    with pytest.raises(ValueError):
        validate_account_route("https://vendor.example/check", True, False, {name: "override"}, None, POLICY, ("GET",))


@pytest.mark.parametrize("name", ("Content-Type", "traceparent", "tracestate", "baggage", "Accept-Encoding"))
def test_credential_headers_cannot_use_protocol_fields_mutated_by_the_gateway(name: str) -> None:
    with pytest.raises(ValidationError):
        CredentialHeader(name=name, field="access value")


def test_provider_identity_comes_from_the_request_credential_snapshot() -> None:
    item: Final = credential(provider="arbitrary-provider")
    resolved: Final = resolve_account_credential(POLICY, item, "https://vendor.example/check")
    stored_info(item)["cube_provider_id"] = "later-provider"
    assert resolved.binding.provider == "arbitrary-provider"
    metadata: Final = stored_metadata(
        {"provider_account_provider": "forged-provider"}, account_binding=resolved.binding
    )
    assert "provider_account_provider" in metadata
    assert metadata["provider_account_provider"] == "arbitrary-provider"


@pytest.mark.parametrize("provider", ("", "Bad Provider", "UPPER"))
def test_credential_provider_identity_must_be_a_valid_provider_id(provider: str) -> None:
    item: Final = credential(provider=provider)
    with pytest.raises(ValueError):
        resolve_account_credential(POLICY, item, "https://vendor.example/check")


RESOURCE_TYPES: Final = (
    {"name": "report-job", "item_paths": ["/reports/jobs/{resource_id}", "/reports/jobs/{resource_id}/cancel"]},
)


def resource_grant(path: str) -> dict[str, object]:
    return {
        "application_id": "fixture-app",
        "key_hash": "a" * 64,
        "account_id": "fixture-account",
        "path": path,
        "methods": ["POST"],
    }


def resource_policy(*paths: str) -> AccountHeaderCredential:
    return AccountHeaderCredential.model_validate(
        POLICY.model_dump()
        | {"resource_types": RESOURCE_TYPES, "application_grants": [resource_grant(path) for path in paths]}
    )


def test_resource_grants_must_name_a_declared_item_path() -> None:
    assert resource_policy("/reports/jobs/{resource_id}/cancel").application_grants[0].path == (
        "/reports/jobs/{resource_id}/cancel"
    )
    with pytest.raises(ValidationError):
        resource_policy("/reports/files/{resource_id}")


@pytest.mark.parametrize(
    "path",
    (
        "/reports/jobs/abc",
        "/reports/jobs/abc/cancel",
        "/reports/jobs/a+b",
        "/reports/jobs/a!b",
        "/reports/jobs/a$b/cancel",
    ),
)
def test_concrete_grants_cannot_bypass_resource_ownership_by_naming_an_item(path: str) -> None:
    with pytest.raises(ValidationError):
        resource_policy(path)


@pytest.mark.parametrize("path", ("/reports/{id}", "/reports/x{resource_id}", "/reports/{resource_id}/{resource_id}"))
def test_resource_grant_paths_need_one_whole_placeholder_segment(path: str) -> None:
    with pytest.raises(ValidationError):
        AccountApplicationGrant.model_validate(resource_grant(path))


@pytest.mark.parametrize("path", ("/reports/jobs/{resource_id}/cancel", "/reports/jobs/abc/cancel"))
def test_resource_grants_never_authorize_through_the_literal_path_check(path: str) -> None:
    policy: Final = resource_policy("/reports/jobs/{resource_id}/cancel")
    assert not account_application_allowed(policy, "fixture-app", "a" * 64, path, "POST")


def test_resource_routes_need_subpath_forwarding_before_they_can_be_persisted() -> None:
    policy: Final = resource_policy()
    with pytest.raises(ValidationError):
        PassThroughGenericEndpoint(path="/reports", target="https://vendor.example", account_credential=policy)
    accepted: Final = PassThroughGenericEndpoint(
        path="/reports",
        target="https://vendor.example",
        include_subpath=True,
        methods=["POST"],
        account_credential=policy,
    )
    assert accepted.account_credential == policy


@pytest.mark.parametrize("path", ("/reports/jobs", "/reports/jobs/abc/cancelled", "/reports/usage"))
def test_concrete_grants_outside_item_shapes_keep_their_exact_behavior(path: str) -> None:
    policy: Final = AccountHeaderCredential.model_validate(
        POLICY.model_dump() | {"resource_types": RESOURCE_TYPES, "application_grants": [resource_grant(path)]}
    )
    assert account_application_allowed(policy, "fixture-app", "a" * 64, path, "POST")
    assert not account_application_allowed(policy, "fixture-app", "a" * 64, path + "/child", "POST")


QUERY_SECRET: Final = "fixture +/ credential&ticket=forged%"
QUERY_POLICY: Final = AccountHeaderCredential(
    account_id="fixture-account",
    credential_name="fixture credential",
    query=CredentialQuery(name="ticket.v2~", field="access value", prefix="k:"),
    allowed_query_parameters=("page", "tag"),
)


def test_query_credentials_need_a_destination_that_clients_cannot_name() -> None:
    with pytest.raises(ValidationError, match="header, query parameter or SigV4 destination"):
        AccountHeaderCredential(account_id="fixture-account", credential_name="fixture credential")
    with pytest.raises(ValidationError, match="also be a client query parameter"):
        AccountHeaderCredential.model_validate(QUERY_POLICY.model_dump() | {"allowed_query_parameters": ["TICKET.V2~"]})
    for name in ("ticket.v2~", "TICKET.v2~"):
        with pytest.raises(ValueError, match="Clients cannot supply the account credential query parameter"):
            validate_account_request(QUERY_POLICY, {}, ("page", name))
    validate_account_request(QUERY_POLICY, {}, ("page", "tag"))


@pytest.mark.parametrize("name", ("api key", "a&b", "a=b", "", "x" * 129))
def test_query_credential_names_are_plain_unreserved_tokens(name: str) -> None:
    with pytest.raises(ValidationError):
        CredentialQuery(name=name, field="access value")


def test_query_credential_resolution_keeps_the_value_private_and_headers_empty() -> None:
    resolved: Final = resolve_account_credential(QUERY_POLICY, credential(QUERY_SECRET), "https://vendor.example/v1")
    assert resolved.headers == {} and isinstance(resolved.outbound, QueryCredentialAuth)
    assert QUERY_SECRET not in repr(resolved) and QUERY_SECRET not in repr(resolved.outbound)
    assert resolved.outbound.redact(f"k:{QUERY_SECRET} and {QUERY_SECRET}") == "[redacted] and [redacted]"


SIGV4: Final = CredentialSigV4(
    region="eu-central-1",
    service="execute-api",
    access_key_field="access key",
    secret_key_field="secret key",
    session_token_field="session token",
)
SIGV4_POLICY: Final = AccountHeaderCredential(
    account_id="fixture-account", credential_name="fixture credential", sigv4=SIGV4, allowed_query_parameters=("page",)
)


def sigv4_credential(**values: str) -> CredentialItem:
    return CredentialItem(
        credential_name=POLICY.credential_name,
        credential_values=values,
        credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": ["https://vendor.example"]},
    )


def test_sigv4_is_the_only_credential_destination_of_its_route() -> None:
    for changes in (
        {"headers": [{"name": "X-Api-Key", "field": "access key"}]},
        {"query": {"name": "key", "field": "access key"}},
    ):
        with pytest.raises(ValidationError, match="cannot be combined"):
            AccountHeaderCredential.model_validate(SIGV4_POLICY.model_dump() | changes)
    for region, service in (
        ("EU-central-1", "execute-api"),
        ("eu-central", "execute-api"),
        ("eu-central-1", "Execute API"),
    ):
        with pytest.raises(ValidationError):
            CredentialSigV4.model_validate(SIGV4.model_dump() | {"region": region, "service": service})


@pytest.mark.parametrize("name", ("Authorization", "X-Amz-Date", "x-amz-security-token", "X-Amz-Content-Sha256"))
def test_inline_headers_cannot_shadow_the_signature(name: str) -> None:
    with pytest.raises(ValueError, match="SigV4 signature"):
        validate_account_route("https://vendor.example/v1", True, False, {name: "forged"}, None, SIGV4_POLICY, ("GET",))


def test_sigv4_credentials_resolve_from_the_named_vault_entry_without_exposing_them() -> None:
    resolved: Final = resolve_account_credential(
        SIGV4_POLICY,
        sigv4_credential(
            **{"access key": "AKIDFIXTURE", "secret key": "fixture/secret+", "session token": "fixture-token"}
        ),
        "https://vendor.example/v1",
    )
    assert resolved.headers == {} and isinstance(resolved.outbound, SigV4CredentialAuth)
    assert repr(resolved.outbound) == "SigV4CredentialAuth(execute-api, eu-central-1, <redacted>)"
    assert (
        resolved.outbound.redact("AKIDFIXTURE fixture%2Fsecret%2B fixture-token") == "[redacted] [redacted] [redacted]"
    )
    with pytest.raises(ValueError, match="unavailable or invalid"):
        resolve_account_credential(
            SIGV4_POLICY, sigv4_credential(**{"access key": "AKIDFIXTURE"}), "https://vendor.example/v1"
        )
