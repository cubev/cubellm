import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr

from cube_control.heygen_jobs import CHECK_LIMIT, VideoResult, check_heygen_jobs, fetch_video
from cube_control.media import JobList, JobView
from cube_control.models import MediaJob
from cube_control.provider_access import Unavailable

NOW: Final = datetime(2026, 11, 14, 1, tzinfo=UTC)


def job(job_id: str, state: str = "submitted") -> JobView:
    return JobView(
        job=MediaJob.model_validate(
            {
                "account_id": "heygen-api",
                "job_id": job_id,
                "state": state,
                "updated_at": NOW - timedelta(hours=1),
                "reserved_amount": "1",
                "unit": "credits",
                "expires_at": NOW - timedelta(minutes=5),
                **({"actual_amount": "1"} if state == "finalized" else {}),
            }
        ),
        version=2,
    )


class Jobs:
    def __init__(self, *views: JobView) -> None:
        self.views: Final = views
        self.calls: list[tuple[str | None, int]] = []

    def recent(self, now: datetime, account_id: str | None = None, limit: int = 50) -> JobList:
        self.calls.append((account_id, limit))
        return JobList(checked_at=now, jobs=self.views, truncated=False)


def detail(video_id: str, status: str, **extra: object) -> dict[str, object]:
    return {"data": {"id": video_id, "status": status, "video_url": "https://files.example/presigned?sig=x", **extra}}


class HeyGenJobTests(unittest.TestCase):
    def setUp(self) -> None:
        directory: Final = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "heygen.env"
        self.config.write_text("HEYGEN_API_KEY=fixture-heygen-key\n")
        os.chmod(self.config, 0o600)

    def test_only_submitted_jobs_are_checked_and_mapped_to_suggestions(self) -> None:
        jobs: Final = Jobs(
            job("v_done"),
            job("v_failed"),
            job("v_busy"),
            job("v_old", "finalized"),
            job("v_unsent", "reserved"),
            job("v_expired", "expired"),
            job("v_gone"),
        )
        answers: Final[dict[str, VideoResult]] = {
            "v_done": fetch_video(
                SecretStr("k"),
                "v_done",
                httpx.MockTransport(
                    lambda r: httpx.Response(
                        200, json=detail("v_done", "completed", completed_at=1794620000, duration=12.5)
                    )
                ),
            ),
            "v_failed": fetch_video(
                SecretStr("k"),
                "v_failed",
                httpx.MockTransport(
                    lambda r: httpx.Response(200, json=detail("v_failed", "failed", failure_code="render_error"))
                ),
            ),
            "v_busy": fetch_video(
                SecretStr("k"),
                "v_busy",
                httpx.MockTransport(lambda r: httpx.Response(200, json=detail("v_busy", "processing"))),
            ),
            "v_gone": fetch_video(
                SecretStr("k"),
                "v_gone",
                httpx.MockTransport(
                    lambda r: httpx.Response(404, json={"error": {"code": "not_found", "message": "fixture-secret"}})
                ),
            ),
        }
        requested: Final[list[str]] = []

        def fetch(key: SecretStr, video_id: str) -> VideoResult:
            self.assertEqual(key.get_secret_value(), "fixture-heygen-key")
            requested.append(video_id)
            return answers[video_id]

        report: Final = check_heygen_jobs(self.config, "heygen-api", jobs, NOW, fetch)
        self.assertEqual(jobs.calls, [("heygen-api", 200)])
        self.assertEqual(requested, ["v_done", "v_failed", "v_busy", "v_gone"])
        checks: Final = {check.job_id: check for check in report.checks}
        self.assertEqual(checks["v_done"].suggestion, "finalize_after_confirming_usage")
        self.assertEqual(checks["v_done"].duration_seconds, Decimal("12.5"))
        self.assertEqual(checks["v_done"].completed_at, datetime.fromtimestamp(1794620000, UTC))
        self.assertEqual(
            (checks["v_failed"].suggestion, checks["v_failed"].failure_code), ("review_failure", "render_error")
        )
        self.assertEqual((checks["v_busy"].suggestion, checks["v_busy"].provider_status), ("wait", "processing"))
        self.assertEqual(checks["v_gone"].suggestion, "provider_status_unavailable")
        self.assertIsNone(checks["v_gone"].provider_status)
        serialized: Final = report.model_dump_json()
        for secret in ("fixture-heygen-key", "fixture-secret", "presigned", "sig=x"):
            self.assertNotIn(secret, serialized)
        self.assertFalse(report.truncated)

    def test_a_mismatched_video_id_is_not_trusted(self) -> None:
        result: Final = fetch_video(
            SecretStr("k"),
            "v_one",
            httpx.MockTransport(lambda r: httpx.Response(200, json=detail("v_two", "completed"))),
        )
        report: Final = check_heygen_jobs(self.config, "heygen-api", Jobs(job("v_one")), NOW, lambda key, video: result)
        self.assertEqual(report.checks[0].suggestion, "provider_status_unavailable")
        self.assertIsNone(report.checks[0].provider_status)

    def test_requests_use_the_documented_endpoint_without_redirects_or_proxies(self) -> None:
        seen: Final[list[httpx.Request]] = []

        def provider(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/"})

        result: Final = fetch_video(SecretStr("fixture-heygen-key"), "v/../x?y", httpx.MockTransport(provider))
        self.assertIsInstance(result, Unavailable)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].method, "GET")
        self.assertEqual(seen[0].url.raw_path, b"/v3/videos/v%2F..%2Fx%3Fy")
        self.assertEqual(seen[0].headers["x-api-key"], "fixture-heygen-key")

    def test_scope_and_invalid_answers_stay_unavailable(self) -> None:
        for response, message in (
            (httpx.Response(403, json={}), "videos:read"),
            (httpx.Response(200, json={"data": {"id": "v", "status": "rendered"}}), "ungültig"),
            (httpx.Response(200, content=b"{" * 10), "ungültig"),
            (httpx.Response(200, json=detail("v", "failed", failure_code="x" * 201)), "ungültig"),
        ):
            with self.subTest(status=response.status_code, body=response.content[:40]):
                result = fetch_video(SecretStr("k"), "v", httpx.MockTransport(lambda r, answer=response: answer))
                self.assertIsInstance(result, Unavailable)
                self.assertIn(message, getattr(result, "message", ""))

    def test_missing_or_public_key_files_make_no_request(self) -> None:
        os.chmod(self.config, 0o644)

        def fail(key: SecretStr, video: str) -> VideoResult:
            raise AssertionError("no request expected")

        report: Final = check_heygen_jobs(self.config, "heygen-api", Jobs(job("v_one")), NOW, fail)
        self.assertEqual(report.checks, ())
        self.assertTrue(report.message)

    def test_a_run_checks_at_most_the_limit(self) -> None:
        views: Final = tuple(job(f"v_{index}") for index in range(CHECK_LIMIT + 3))
        unavailable: Final = Unavailable("error", "fixture")
        report: Final = check_heygen_jobs(self.config, "heygen-api", Jobs(*views), NOW, lambda key, video: unavailable)
        self.assertEqual((len(report.checks), report.truncated), (CHECK_LIMIT, True))
        self.assertEqual(json.loads(report.model_dump_json())["account_id"], "heygen-api")


if __name__ == "__main__":
    unittest.main()
