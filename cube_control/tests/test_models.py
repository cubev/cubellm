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
    def test_provider_character_usage_is_not_money_and_expires_at_reset(self) -> None:
        account: Final = Account(id="elevenlabs-api", provider=Provider.ELEVENLABS, name="ElevenLabs", kind="api")
        reading: Final = observation(
            account_id=account.id, metric="usage", unit="characters", value="13", period="",
            resets_at=NOW + timedelta(seconds=60), expires_at=NOW + timedelta(seconds=60),
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

    def test_reservations_are_separated_by_unit_and_expire(self) -> None:
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
        self.assertEqual(reservations(jobs, "example", NOW + timedelta(minutes=2)), {})

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
