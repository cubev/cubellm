import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from inventory_fixtures import bound, collected, gateway_inventory, snapshot

from cube_control.gateway_inventory import (
    GatewayInventoryAttempt,
    GatewayInventoryState,
    InventoryCredential,
    InventoryDeployment,
    InventoryRoute,
    Origin,
)
from cube_control.inventory import STALE_AFTER, AccountInventoryReport, account_inventory
from cube_control.models import Account, Observation, Provider, UsageEvent

NOW: Final = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
A: Final = "http://127.0.0.1:4000"
B: Final = "http://127.0.0.1:4001"
ACCOUNT: Final = Account(id="openai-main", provider=Provider.OPENAI, name="Main", kind="api")


def report(
    gateways: tuple[GatewayInventoryState, ...],
    accounts: tuple[Account, ...] = (ACCOUNT,),
    observations: tuple[Observation, ...] = (),
    events: tuple[UsageEvent, ...] = (),
) -> AccountInventoryReport:
    return account_inventory(accounts, observations, events, (), gateways, NOW)


def gaps(result: AccountInventoryReport, account_id: str = ACCOUNT.id) -> tuple[str, ...]:
    return next(entry.gaps for entry in result.accounts if entry.account_id == account_id)


def observation(metric: str, source: str, status: str) -> Observation:
    return Observation.model_validate(
        {
            "account_id": ACCOUNT.id,
            "external_id": f"{metric}-{source}",
            "metric": metric,
            "window": "month",
            "period": "2026-11",
            "status": status,
            "source": source,
            "observed_at": NOW,
            "collected_at": NOW,
            "value": Decimal(1),
            "unit": "USD",
        }
    )


def referenced(
    gateway: str, origin: Origin | Literal["both"], name: str, account_id: str = ACCOUNT.id
) -> GatewayInventoryState:
    return collected(
        gateway,
        NOW,
        gateway_inventory(
            NOW,
            credentials=(bound("database", name, account_id), bound("runtime", name, account_id))
            if origin == "both"
            else (bound("database" if origin == "database" else "runtime", name, account_id),),
            deployments=tuple(
                InventoryDeployment(origin=item, model_name="m", credential_name=name, auth="credential_reference")
                for item in (("database", "runtime") if origin == "both" else (origin,))
            ),
        ),
    )


