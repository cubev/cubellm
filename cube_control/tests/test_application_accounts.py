import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from cube_control.application_access import (
    ApplicationAccessSpec,
    deliver_application_access,
    prepare_application_access,
    provision_application_access,
    revoke_application_access,
)
from cube_control.application_accounts import AccountGrant, AccountRouteScope

SCOPE: Final = AccountRouteScope(
    route_id="fixture-route",
    account_id="fixture-account",
    expected_fingerprint="a" * 64,
    path="/cube_api/fixture/quota",
    methods=("GET",),
)
SPEC: Final = ApplicationAccessSpec(
    application_id="fixture-account-app",
    team_id="fixture-team",
    gateway_origin="http://127.0.0.1:4000",
    account_routes=(SCOPE,),
)


class AccountGateway:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.gets: list[str] = []
        self.user: dict[str, object] | None = None
        self.key: dict[str, object] | None = None
        self.grants: tuple[AccountGrant, ...] = ()
        self.route_id: str = SCOPE.route_id
        self.capabilities: bool = True
        self.resource_capability: bool = False
        self.fingerprint: str = SCOPE.expected_fingerprint
        self.account_id: str = SCOPE.account_id
        self.methods: tuple[str, ...] = ("GET", "POST")
        self.include_subpath: bool = False
        self.grant_failure: bool = False
        self.timeout_after_commit: bool = False
        self.timeout_before_commit: bool = False
        self.lose_grant_on_read: bool = False
        self.foreign_receipt: bool = False
        self.receipt_without_grant: bool = False
        self.route_exists: bool = True
        self.other_routes: dict[str, dict[str, object]] = {}

    def route(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "account_id": self.account_id,
            "path": SCOPE.path,
            "methods": self.methods,
            "include_subpath": self.include_subpath,
            "origin": "https://vendor.example",
            "fingerprint": self.fingerprint,
            "grants": tuple(grant.model_dump(mode="json") for grant in self.grants),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path: Final = request.url.path
        other_path: Final = path.removesuffix("/grants") if request.method == "POST" else path
        other: Final = self.other_routes.get(other_path)
        if other is not None:
            if request.method == "GET":
                return httpx.Response(200, json=other)
            self.posts.append(path)
            other_body: Final = TypeAdapter(dict[str, object]).validate_json(request.content)
            assert other_body["expected_fingerprint"] == other["fingerprint"]
            other_grant: Final = AccountGrant.model_validate(other_body["grant"])
            existing: Final = TypeAdapter(tuple[AccountGrant, ...]).validate_python(other["grants"])
            self.other_routes[other_path] = other | {
                "grants": [grant.model_dump(mode="json") for grant in (*existing, other_grant)]
            }
            return httpx.Response(
                200, json={"stored": True, "enforcement": "not_verified", "route": self.other_routes[other_path]}
            )
        if request.method == "POST":
            self.posts.append(path)
            body: Final = TypeAdapter(dict[str, object]).validate_json(request.content)
            if path == "/user/new":
                self.user = body
            elif path == "/key/generate":
                self.key = body | {"status": "active"}
            elif path == "/key/block":
                assert self.key is not None
                self.key = self.key | {"blocked": True, "status": "revoked"}
            elif path == SCOPE.management_path + "/grants":
                if self.grant_failure:
                    return httpx.Response(409)
                if self.timeout_before_commit:
                    raise httpx.ReadTimeout("inert timeout before commit", request=request)
                assert body["expected_fingerprint"] == self.fingerprint
                grant: Final = AccountGrant.model_validate(body["grant"])
                assert grant not in self.grants
                self.grants = (*self.grants, grant)
                if self.timeout_after_commit:
                    raise httpx.ReadTimeout("inert timeout after commit", request=request)
                if self.foreign_receipt or self.receipt_without_grant:
                    return httpx.Response(
                        200,
                        json={
                            "stored": True,
                            "enforcement": "not_verified",
                            "route": self.route()
                            | ({"fingerprint": "b" * 64} if self.foreign_receipt else {"grants": []}),
                        },
                    )
                return httpx.Response(200, json={"stored": True, "enforcement": "not_verified", "route": self.route()})
            else:
                raise AssertionError("Unexpected mutation " + path)
            return httpx.Response(200, json={})
        self.gets.append(path)
        if path == "/cube_control/capabilities":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "application_identity": "authenticated_object_v1",
                    "virtual_key_log_redaction": True,
                    "custom_keys_allowed": True,
                    "client_side_credentials_allowed": False,
                    **(
                        {
                            "application_exact_routes": "exact_path_v1",
                            "passthrough_account_grants": "application_account_path_method_v1",
                            "passthrough_account_grant_management": "atomic_append_v1",
                            "passthrough_account_binding": "configured_credential_v1",
                            "passthrough_header_isolation": True,
                            "credential_values_full_masking": True,
                        }
                        if self.capabilities
                        else {}
                    ),
                    **(
                        {"passthrough_account_resources": "application_ownership_v1"}
                        if self.resource_capability
                        else {}
                    ),
                },
            )
        if path == SCOPE.management_path:
            if self.lose_grant_on_read and self.grants:
                self.grants = ()
            return httpx.Response(200, json=self.route()) if self.route_exists else httpx.Response(404)
        if path == "/team/info":
            return httpx.Response(
                200,
                json={
                    "team_info": {
                        "team_id": SPEC.team_id,
                        "models": ["fixture-model"],
                        "members_with_roles": [{"user_id": SPEC.user_id, "role": "user"}] if self.user else [],
                    }
                },
            )
        if path == "/user/info":
            return httpx.Response(200, json={"user_info": self.user}) if self.user else httpx.Response(404)
        if path == "/key/info":
            assert request.url.params["key"] == self.key_hash() if self.key else True
            return httpx.Response(200, json={"info": self.key}) if self.key else httpx.Response(404)
        if path == "/model/info":
            return httpx.Response(200, json={"data": [{"model_name": "fixture-model", "litellm_params": {}}]})
        raise AssertionError("Unexpected read " + path)

    def key_hash(self) -> str:
        assert self.key is not None
        return sha256(str(self.key["key"]).encode()).hexdigest()


