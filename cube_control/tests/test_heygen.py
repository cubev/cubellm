import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr

from cube_control.heygen import BillingProfile, collect_heygen, fetch_profile, profile_readings
from cube_control.models import Account
from cube_control.provider_access import Unavailable
from cube_control.store import StoredState
from cube_control.summary import account_view

NOW: Final = datetime(2026, 1, 10, tzinfo=UTC)


class HeyGenTests(unittest.TestCase):
    def test_wallet_numbers_keep_all_digits_without_exporting_profile_or_key(self) -> None:
        raw: Final = b'{"data":{"billing_type":"wallet","wallet":{"currency":"usd","remaining_balance":0.12345678901234567890123456789},"email":"private-profile-canary"}}'

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://api.heygen.com/v3/users/me")
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["x-api-key"], "private-key-canary")
            return httpx.Response(200, content=raw)

        result: Final = fetch_profile(SecretStr("private-key-canary"), httpx.MockTransport(handler))
        self.assertIsInstance(result, BillingProfile)
        readings: Final = profile_readings("account-one", result, NOW)
        self.assertEqual(readings[0].value, Decimal("0.12345678901234567890123456789"))
        self.assertEqual(readings[0].unit, "USD")
        self.assertTrue(all(row.status == "unknown" and row.value is None for row in readings[1:]))
        self.assertNotIn("private-profile-canary", str(result))
        self.assertNotIn("private-key-canary", str(result))

    def test_wallet_null_zero_credits_and_overdraft_are_distinct(self) -> None:
        for currency, value in (("usd", None), ("usd", "0"), ("credits", "60"), ("usd", "-0.1")):
            with self.subTest(currency=currency, value=value):
                profile: Final = BillingProfile.model_validate(
                    {"billing_type": "wallet", "wallet": {"currency": currency, "remaining_balance": value}}
                )
                row: Final = profile_readings("example", profile, NOW)[0]
                self.assertEqual(row.value, Decimal(value) if value is not None else None)
                self.assertEqual(row.status, "known" if value is not None else "unknown")
                self.assertEqual(row.unit, None if value is None else "USD" if currency == "usd" else "credits")

    def test_subscription_pools_reset_independently_and_old_wallet_is_not_current(self) -> None:
        wallet: Final = BillingProfile.model_validate(
            {"billing_type": "wallet", "wallet": {"currency": "usd", "remaining_balance": "10"}}
        )
        subscription: Final = BillingProfile.model_validate(
            {
                "billing_type": "subscription",
                "subscription": {
                    "plan": "fixture",
                    "credits": {
                        "premium_credits": {"remaining": 0, "resets_at": NOW.isoformat()},
                        "add_on_credits": {"remaining": 7},
                    },
                },
            }
        )
        rows: Final = profile_readings("example", subscription, NOW)
        self.assertEqual(rows[1].value, Decimal(0))
        self.assertEqual(rows[1].freshness(NOW), "expired")
        self.assertEqual(rows[2].value, Decimal(7))
        self.assertIsNone(rows[2].expires_at)
        account: Final = Account(id="example", provider="heygen", name="Fixture", kind="api")
        state: Final = StoredState(
            (account,), profile_readings("example", wallet, NOW - timedelta(seconds=1)) + rows, (), ()
        )
        view: Final = account_view(account, state, NOW, "2026-01")
        self.assertEqual(next(row for row in view.observations if row.window == "API-Wallet").status, "unknown")

    def test_metered_usage_and_cap_do_not_invent_monthly_spending(self) -> None:
        profile: Final = BillingProfile.model_validate(
            {
                "billing_type": "usage_based",
                "usage_based": {"spending_current_usd": "0.1234567890123456789", "spending_cap_usd": "12.34"},
            }
        )
        rows: Final = profile_readings("example", profile, NOW)
        self.assertEqual(rows[3].value, Decimal("0.1234567890123456789"))
        self.assertEqual(rows[4].metric, "spending_cap")
        self.assertEqual(rows[4].value, Decimal("12.34"))
        self.assertTrue(all(row.period == "" and row.metric not in ("spend", "budget") for row in rows))

    def test_invalid_and_unavailable_data_never_becomes_zero_or_echoes_provider_errors(self) -> None:
        for raw in (
            b"not json",
            b"\xff",
            b'{"data":{}}',
            b'{"error":"private-canary","data":{}}',
            b'{"data":{"billing_type":"unknown"}}',
            b'{"data":{"billing_type":"wallet"}}',
            b'{"data":{"billing_type":"wallet","wallet":{"currency":"usd","remaining_balance":true}}}',
            b'{"data":{"billing_type":"wallet","wallet":{"currency":"usd","remaining_balance":"NaN"}}}',
        ):
            with self.subTest(raw=raw):
                result: Final = fetch_profile(
                    SecretStr("test"), httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
                )
                self.assertIsInstance(result, Unavailable)
                self.assertNotIn("private-canary", str(result))
                self.assertTrue(all(row.value is None for row in profile_readings("example", result, NOW)))
        for status in (401, 403, 429, 500, 302):
            calls: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                return httpx.Response(status, text="private-canary", headers={"Location": "https://elsewhere.invalid/"})

            result: Final = fetch_profile(SecretStr("test"), httpx.MockTransport(handler))
            self.assertIsInstance(result, Unavailable)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("private-canary", str(result))

    def test_oversized_report_stops_reading_and_network_errors_are_sanitized(self) -> None:
        chunks: list[int] = []

        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                for index in range(100):
                    chunks.append(index)
                    yield b"x" * 65536

        result: Final = fetch_profile(
            SecretStr("test"), httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
        )
        self.assertIsInstance(result, Unavailable)
        self.assertLess(len(chunks), 100)

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("private-canary", request=request)

        failure: Final = fetch_profile(SecretStr("test"), httpx.MockTransport(timeout))
        self.assertIsInstance(failure, Unavailable)
        self.assertNotIn("private-canary", str(failure))

    def test_collection_keeps_account_identity_and_never_fetches_without_private_key(self) -> None:
        profile: Final = BillingProfile.model_validate(
            {"billing_type": "wallet", "wallet": {"currency": "credits", "remaining_balance": "12.3"}}
        )
        with tempfile.TemporaryDirectory() as directory:
            path: Final = Path(directory) / "provider.env"
            path.write_text("HEYGEN_API_KEY=fixture-private-key\n")
            path.chmod(0o600)
            batch: Final = collect_heygen(path, NOW, lambda _: profile, account_id="account-two", account_name="Second")
            self.assertEqual(batch.accounts[0].id, "account-two")
            self.assertEqual({row.account_id for row in batch.observations}, {"account-two"})
            self.assertNotIn("fixture-private-key", batch.model_dump_json())
            path.chmod(0o644)
            failure: Final = collect_heygen(
                path, NOW, lambda _: self.fail("Non-private key was used"), account_id="account-two"
            )
            self.assertFalse(failure.accounts)
            self.assertTrue(all(row.status == "error" and row.value is None for row in failure.observations))


if __name__ == "__main__":
    unittest.main()
