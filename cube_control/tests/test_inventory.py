import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from inventory_fixtures import bound, collected, gateway_inventory, snapshot

from cube_control.gateway_inventory import (
    GatewayInventoryAttempt,
    InventoryCredential,
    InventoryDeployment,
    InventoryRoute,
    Origin,
)
from cube_control.inventory_evidence import GatewayInventoryState, merge_evidence
from cube_control.inventory import STALE_AFTER, AccountInventoryEntry, AccountInventoryReport, account_inventory
from cube_control.models import (
    Account,
    ManualChange,
    ManualCommand,
    ManualEntry,
    MediaJob,
    Observation,
    Provider,
)
from cube_control.store import StoredState

NOW: Final = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
A: Final = "http://127.0.0.1:4000"
B: Final = "http://127.0.0.1:4001"
ACCOUNT: Final = Account(id="openai-main", provider=Provider.OPENAI, name="Main", kind="api")


def report(
    gateways: tuple[GatewayInventoryState, ...],
    accounts: tuple[Account, ...] = (ACCOUNT,),
    observations: tuple[Observation, ...] = (),
    jobs: tuple[MediaJob, ...] = (),
    manual_entries: tuple[ManualEntry, ...] = (),
) -> AccountInventoryReport:
    return account_inventory(
        StoredState(
            accounts=accounts,
            observations=observations,
            events=(),
            jobs=jobs,
            manual_entries=manual_entries,
            gateway_inventories=gateways,
        ),
        NOW,
    )


def entry(result: AccountInventoryReport, account_id: str = ACCOUNT.id) -> AccountInventoryEntry:
    return next(item for item in result.accounts if item.account_id == account_id)


def gaps(result: AccountInventoryReport, account_id: str = ACCOUNT.id) -> tuple[str, ...]:
    return next(entry.gaps for entry in result.accounts if entry.account_id == account_id)