class AccountApplicationTests(unittest.TestCase):
    def test_account_only_key_is_scoped_stored_delivered_and_revoked_without_provider_keys(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            destination: Final = Path(root) / "consumer"
            gateway: Final = AccountGateway()
            self.assertEqual(prepare_application_access(SPEC, directory).status, "prepared")
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "provisioned")
            self.assertEqual(result.enforcement, "not_verified")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate", SCOPE.management_path + "/grants"])
            assert gateway.key is not None and gateway.user is not None
            self.assertEqual(gateway.key["allowed_routes"], ["exact:" + SCOPE.path])
            self.assertEqual(gateway.key["models"], [])
            self.assertEqual(
                gateway.key["metadata"],
                {
                    **dict(TypeAdapter(dict[str, str]).validate_python(gateway.user["metadata"])),
                    "allowed_passthrough_routes": [SCOPE.path],
                },
            )
            self.assertEqual(
                gateway.grants,
                (
                    AccountGrant(
                        application_id=SPEC.application_id,
                        key_hash=gateway.key_hash(),
                        account_id=SCOPE.account_id,
                        path=SCOPE.path,
                        methods=SCOPE.methods,
                    ),
                ),
            )
            self.assertNotIn("/model/info", gateway.gets)
            self.assertIn('"account_grants_attempted":true', (directory / "state.json").read_text().replace(" ", ""))
            before: Final = tuple(gateway.posts)
            journal: Final = (directory / "state.json").read_bytes()
            delivered: Final = deliver_application_access(
                directory, destination, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(delivered.status, "delivered")
            self.assertEqual((destination / "api-key").read_text(), str(gateway.key["key"]) + "\n")
            self.assertEqual(tuple(gateway.posts), before)
            self.assertEqual((directory / "state.json").read_bytes(), journal)
            self.assertNotIn(str(gateway.key["key"]), result.model_dump_json() + delivered.model_dump_json())
            revoked: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(revoked.status, "blocked")
            self.assertEqual(revoked.enforcement, "not_verified")
            self.assertEqual(
                deliver_application_access(
                    directory, destination, SecretStr("inert-master"), httpx.MockTransport(gateway)
                ).status,
                "error",
            )

    def test_account_scope_rejects_unbounded_methods_paths_and_empty_application_scope(self) -> None:
        for changes in (
            {"path": "/key/generate"},
            {"path": "/v1/chat/completions"},
            {"path": "/cube_api/"},
            {"path": "/cube_api/../key"},
            {"path": "/cube_api/a%2fb"},
            {"path": "/cube_api/a//b"},
            {"path": "/cube_api/*"},
            {"methods": ()},
            {"methods": ["get"]},
            {"methods": ["GET", "GET"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                AccountRouteScope.model_validate(SCOPE.model_dump() | changes)
        for changes in (
            {"account_routes": ()},
            {"models": ["fixture-model"]},
            {"routes": ["/v1/chat/completions"]},
            {"account_routes": [SCOPE, SCOPE]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ApplicationAccessSpec.model_validate(SPEC.model_dump() | changes)

    def test_resource_scopes_follow_the_native_template_grammar(self) -> None:
        for path in (
            "/cube_api/fixture/quota/{resource_id}/",
            "/cube_api/fixture/quota//{resource_id}",
            "/cube_api/fixture/quota/x{resource_id}",
            "/cube_api/fixture/{resource_id}/{resource_id}",
            "/cube_api/fixture/quota/{id}",
            "/cube_api/fixture/quota/{resource_id}/../cancel",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                AccountRouteScope.model_validate(SCOPE.model_dump() | {"path": path})
        for path in (
            "/cube_api/fixture/quota/{resource_id}",
            "/cube_api/fixture/quota/{resource_id}/cancel",
            "/cube_api/fixture/{resource_id}/quota",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    AccountRouteScope.model_validate(SCOPE.model_dump() | {"path": path}).key_route, "resource:" + path
                )
        self.assertEqual(SCOPE.key_route, "exact:/cube_api/fixture/quota")

    def test_resource_scope_keys_carry_resource_routes_only_on_gateways_with_resource_ownership(self) -> None:
        template: Final = SCOPE.path + "/{resource_id}"
        spec: Final = ApplicationAccessSpec.model_validate(
            SPEC.model_dump() | {"account_routes": [SCOPE.model_dump() | {"path": template}]}
        )
        self.assertEqual(spec.key_routes, ("resource:" + template,))
        self._assert_preflight_rejected(spec, "include_subpath", True, "Application state or gateway response rejected")
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            gateway.include_subpath = True
            gateway.resource_capability = True
            prepare_application_access(spec, directory)
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "provisioned", result.message)
            assert gateway.key is not None
            self.assertEqual(gateway.key["allowed_routes"], ["resource:" + template])
            self.assertEqual(
                gateway.key["metadata"]["allowed_passthrough_routes"],  # pyright: ignore[reportIndexIssue, reportUnknownMemberType]  # fake stores raw JSON
                ["resource:" + template],
            )
            self.assertEqual(tuple(grant.path for grant in gateway.grants), (template,))

    def test_preflight_rejects_drift_missing_routes_and_old_capabilities_before_mutation(self) -> None:
        conflict: Final = "Account route conflicts with the prepared scope"
        for field, value, message in (
            ("fingerprint", "b" * 64, conflict),
            ("account_id", "other-account", conflict),
            ("route_id", "other-route", conflict),
            ("methods", ("POST",), conflict),
            ("route_exists", False, "Configured account route cannot be inspected"),
            ("capabilities", False, "Application state or gateway response rejected"),
        ):
            self._assert_preflight_rejected(SPEC, field, value, message)

    def test_child_scope_requires_a_registered_subpath_boundary(self) -> None:
        conflict: Final = "Account route conflicts with the prepared scope"
        child: Final = SPEC.model_copy(
            update={"account_routes": (SCOPE.model_copy(update={"path": SCOPE.path + "/child"}),)}
        )
        sibling: Final = SPEC.model_copy(
            update={"account_routes": (SCOPE.model_copy(update={"path": SCOPE.path + "-extra"}),)}
        )
        self._assert_preflight_rejected(child, "include_subpath", False, conflict)
        self._assert_preflight_rejected(sibling, "include_subpath", True, conflict)

    def _assert_preflight_rejected(self, spec: ApplicationAccessSpec, field: str, value: object, message: str) -> None:
        with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            setattr(gateway, field, value)
            prepare_application_access(spec, directory)
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual((result.status, result.message), ("error", message))
            self.assertEqual(gateway.posts, [])

    def test_committed_timeout_reconciles_without_repeating_grant_or_key_creation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            gateway.timeout_after_commit = True
            prepare_application_access(SPEC, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "uncertain",
            )
            before: Final = tuple(gateway.posts)
            gateway.timeout_after_commit = False
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            self.assertEqual(tuple(gateway.posts), before)

    def test_uncommitted_timeout_retries_only_the_idempotent_grant(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            gateway.timeout_before_commit = True
            prepare_application_access(SPEC, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "uncertain",
            )
            gateway.timeout_before_commit = False
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            self.assertEqual(
                gateway.posts,
                ["/user/new", "/key/generate", SCOPE.management_path + "/grants", SCOPE.management_path + "/grants"],
            )

    def test_delivery_never_creates_missing_grants_and_rejects_scope_expansion(self) -> None:
        for change in ("missing", "expanded", "fingerprint", "key_metadata", "passthrough_routes"):
            self._assert_delivery_drift(change)

    def _assert_delivery_drift(self, change: str) -> None:
        with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            destination: Final = Path(root) / "consumer"
            gateway: Final = AccountGateway()
            prepare_application_access(SPEC, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            if change == "missing":
                gateway.grants = ()
            elif change == "expanded":
                gateway.grants = (gateway.grants[0].model_copy(update={"methods": ("GET", "POST")}),)
            elif change == "fingerprint":
                gateway.fingerprint = "b" * 64
            elif change == "key_metadata":
                assert gateway.key is not None
                gateway.key["metadata"] = {}
            else:
                assert gateway.key is not None
                gateway.key["allowed_passthrough_routes"] = ["/cube_api/other"]
            before: Final = tuple(gateway.posts)
            self.assertEqual(
                deliver_application_access(
                    directory, destination, SecretStr("inert-master"), httpx.MockTransport(gateway)
                ).status,
                "error",
            )
            self.assertFalse(destination.exists())
            self.assertEqual(tuple(gateway.posts), before)

    def test_failed_or_inconsistent_grant_storage_never_reports_provisioned(self) -> None:
        for field, status, message in (
            ("grant_failure", "uncertain", "Account grant storage requires reconciliation"),
            ("foreign_receipt", "uncertain", "Account grant receipt conflicts with the prepared scope"),
            ("receipt_without_grant", "uncertain", "Account grant receipt conflicts with the prepared scope"),
            ("lose_grant_on_read", "error", "Prepared account grant is missing; no credential delivered"),
        ):
            self._assert_storage_failure(field, status, message)

    def _assert_storage_failure(self, field: str, status: str, message: str) -> None:
        with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            setattr(gateway, field, True)
            prepare_application_access(SPEC, directory)
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual((result.status, result.message), (status, message))

    def test_grants_of_other_keys_on_the_same_route_are_preserved_and_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            neighbour: Final = AccountGrant(
                application_id="neighbour-app",
                key_hash="c" * 64,
                account_id=SCOPE.account_id,
                path=SCOPE.path,
                methods=("POST",),
            )
            gateway.grants = (neighbour,)
            prepare_application_access(SPEC, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            self.assertEqual(gateway.grants[0], neighbour)
            self.assertEqual(
                tuple(grant.application_id for grant in gateway.grants), ("neighbour-app", SPEC.application_id)
            )

    def test_revocation_refuses_a_key_whose_account_metadata_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            prepare_application_access(SPEC, directory)
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            assert gateway.key is not None
            gateway.key["metadata"] = {
                **TypeAdapter(dict[str, object]).validate_python(gateway.key["metadata"]),
                "allowed_passthrough_routes": ["/cube_api/other"],
            }
            before: Final = tuple(gateway.posts)
            self.assertEqual(
                revoke_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "error",
            )
            self.assertEqual(tuple(gateway.posts), before)

    def test_mixed_inference_and_account_key_keeps_both_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            mixed: Final = ApplicationAccessSpec.model_validate(
                SPEC.model_dump() | {"models": ["fixture-model"], "routes": ["/v1/chat/completions"]}
            )
            prepare_application_access(mixed, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            assert gateway.key is not None
            self.assertEqual(gateway.key["allowed_routes"], ["/v1/chat/completions", "exact:" + SCOPE.path])
            self.assertEqual(gateway.key["models"], ["fixture-model"])

    def test_two_concrete_children_share_registered_route_without_granting_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            destination: Final = Path(root) / "consumer"
            gateway: Final = AccountGateway()
            gateway.include_subpath = True
            first: Final = SCOPE.model_copy(update={"path": SCOPE.path + "/first"})
            second: Final = SCOPE.model_copy(update={"path": SCOPE.path + "/second", "methods": ("POST",)})
            spec: Final = SPEC.model_copy(update={"account_routes": (first, second)})
            prepare_application_access(spec, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            assert gateway.key is not None
            self.assertEqual(gateway.key["allowed_routes"], ["exact:" + first.path, "exact:" + second.path])
            self.assertEqual(
                tuple((grant.path, grant.methods) for grant in gateway.grants),
                ((first.path, first.methods), (second.path, second.methods)),
            )
            before: Final = tuple(gateway.posts)
            self.assertEqual(
                deliver_application_access(
                    directory, destination, SecretStr("inert-master"), httpx.MockTransport(gateway)
                ).status,
                "delivered",
            )
            self.assertEqual(tuple(gateway.posts), before)

    def test_existing_key_grant_with_foreign_identity_or_path_stops_reconciliation(self) -> None:
        for changes in (
            {"application_id": "another-app"},
            {"path": SCOPE.path + "/other"},
            {"account_id": "another-account"},
        ):
            self._assert_foreign_grant_rejected(changes)

    def _assert_foreign_grant_rejected(self, changes: dict[str, str]) -> None:
        with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            prepare_application_access(SPEC, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            gateway.grants = (gateway.grants[0].model_copy(update=changes),)
            before: Final = tuple(gateway.posts)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "error",
            )
            self.assertEqual(tuple(gateway.posts), before)

    def test_oversized_scope_does_not_leave_an_unreadable_private_journal(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            methods: Final = tuple("X" + str(index).zfill(31) for index in range(16))
            scopes: Final = tuple(
                SCOPE.model_copy(update={"path": "/cube_api/" + str(index) + "x" * 1000, "methods": methods})
                for index in range(32)
            )
            models: Final = tuple(str(index).zfill(3) + "m" * 125 for index in range(100))
            spec: Final = ApplicationAccessSpec(
                application_id=SPEC.application_id,
                team_id=SPEC.team_id,
                gateway_origin=SPEC.gateway_origin,
                models=models,
                routes=("/v1/chat/completions",),
                account_routes=scopes,
            )
            self.assertEqual(prepare_application_access(spec, directory).status, "error")
            self.assertFalse((directory / "state.json").exists())

    def test_grant_for_one_route_stored_on_another_route_stops_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            gateway: Final = AccountGateway()
            other: Final = SCOPE.model_copy(
                update={"route_id": "other-route", "expected_fingerprint": "b" * 64, "path": "/cube_api/other/quota"}
            )
            gateway.other_routes[other.management_path] = gateway.route() | {
                "route_id": other.route_id,
                "path": other.path,
                "fingerprint": other.expected_fingerprint,
            }
            spec: Final = SPEC.model_copy(update={"account_routes": (SCOPE, other)})
            prepare_application_access(spec, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            misplaced: Final = AccountGrant(
                application_id=spec.application_id,
                key_hash=gateway.key_hash(),
                account_id=other.account_id,
                path=other.path,
                methods=other.methods,
            )
            gateway.grants = (*gateway.grants, misplaced)
            before: Final = tuple(gateway.posts)
            self.assertEqual(
                provision_application_access(
                    directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
                ).message,
                "Stored account grants exceed the prepared key scope",
            )
            self.assertEqual(tuple(gateway.posts), before)

    def test_one_key_can_use_two_accounts_with_independent_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            destination: Final = Path(root) / "consumer"
            gateway: Final = AccountGateway()
            other: Final = AccountRouteScope(
                route_id="other-route",
                account_id="other-account",
                expected_fingerprint="b" * 64,
                path="/cube_api/other/quota",
                methods=("POST",),
            )
            gateway.other_routes[other.management_path] = gateway.route() | {
                "route_id": other.route_id,
                "account_id": other.account_id,
                "path": other.path,
                "fingerprint": other.expected_fingerprint,
            }
            spec: Final = SPEC.model_copy(update={"account_routes": (SCOPE, other)})
            prepare_application_access(spec, directory)
            self.assertEqual(
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway)).status,
                "provisioned",
            )
            assert gateway.key is not None
            self.assertEqual(gateway.key["allowed_routes"], ["exact:" + SCOPE.path, "exact:" + other.path])
            other_grants: Final = TypeAdapter(tuple[AccountGrant, ...]).validate_python(
                gateway.other_routes[other.management_path]["grants"]
            )
            self.assertEqual(
                other_grants,
                (
                    AccountGrant(
                        application_id=spec.application_id,
                        key_hash=gateway.key_hash(),
                        account_id=other.account_id,
                        path=other.path,
                        methods=other.methods,
                    ),
                ),
            )
            self.assertEqual(tuple(grant.account_id for grant in gateway.grants), (SCOPE.account_id,))
            before: Final = tuple(gateway.posts)
            self.assertEqual(
                deliver_application_access(
                    directory, destination, SecretStr("inert-master"), httpx.MockTransport(gateway)
                ).status,
                "delivered",
            )
            self.assertEqual(tuple(gateway.posts), before)