class AccountInventoryTests(unittest.TestCase):
    def test_without_any_collected_gateway_nothing_claims_missing_credentials(self) -> None:
        result: Final = report(())
        self.assertEqual(result.coverage, "not_collected")
        self.assertIn("inventory_not_collected", gaps(result))
        self.assertNotIn("no_gateway_credential", gaps(result))

    def test_each_gateway_keeps_its_own_records_and_presence_is_compared_per_gateway(self) -> None:
        result: Final = report((referenced(A, "database", "shared"), referenced(B, "runtime", "shared")))
        entry: Final = result.accounts[0]
        self.assertEqual(
            sorted((item.gateway_origin, item.credential.origin) for item in entry.credentials),
            [(A, "database"), (B, "runtime")],
        )
        self.assertTrue({"credential_only_in_database", "credential_only_in_runtime"} <= set(entry.gaps))
        self.assertEqual(result.coverage, "configured_sources_complete")

    def test_a_credential_present_in_both_layers_of_one_gateway_has_no_presence_gap(self) -> None:
        result: Final = report((referenced(A, "both", "shared"),))
        self.assertFalse(
            {"credential_only_in_database", "credential_only_in_runtime", "credential_unused"} & set(gaps(result))
        )

    def test_a_bound_credential_nothing_references_on_its_gateway_is_unused(self) -> None:
        unused: Final = collected(A, NOW, gateway_inventory(NOW, credentials=(bound("runtime", "idle", ACCOUNT.id),)))
        other_gateway_reference: Final = collected(
            B,
            NOW,
            gateway_inventory(
                NOW,
                deployments=(
                    InventoryDeployment(origin="runtime", model_name="m", credential_name="idle", auth="unknown"),
                ),
            ),
        )
        self.assertIn("credential_unused", gaps(report((unused, other_gateway_reference))))

    def test_gateway_state_distinguishes_current_stale_failed_and_never_collected(self) -> None:
        inventory: Final = gateway_inventory(NOW)
        failed: Final = GatewayInventoryState(
            gateway_origin=B,
            attempt=GatewayInventoryAttempt(attempted_at=NOW, status="error", message="Gateway response rejected"),
            last_good=snapshot(B, NOW - timedelta(hours=1), gateway_inventory(NOW - timedelta(hours=1))),
        )
        never: Final = GatewayInventoryState(
            gateway_origin="http://127.0.0.1:4002",
            attempt=GatewayInventoryAttempt(attempted_at=NOW, status="uncertain", message="Gateway request failed"),
        )
        old: Final = NOW - STALE_AFTER - timedelta(seconds=1)
        stale: Final = collected("http://127.0.0.1:4003", old, gateway_inventory(old))
        result: Final = report((collected(A, NOW, inventory), failed, never, stale))
        self.assertEqual(
            [(item.gateway_origin, item.state, item.last_good_observed_at) for item in result.gateways],
            [
                (A, "current", NOW),
                (B, "last_attempt_failed", NOW - timedelta(hours=1)),
                ("http://127.0.0.1:4002", "never_collected", None),
                ("http://127.0.0.1:4003", "stale", old),
            ],
        )
        self.assertEqual(result.coverage, "partial")

    def test_an_unavailable_source_keeps_coverage_partial_and_is_reported(self) -> None:
        inventory: Final = gateway_inventory(NOW, unavailable=frozenset({("routes", "database")}))
        result: Final = report((collected(A, NOW, inventory),))
        self.assertEqual(result.coverage, "partial")
        self.assertIn(
            ("source_unavailable", A, "database", "routes"),
            [(item.kind, item.gateway_origin, item.origin, item.source) for item in result.findings],
        )

    def test_only_usage_bearing_observations_count_as_usage(self) -> None:
        quota_only: Final = report((), observations=(observation("quota_remaining", "provider_api", "known"),))
        manual_usage: Final = report((), observations=(observation("usage", "manual", "manual"),))
        provider_spend: Final = report((), observations=(observation("spend", "provider_api", "known"),))
        self.assertEqual(
            [
                ("no_usage_source" in gaps(item), "no_verified_provider_usage" in gaps(item))
                for item in (quota_only, manual_usage, provider_spend)
            ],
            [(True, True), (False, True), (False, False)],
        )

    def test_unbound_invalid_and_unattributed_records_stay_visible_per_gateway(self) -> None:
        inventory: Final = gateway_inventory(
            NOW,
            credentials=(
                InventoryCredential(origin="runtime", credential_name="plain", binding="unbound"),
                InventoryCredential(
                    origin="runtime", credential_name="broken", account_id=ACCOUNT.id, binding="invalid"
                ),
            ),
            routes=(InventoryRoute(origin="database", route_id="legacy", path="/legacy", binding="unbound"),),
            deployments=(InventoryDeployment(origin="runtime", model_name="loose", auth="inline_api_key"),),
        )
        result: Final = report((collected(A, NOW, inventory),))
        self.assertEqual(
            sorted((item.kind, item.gateway_origin, item.name, item.account_id) for item in result.findings),
            [
                ("invalid_credential", A, "broken", ACCOUNT.id),
                ("unattributed_deployment", A, "loose", None),
                ("unbound_credential", A, "plain", None),
                ("unbound_route", A, "legacy", None),
            ],
        )
        self.assertIn("invalid_binding", gaps(result))

    def test_one_credential_bound_to_two_accounts_on_one_gateway_is_a_conflict(self) -> None:
        conflicting: Final = collected(
            A,
            NOW,
            gateway_inventory(NOW, credentials=(bound("database", "c", ACCOUNT.id), bound("runtime", "c", "other"))),
        )
        separate_gateways: Final = (referenced(A, "both", "c"), referenced(B, "both", "c", account_id="other"))
        self.assertEqual(
            [
                (item.kind, item.gateway_origin, item.name)
                for item in report((conflicting,)).findings
                if item.kind == "binding_conflict"
            ],
            [("binding_conflict", A, "c")],
        )
        self.assertNotIn("binding_conflict", [item.kind for item in report(separate_gateways).findings])

    def test_accounts_known_only_from_a_gateway_are_listed_as_not_catalogued(self) -> None:
        result: Final = report((referenced(A, "both", "c", account_id="unknown-account"),))
        self.assertIn("not_in_catalogue", gaps(result, "unknown-account"))
        self.assertIn("no_gateway_credential", gaps(result))
