import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from cube_control.api import reading_view
from cube_control.manual import active_fields, effective_account
from cube_control.models import Account, ManualChange, ManualCommand, ManualEntry, Observation, Provider
from cube_control.store import StoredState
from cube_control.summary import account_view

NOW: Final = datetime(2026, 1, 10, tzinfo=UTC)
ACCOUNT: Final = Account(id="example", provider=Provider.SUNO, name="Example", kind="subscription", plan="Automatic")


def entry(revision: int, changes: tuple[ManualChange, ...]) -> ManualEntry:
    return ManualEntry(
        command=ManualCommand(
            command_id=f"command-{revision}", account_id=ACCOUNT.id, expected_version=revision - 1, changes=changes
        ),
        revision=revision,
        actor="local-owner",
        received_at=NOW,
    )


class ManualTests(unittest.TestCase):
    def test_per_field_override_preserves_collector_and_can_return_to_automatic(self) -> None:
        first: Final = entry(1, (ManualChange(field="plan", action="set", value="Manual plan"),))
        second: Final = entry(2, (ManualChange(field="state", action="set", value="paused"),))
        automatic: Final = ACCOUNT.model_copy(update={"plan": "New automatic plan", "state": "active"})
        effective: Final = effective_account(automatic, active_fields(ACCOUNT.id, (first, second)))
        self.assertEqual((effective.plan, effective.state), ("Manual plan", "paused"))
        release: Final = entry(3, (ManualChange(field="plan", action="automatic"),))
        returned: Final = effective_account(automatic, active_fields(ACCOUNT.id, (first, second, release)))
        self.assertEqual((returned.plan, returned.state), ("New automatic plan", "paused"))
        self.assertEqual(automatic.plan, "New automatic plan")

    def test_revision_order_wins_over_wall_clock_and_other_accounts(self) -> None:
        first: Final = entry(1, (ManualChange(field="plan", action="set", value="First"),))
        second: Final = entry(2, (ManualChange(field="plan", action="set", value="Second"),)).model_copy(
            update={"received_at": NOW - timedelta(hours=1)}
        )
        self.assertEqual(active_fields(ACCOUNT.id, (second, first))[0].value, "Second")
        self.assertEqual(active_fields("different", (first,)), ())

    def test_budget_override_does_not_get_restamped_or_overwritten_by_collector(self) -> None:
        budget: Final = entry(1, (ManualChange(field="budget_usd", period="2026-01", action="set", value="15.20"),))
        collected: Final = Observation(
            account_id=ACCOUNT.id,
            external_id="config",
            metric="budget",
            window="Monat",
            period="2026-01",
            status="manual",
            source="manual",
            observed_at=NOW + timedelta(minutes=1),
            collected_at=NOW + timedelta(minutes=1),
            value=Decimal(100),
            unit="USD",
        )
        state: Final = StoredState((ACCOUNT,), (collected,), (), (), manual_entries=(budget,))
        view: Final = account_view(ACCOUNT, state, NOW + timedelta(minutes=2), "2026-01")
        self.assertEqual(len(view.observations), 1)
        self.assertEqual(view.observations[0].value, Decimal("15.20"))
        self.assertEqual(view.observations[0].observed_at, NOW)
        self.assertEqual(state.observations, (collected,))
        self.assertEqual(view.manual_version, 1)
        released: Final = entry(2, (ManualChange(field="budget_usd", period="2026-01", action="automatic"),))
        restored: Final = account_view(
            ACCOUNT, StoredState((ACCOUNT,), (collected,), (), (), manual_entries=(budget, released)), NOW, "2026-01"
        )
        self.assertEqual(restored.observations, (collected,))

    def test_future_month_budget_is_not_current(self) -> None:
        budget: Final = entry(1, (ManualChange(field="budget_usd", period="2026-02", action="set", value="20"),))
        view: Final = account_view(
            ACCOUNT, StoredState((ACCOUNT,), (), (), (), manual_entries=(budget,)), NOW, "2026-01"
        )
        self.assertFalse(reading_view(view.observations[0], view.observations, NOW).current)

    def test_invalid_values_and_forged_provenance_are_rejected(self) -> None:
        for field, value in (
            ("state", "possibly"),
            ("budget_usd", "-1"),
            ("recurring_usd", "0.001"),
            ("balance_usd", "NaN"),
            ("balance_credits", "1e100"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                ManualChange(field=field, action="set", value=value, period="2026-01" if field == "budget_usd" else "")
        with self.assertRaises(ValidationError):
            ManualChange.model_validate({"field": "plan", "action": "set", "value": "x", "source": "provider_api"})
        with self.assertRaises(ValidationError):
            ManualChange(field="plan", action="automatic", value="unexpected")

    def test_duplicate_changes_and_missing_period_are_rejected(self) -> None:
        change: Final = ManualChange(field="plan", action="set", value="x")
        with self.assertRaises(ValidationError):
            ManualCommand(command_id="duplicate", account_id=ACCOUNT.id, expected_version=0, changes=(change, change))
        with self.assertRaises(ValidationError):
            ManualChange(field="budget_usd", action="set", value="10")
