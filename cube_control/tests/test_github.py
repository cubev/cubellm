import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr, ValidationError

from cube_control.github import RateLimits, collect_github, fetch_limits, quota_readings
from cube_control.models import Account
from cube_control.provider_access import Unavailable
from cube_control.store import StoredState
from cube_control.summary import account_view

NOW: Final = datetime(2026, 1, 10, tzinfo=UTC)


class GitHubTests(unittest.TestCase):
    def test_fixed_read_endpoint_and_authoritative_headers_preserve_other_resource_units(self) -> None:
        reset: Final = int((NOW + timedelta(minutes=5)).timestamp())

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://api.github.com/rate_limit")
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["authorization"], "Bearer private-canary")
            self.assertIn("x-github-api-version", request.headers)
            return httpx.Response(
                200,
                json={
                    "resources": {
                        "core": {"used": 1, "limit": 10, "remaining": 9, "reset": reset},
                        "graphql": {"used": 5, "limit": 20, "remaining": 15, "reset": reset},
                        "future_resource": {"used": 0},
                    }
                },
                headers={
                    "x-ratelimit-resource": "core",
                    "x-ratelimit-used": "2",
                    "x-ratelimit-limit": "20",
                    "x-ratelimit-remaining": "18",
                    "x-ratelimit-reset": str(reset + 1),
                },
            )

        result: Final = fetch_limits(SecretStr("private-canary"), httpx.MockTransport(handler))
        self.assertIsInstance(result, RateLimits)
        rows: Final = quota_readings("account-one", result, NOW)
        core: Final = next(row for row in rows if row.metric == "usage" and row.window == "GitHub core")
        self.assertEqual(core.value, Decimal(2))
        self.assertEqual(core.unit, "requests")
        self.assertEqual(core.expires_at, datetime.fromtimestamp(reset + 1, UTC))
        self.assertEqual(core.freshness(datetime.fromtimestamp(reset + 1, UTC)), "expired")
        self.assertEqual(next(row for row in rows if row.window == "GitHub graphql").unit, "points")
        self.assertEqual(next(row for row in rows if row.window == "GitHub future_resource").unit, "quota_units")
        self.assertTrue(all(row.metric not in ("spend", "budget") and row.period == "" for row in rows))
        self.assertNotIn("private-canary", str(rows))

    def test_numeric_precision_null_zero_and_provider_inconsistency_are_preserved(self) -> None:
        raw: Final = b'{"resources":{"core":{"used":9007199254740993,"limit":1,"remaining":null},"graphql":{"used":0,"remaining":0,"limit":0}}}'
        result: Final = fetch_limits(SecretStr("test"), httpx.MockTransport(lambda _: httpx.Response(200, content=raw)))
        rows: Final = quota_readings("example", result, NOW)
        core: Final = tuple(row for row in rows if row.window == "GitHub core")
        self.assertEqual(next(row for row in core if row.metric == "usage").value, Decimal("9007199254740993"))
        self.assertEqual(next(row for row in core if row.metric == "quota_remaining").status, "unknown")
        self.assertEqual(next(row for row in core if row.metric == "quota_limit").value, Decimal(1))
        self.assertTrue(
            all(row.value == Decimal(0) and row.status == "known" for row in rows if row.window == "GitHub graphql")
        )

    def test_snapshot_removal_outage_and_recovery_do_not_resurrect_old_quotas(self) -> None:
        first: Final = RateLimits.model_validate({"resources": {"core": {"used": 1}, "graphql": {"used": 2}}})
        latest: Final = RateLimits.model_validate({"resources": {"core": {"used": 3}}})
        account: Final = Account(id="example", provider="github", name="Fixture", kind="api")
        old: Final = quota_readings(account.id, first, NOW)
        outage: Final = quota_readings(account.id, Unavailable("error", "Unavailable"), NOW + timedelta(seconds=1))
        failed: Final = account_view(
            account, StoredState((account,), old + outage, (), ()), NOW + timedelta(seconds=1), "2026-01"
        )
        self.assertEqual(failed.observations, outage)
        current: Final = quota_readings(account.id, latest, NOW + timedelta(seconds=2))
        state: Final = StoredState((account,), old + outage + current, (), ())
        view: Final = account_view(account, state, NOW + timedelta(seconds=3), "2026-01")
        self.assertEqual({row.window for row in view.observations}, {"GitHub core"})
        self.assertEqual(next(row for row in view.observations if row.metric == "usage").value, Decimal(3))
        self.assertIsNone(view.selected_spend)

    def test_malformed_provider_data_and_headers_never_become_zero(self) -> None:
        for value in (
            {},
            {"rate": {"used": 1}},
            {"resources": {}},
            {"resources": {"core": {"used": True}}},
            {"resources": {"core": {"used": 1.2}}},
            {"resources": {"core": {"used": "1"}}},
            {"resources": {"core": {"remaining": -1}}},
            {"resources": {"core": {"reset": True}}},
            {"resources": {"core": {"reset": 253402300800}}},
            {"resources": {"not/a/resource": {"used": 1}}},
        ):
            with self.subTest(value=value):
                result: Final = fetch_limits(
                    SecretStr("test"), httpx.MockTransport(lambda _: httpx.Response(200, json=value))
                )
                self.assertIsInstance(result, Unavailable)
                self.assertIsNone(quota_readings("example", result, NOW)[0].value)
        valid: Final = {"resources": {"core": {"used": 1}}}
        for headers in (
            {"x-ratelimit-resource": "core"},
            {
                "x-ratelimit-resource": "core",
                "x-ratelimit-used": "-1",
                "x-ratelimit-limit": "10",
                "x-ratelimit-remaining": "9",
                "x-ratelimit-reset": "123",
            },
        ):
            with self.subTest(headers=headers):
                result: Final = fetch_limits(
                    SecretStr("test"), httpx.MockTransport(lambda _: httpx.Response(200, json=valid, headers=headers))
                )
                self.assertIsInstance(result, Unavailable)

    def test_errors_and_redirects_do_not_forward_bodies_or_credentials(self) -> None:
        for status in (302, 401, 403, 429, 500):
            calls: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                return httpx.Response(status, text="private-canary", headers={"Location": "https://elsewhere.invalid/"})

            result: Final = fetch_limits(SecretStr("test"), httpx.MockTransport(handler))
            self.assertIsInstance(result, Unavailable)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("private-canary", str(result))

    def test_private_collection_is_account_qualified_and_invalid_accounts_never_fetch(self) -> None:
        result: Final = RateLimits.model_validate({"resources": {"core": {"used": 1}}})
        with tempfile.TemporaryDirectory() as directory:
            path: Final = Path(directory) / "provider.env"
            path.write_text("GITHUB_TOKEN=fixture-key\n")
            path.chmod(0o600)
            first: Final = collect_github(path, NOW, lambda _: result, account_id="first")
            second: Final = collect_github(path, NOW, lambda _: result, account_id="second")
            self.assertEqual(first.accounts[0].id, "first")
            self.assertEqual({row.account_id for row in second.observations}, {"second"})
            self.assertTrue({row.id for row in first.observations}.isdisjoint(row.id for row in second.observations))
            self.assertNotIn("fixture-key", first.model_dump_json())
            with self.assertRaises(ValidationError):
                collect_github(
                    path, NOW, lambda _: self.fail("Invalid account reached provider"), account_id="bad/account"
                )
            path.chmod(0o644)
            failure: Final = collect_github(path, NOW, lambda _: self.fail("Unsafe key was used"))
            self.assertFalse(failure.accounts)
            self.assertEqual(failure.observations[0].status, "error")

    def test_cli_without_database_emits_unconfigured_report_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Final = Path(directory) / "provider.env"
            path.write_text("GITHUB_TOKEN=\n")
            path.chmod(0o600)
            environment: Final = {key: value for key, value in os.environ.items() if key != "CUBE_CONTROL_DATABASE_URL"}
            output: Final = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cube_control",
                    "collect-github",
                    "--config",
                    str(path),
                    "--account-id",
                    "custom",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            batch: Final = json.loads(output.stdout)
            self.assertEqual(batch["observations"][0]["account_id"], "custom")
            self.assertEqual(batch["observations"][0]["status"], "not_configured")
            self.assertEqual(batch["accounts"], [])


if __name__ == "__main__":
    unittest.main()