def observation(
    metric: str,
    source: str,
    status: str,
    *,
    external_id: str = "",
    window: str = "month",
    period: str = "2026-11",
    at: datetime = NOW,
    scope: str | None = None,
    expires: datetime | None = None,
) -> Observation:
    measured: Final = status in ("known", "manual", "estimated")
    return Observation.model_validate(
        {
            "account_id": ACCOUNT.id,
            "external_id": external_id or f"{metric}-{source}-{status}",
            "metric": metric,
            "window": window,
            "period": period,
            "status": status,
            "source": source,
            "snapshot_scope": scope,
            "expires_at": expires,
            "observed_at": at,
            "collected_at": at,
            "value": Decimal(1) if measured else None,
            "unit": "USD" if measured else None,
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


def _provider_usage_flags(found: AccountInventoryEntry) -> tuple[bool, bool, tuple[bool, ...]]:
    return (
        "provider_usage_not_current" in found.gaps,
        "no_verified_provider_usage" in found.gaps,
        tuple(
            view.current
            for view in found.usage_evidence
            if view.reading.source == "provider_api" and view.reading.status == "known"
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
            evidence=merge_evidence(
                None, snapshot(B, NOW - timedelta(hours=1), gateway_inventory(NOW - timedelta(hours=1)))
            ),
        )
        never: Final = GatewayInventoryState(
            gateway_origin="http://127.0.0.1:4002",
            attempt=GatewayInventoryAttempt(attempted_at=NOW, status="uncertain", message="Gateway request failed"),
        )
        old: Final = NOW - STALE_AFTER - timedelta(seconds=1)
        stale: Final = collected("http://127.0.0.1:4003", old, gateway_inventory(old))
        result: Final = report((collected(A, NOW, inventory), failed, never, stale))
        self.assertEqual(
            [(item.gateway_origin, item.state, item.last_collected_observed_at) for item in result.gateways],
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

    def test_provider_usage_is_current_only_while_it_is_the_latest_fresh_reading_of_this_period(self) -> None:
        current: Final = observation("spend", "provider_api", "known")
        earlier: Final = NOW - timedelta(seconds=30)
        histories: Final = {
            "previous_period": (observation("spend", "provider_api", "known", period="2026-10"),),
            "stale": (observation("spend", "provider_api", "known", at=NOW - timedelta(hours=2)),),
            "replaced_unscoped_by_newer_estimate": (
                observation("spend", "provider_api", "known", at=earlier),
                observation("spend", "provider_api", "estimated"),
            ),
            "superseded_scoped_snapshot": (
                observation("spend", "provider_api", "known", external_id="old", at=earlier, scope="billing"),
                observation("budget", "provider_api", "known", external_id="new", scope="billing"),
            ),
            "expired": (observation("spend", "provider_api", "known", expires=NOW),),
            "clock_skew": (observation("spend", "provider_api", "known", at=NOW + timedelta(minutes=5)),),
            "provider_wide_error_in_all_window": (
                observation("spend", "provider_api", "known", at=earlier),
                observation("spend", "provider_api", "error", window="all", period="2026-11"),
            ),
            "later_error_same_window": (
                observation("spend", "provider_api", "known", at=earlier),
                observation("spend", "provider_api", "error"),
            ),
            "later_unknown_same_window": (
                observation("spend", "provider_api", "known", at=earlier),
                observation("spend", "provider_api", "unknown"),
            ),
        }
        self.assertEqual(
            {
                name: _provider_usage_flags(entry(report((), observations=readings)))
                for name, readings in histories.items()
            },
            dict.fromkeys(histories, (True, False, (False,))),
        )
        fresh: Final = entry(report((), observations=(current,)))
        self.assertEqual([view.current for view in fresh.usage_evidence], [True])
        self.assertFalse({"provider_usage_not_current", "no_verified_provider_usage"} & set(fresh.gaps))

    def test_newer_readings_from_another_source_leave_provider_usage_current(self) -> None:
        provider: Final = observation("spend", "provider_api", "known", at=NOW - timedelta(seconds=30))
        imported: Final = observation("spend", "import", "known")
        self.assertEqual(
            _provider_usage_flags(entry(report((), observations=(provider, imported)))), (False, False, (True,))
        )

    def test_replaced_readings_stay_in_the_history_with_their_identity(self) -> None:
        old: Final = observation("spend", "provider_api", "known", external_id="old", at=NOW - timedelta(seconds=30))
        new: Final = observation("spend", "provider_api", "known", external_id="new")
        found: Final = entry(report((), observations=(new, old)))
        self.assertEqual(
            [(view.reading.external_id, view.current) for view in found.usage_evidence], [("old", False), ("new", True)]
        )

    def test_accounts_referenced_only_by_jobs_or_manual_entries_are_listed_with_their_source(self) -> None:
        job: Final = MediaJob(
            account_id="job-only",
            job_id="job-1",
            state="reserved",
            updated_at=NOW,
            reserved_amount=Decimal(1),
            unit="USD",
            expires_at=NOW + timedelta(hours=1),
        )
        manual: Final = ManualEntry(
            command=ManualCommand(
                command_id="command-1",
                account_id="manual-only",
                expected_version=0,
                changes=(ManualChange(field="note", action="set", value="kept"),),
            ),
            revision=1,
            actor="owner",
            received_at=NOW,
        )
        result: Final = report((), jobs=(job,), manual_entries=(manual,))
        self.assertEqual(
            [(item.account_id, item.references, "not_in_catalogue" in item.gaps) for item in result.accounts],
            [
                ("job-only", ("media_job",), True),
                ("manual-only", ("manual_entry",), True),
                (ACCOUNT.id, ("catalogue",), False),
            ],
        )

    def test_a_previously_known_account_survives_a_newer_scan_with_an_unavailable_source(self) -> None:
        first: Final = NOW - timedelta(minutes=10)
        known: Final = merge_evidence(
            None,
            snapshot(A, first, gateway_inventory(first, credentials=(bound("database", "kept", "previously-known"),))),
        )
        merged: Final = merge_evidence(
            known,
            snapshot(
                A,
                NOW,
                gateway_inventory(
                    NOW,
                    routes=(
                        InventoryRoute(origin="runtime", route_id="new", account_id="new-account", binding="account"),
                    ),
                    unavailable=frozenset({("credentials", "database")}),
                ),
            ),
        )
        state: Final = GatewayInventoryState(
            gateway_origin=A,
            attempt=GatewayInventoryAttempt(attempted_at=NOW, status="collected", message="collected"),
            evidence=merged,
        )
        result: Final = report((state,), accounts=())
        previous: Final = entry(result, "previously-known")
        self.assertEqual(
            [(item.credential.credential_name, item.presence, item.observed_at) for item in previous.credentials],
            [("kept", "retained", first)],
        )
        self.assertTrue({"not_in_catalogue", "binding_not_in_latest_collection"} <= set(previous.gaps))
        self.assertEqual([item.route.route_id for item in entry(result, "new-account").routes], ["new"])
        self.assertIn(
            ("source_unavailable", "database", "credentials"),
            [(item.kind, item.origin, item.source) for item in result.findings],
        )
        self.assertEqual(result.coverage, "partial")
