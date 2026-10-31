from types import MappingProxyType
from typing import Final, cast

import pytest
from pydantic import SecretStr, ValidationError

from litellm.models.credentials import CredentialItem
from litellm.proxy._types import PassThroughGenericEndpoint
from litellm.proxy.pass_through_endpoints.account_credentials import (
    AccountHeaderCredential,
    CredentialHeader,
    credential_origin,
    resolve_account_credential,
    validate_account_request,
    validate_account_route,
)
from litellm.proxy.spend_tracking.spend_tracking_utils import (
    _get_spend_logs_metadata as stored_metadata,  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
)

POLICY: Final = AccountHeaderCredential(
    account_id="fixture-account",
    credential_name="fixture credential",
    headers=(CredentialHeader(name="X-Vendor-Credential", field="access value", prefix="Bearer "),),
    allowed_query_parameters=("page",),
)


def credential(
    value: str | SecretStr = "inert-provider-canary",
    account_id: str = "fixture-account",
    origin: str = "https://vendor.example",
) -> CredentialItem:
    return CredentialItem(
        credential_name=POLICY.credential_name,
        credential_values={"access value": value},
        credential_info={"cube_account_id": account_id, "cube_allowed_origins": [origin]},
    )


def test_native_credential_resolution_is_private_and_has_an_immutable_snapshot() -> None:
    item: Final = credential(SecretStr("inert-provider-canary"))
    resolved: Final = resolve_account_credential(POLICY, item, "https://vendor.example/check")
    assert resolved.headers["X-Vendor-Credential"] == "Bearer inert-provider-canary"
    assert "inert-provider-canary" not in repr(resolved)
    cast(dict[str, object], item.credential_info)["cube_account_id"] = "later-account"
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
            "provider_origin": "https://forged.example",
            "provider_account_id": "forged-account",
            "provider_account_source": "configured_credential",
            "_cube_account_binding": supplied,
        }
    )
    assert "provider_account_id" in stored
    assert "provider_account_source" in stored
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
