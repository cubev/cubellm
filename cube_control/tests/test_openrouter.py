import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr

from cube_control.openrouter import Credits, Unavailable, collect_openrouter, fetch_credits, read_management_key

NOW: Final = datetime(2026, 1, 4, tzinfo=UTC)


class OpenRouterTests(unittest.TestCase):
    def test_balance_uses_decimal_and_never_becomes_monthly_spend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Final = Path(directory) / "provider.env"
            path.write_text('OPENROUTER_MANAGEMENT_KEY="example-test-value"\n')
            path.chmod(0o600)
            batch: Final = collect_openrouter(
                path, NOW, lambda _: Credits(total_credits=Decimal("0.3"), total_usage=Decimal("0.1"))
            )
            self.assertEqual(batch.observations[0].value, Decimal("0.2"))
            self.assertEqual(batch.observations[1].value, Decimal("0.1"))
            self.assertEqual({row.metric for row in batch.observations}, {"balance", "usage"})
            self.assertTrue(all(not row.period and row.unit == "USD" for row in batch.observations))
            self.assertNotIn("example-test-value", batch.model_dump_json())

    def test_missing_or_nonprivate_key_never_calls_provider(self) -> None:
        def unexpected(_: SecretStr) -> Credits:
            self.fail("A request was made without a usable private credential")

        with tempfile.TemporaryDirectory() as directory:
            path: Final = Path(directory) / "provider.env"
            path.write_text("OPENROUTER_MANAGEMENT_KEY=\n")
            path.chmod(0o600)
            missing: Final = collect_openrouter(path, NOW, unexpected)
            self.assertTrue(all(row.status == "not_configured" and row.value is None for row in missing.observations))
            path.write_text("OPENROUTER_MANAGEMENT_KEY=example-test-value\n")
            path.chmod(0o644)
            denied: Final = collect_openrouter(path, NOW, unexpected)
            self.assertTrue(all(row.status == "error" and row.value is None for row in denied.observations))
            self.assertFalse(denied.accounts)

    def test_invalid_response_is_not_zero_and_does_not_echo_body(self) -> None:
        for raw in (
            b'{"data":{}}',
            b'{"data":{"total_credits":true,"total_usage":0}}',
            b'{"data":{"total_credits":"NaN","total_usage":0}}',
            b'{"data":{"total_credits":0,"total_usage":-1}}',
        ):
            with self.subTest(raw=raw):
                result: Final = fetch_credits(
                    SecretStr("test"), httpx.MockTransport(lambda _, content=raw: httpx.Response(200, content=content))
                )
                self.assertIsInstance(result, Unavailable)
        denied: Final = fetch_credits(
            SecretStr("test"), httpx.MockTransport(lambda _: httpx.Response(403, text="sensitive-response"))
        )
        self.assertIsInstance(denied, Unavailable)
        self.assertNotIn("sensitive-response", repr(denied))

    def test_only_fixed_read_endpoint_and_no_redirect_credentials(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "openrouter.ai")
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["authorization"], "Bearer test")
            return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/"})

        result: Final = fetch_credits(SecretStr("test"), httpx.MockTransport(handler))
        self.assertIsInstance(result, Unavailable)

    def test_negative_balance_is_preserved_as_overdrawn(self) -> None:
        result: Final = fetch_credits(
            SecretStr("test"),
            httpx.MockTransport(lambda _: httpx.Response(200, json={"data": {"total_credits": 5, "total_usage": 7}})),
        )
        self.assertIsInstance(result, Credits)
        if isinstance(result, Credits):
            self.assertEqual(result.total_credits - result.total_usage, Decimal(-2))

    def test_incomplete_assignment_and_duplicate_key_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Final = Path(directory) / "provider.env"
            path.write_text("OPENROUTER_MANAGEMENT_KEY\n")
            path.chmod(0o600)
            missing: Final = read_management_key(path)
            self.assertIsInstance(missing, Unavailable)
            path.write_text("OPENROUTER_MANAGEMENT_KEY=\nOPENROUTER_MANAGEMENT_KEY=test\n")
            duplicate: Final = read_management_key(path)
            self.assertIsInstance(duplicate, Unavailable)
            if isinstance(duplicate, Unavailable):
                self.assertEqual(duplicate.status, "error")
