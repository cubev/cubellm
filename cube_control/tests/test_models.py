import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from cube_control.api import reading_view
from cube_control.models import Account, MediaJob, Observation, Provider, UsageEvent
from cube_control.store import StoredState
from cube_control.summary import account_view, reservations, selected_spend

NOW: Final[datetime] = datetime(2026, 1, 15, 12, tzinfo=UTC)


def observation(**changes: object) -> Observation:
    data: Final[dict[str, object]] = {
        "account_id": "example",
        "external_id": "report-1",
        "metric": "spend",
        "window": "month",
        "period": "2026-01",
        "status": "known",
        "source": "provider_api",
        "observed_at": NOW,
        "collected_at": NOW,
        "value": "12.50",
        "unit": "USD",
    }
    return Observation.model_validate(data | changes)


class ModelTests(unittest.TestCase):
    def test_full_provider_snapshots_retire_missing_resources_and_preserve_other_sources(self) -> None:
        account: Final = Account(id="example", provider="fixture", name="Fixture", kind="api")
        old: Final = observation(metric="balance", window="removed-resource", period="", snapshot_scope="quotas")
        current: Final = observation(
            metric="balance",
            window="current-resource",
            period="",
            snapshot_scope="quotas",
            external_id="new",
            collected_at=NOW + timedelta(seconds=1),
            observed_at=NOW + timedelta(seconds=1),
        )
        independent: Final = observation(metric="balance", window="wallet", period="", snapshot_scope="wallet")
        unscoped: Final = observation(metric="balance", window="legacy", period="")
        state: Final = StoredState((account,), (old, current, independent, unscoped), (), ())
        view: Final = account_view(account, state, NOW + timedelta(seconds=2), "2026-01")
        self.assertEqual({row.window for row in view.observations}, {"current-resource", "wallet", "legacy"})
        self.assertFalse(reading_view(old, state.observations, NOW + timedelta(seconds=2)).current)
        other_metric: Final = current.model_copy(update={"metric": "usage"})
        self.assertFalse(reading_view(old, (old, other_metric), NOW + timedelta(seconds=2)).current)

    def test_snapshot_outages_and_late_arrivals_do_not_resurrect_old_or_poison_independent_data(self) -> None:
        account: Final = Account(id="example", provider="fixture", name="Fixture", kind="api")
        old: Final = observation(snapshot_scope="billing")
        outage: Final = observation(
            snapshot_scope="billing",
            window="all",
            status="error",
            value=None,
            observed_at=NOW + timedelta(seconds=2),
            collected_at=NOW + timedelta(seconds=2),
        )
        late: Final = observation(
            snapshot_scope="billing",
            external_id="late",
            value="99",
            observed_at=NOW + timedelta(seconds=1),
            collected_at=NOW + timedelta(seconds=1),
        )
        independent: Final = observation(snapshot_scope="independent", external_id="independent")
        state: Final = StoredState((account,), (outage, late, old, independent), (), ())
        view: Final = account_view(account, state, NOW + timedelta(seconds=3), "2026-01")
        self.assertEqual({row.external_id for row in view.observations}, {outage.external_id, independent.external_id})
        self.assertEqual(view.selected_spend, independent)
        self.assertTrue(reading_view(independent, (independent, outage), NOW + timedelta(seconds=3)).current)

    def test_provider_character_usage_is_not_money_and_expires_at_reset(self) -> None:
        account: Final = Account(id="elevenlabs-api", provider=Provider.ELEVENLABS, name="ElevenLabs", kind="api")
        reading: Final = observation(
            account_id=account.id,
            metric="usage",
            unit="characters",
            value="13",
            period="",
            resets_at=NOW + timedelta(seconds=60),
            expires_at=NOW + timedelta(seconds=60),
        )
        restored: Final = Observation.model_validate_json(reading.model_dump_json())
        self.assertEqual(restored.value, Decimal(13))
        self.assertEqual(restored.unit, "characters")
        self.assertEqual(restored.freshness(NOW + timedelta(seconds=60)), "expired")
        with self.assertRaises(ValidationError):
            observation(unit="characters", value="13")

    def test_unavailable_cannot_look_like_zero_spend(self) -> None:
        with self.assertRaises(ValidationError):
            observation(status="error", value="0")
        self.assertIsNone(observation(status="error", value=None).value)

    def test_money_rejects_credit_units(self) -> None:
        with self.assertRaises(ValidationError):
            observation(unit="credits")

    def test_timezone_and_nonfinite_values_are_rejected(self) -> None:
        for invalid in ({"observed_at": NOW.replace(tzinfo=None)}, {"value": "NaN"}, {"value": "Infinity"}):
            with self.assertRaises(ValidationError):
                observation(**invalid)

    def test_stale_expired_and_clock_skew_are_distinct(self) -> None:
        self.assertEqual(observation().freshness(NOW + timedelta(seconds=901)), "stale")
        self.assertEqual(observation(expires_at=NOW).freshness(NOW), "expired")
        self.assertEqual(observation(observed_at=NOW + timedelta(minutes=2)).freshness(NOW), "clock_skew")

    def test_one_weekly_window_is_valid(self) -> None:
        reading: Final[Observation] = observation(metric="limit", window="7d", value="0.09", unit="ratio")
        self.assertTrue(reading_view(reading, (reading,), NOW).current)

    def test_new_collector_outage_masks_older_limit(self) -> None:
        reading: Final[Observation] = observation(metric="limit", window="7d", value="0.09", unit="ratio")
        outage: Final[Observation] = observation(
            external_id="outage",
            metric="limit",
            window="all",
            value=None,
            status="error",
            observed_at=NOW + timedelta(seconds=1),
        )
        self.assertFalse(reading_view(reading, (reading, outage), NOW + timedelta(seconds=2)).current)

    def test_provider_report_is_not_added_to_proxy_usage(self) -> None:
        account: Final[Account] = Account(id="example", provider=Provider.OPENAI, name="Example", kind="api")
        event: Final[UsageEvent] = UsageEvent(
            account_id=account.id,
            event_id="request-1",
            occurred_at=NOW,
            collected_at=NOW,
            model="example-model",
            period="2026-01",
            amount=Decimal("8.00"),
        )
        state: Final[StoredState] = StoredState((account,), (observation(),), (event,), ())
        view: Final = account_view(account, state, NOW, "2026-01")
        self.assertEqual(view.selected_spend.value if view.selected_spend else None, Decimal("12.50"))
        self.assertEqual(view.proxy_amounts, {"USD": Decimal("8.00")})

    def test_wrong_period_and_stale_report_do_not_become_current(self) -> None:
        self.assertIsNone(selected_spend((observation(),), NOW, "2026-02"))
        self.assertIsNone(selected_spend((observation(),), NOW + timedelta(hours=1), "2026-01"))

    def test_spend_selection_matches_reading_availability_after_source_outage(self) -> None:
        reading: Final = observation(observed_at=NOW - timedelta(seconds=10))
        fallback: Final = observation(external_id="fallback", source="import", value="9.00")
        for status in ("error", "unknown", "not_configured"):
            outage: Final = observation(external_id="outage", window="all", status=status, value=None)
            with self.subTest(status=status):
                self.assertFalse(reading_view(reading, (reading, outage), NOW).current)
                self.assertIsNone(selected_spend((reading, outage), NOW, reading.period))
                self.assertEqual(selected_spend((reading, outage, fallback), NOW, reading.period), fallback)

    def test_spend_recovers_only_after_newer_report_from_same_source(self) -> None:
        outage: Final = observation(external_id="outage", window="all", status="error", value=None)
        for offset in (-1, 0, 1):
            reading: Final = observation(observed_at=NOW + timedelta(seconds=offset))
            with self.subTest(offset=offset):
                self.assertEqual(reading_view(reading, (reading, outage), NOW).current, offset > 0)
                self.assertEqual(
                    selected_spend((reading, outage), NOW, reading.period), reading if offset > 0 else None
                )

    def test_unrelated_outages_do_not_hide_current_spend(self) -> None:
        reading: Final = observation()
        for changes in ({"source": "import"}, {"metric": "balance"}, {"account_id": "other"}):
            outage: Final = observation(external_id="outage", window="all", status="error", value=None, **changes)
            with self.subTest(changes=changes):
                self.assertTrue(reading_view(reading, (reading, outage), NOW).current)
                self.assertEqual(selected_spend((reading, outage), NOW, reading.period), reading)

    def test_submitted_reservations_remain_separate_and_held_after_local_deadline(self) -> None:
        jobs: Final[tuple[MediaJob, ...]] = tuple(
            MediaJob(
                account_id="example",
                job_id=unit,
                state="submitted",
                updated_at=NOW,
                reserved_amount=Decimal(3),
                unit=unit,
                expires_at=NOW + timedelta(minutes=1),
            )
            for unit in ("USD", "credits")
        )
        self.assertEqual(reservations(jobs, "example", NOW), {"USD": Decimal(3), "credits": Decimal(3)})
        self.assertEqual(
            reservations(jobs, "example", NOW + timedelta(minutes=2)), {"USD": Decimal(3), "credits": Decimal(3)}
        )
        unsubmitted: Final = tuple(job.model_copy(update={"state": "reserved"}) for job in jobs)
        self.assertEqual(reservations(unsubmitted, "example", NOW + timedelta(minutes=2)), {})
        self.assertFalse(
            jobs[0].model_copy(update={"state": "expired", "updated_at": NOW + timedelta(hours=1)}).can_replace(jobs[0])
        )

    def test_media_jobs_cannot_be_reopened_or_switch_units(self) -> None:
        reserved: Final[MediaJob] = MediaJob(
            account_id="example",
            job_id="job-1",
            state="reserved",
            updated_at=NOW,
            reserved_amount=Decimal(3),
            unit="credits",
            expires_at=NOW + timedelta(hours=1),
        )
        submitted: Final[MediaJob] = reserved.model_copy(update={"state": "submitted"})
        completed: Final[MediaJob] = reserved.model_copy(update={"state": "finalized", "actual_amount": Decimal(2)})
        self.assertTrue(submitted.can_replace(reserved))
        self.assertTrue(completed.can_replace(submitted))
        self.assertFalse(submitted.can_replace(completed))
        self.assertFalse(submitted.model_copy(update={"unit": "USD"}).can_replace(reserved))
