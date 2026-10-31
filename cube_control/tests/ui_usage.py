import os
import socket
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import uuid4

import uvicorn
from playwright.sync_api import expect, sync_playwright

from cube_control.api import create_app
from cube_control.gateway_store import GatewayRepository
from cube_control.models import Account, GatewayBatch, GatewayRecord, GatewayScan, ImportBatch, Observation
from cube_control.store import PostgresRepository
from cube_control.usage import PostgresUsageReader


def main() -> None:
    dsn: Final = os.environ["CUBE_CONTROL_TEST_DATABASE_URL"]
    repository: Final = PostgresRepository(dsn)
    with repository.connect() as connection:
        row: Final = connection.execute("SELECT current_database()").fetchone()
        if row is None or row[0] != "cubecontrol_test":
            raise RuntimeError("Browser tests require the dedicated test database")
    repository.initialize()
    source: Final = "browser-" + uuid4().hex
    provider: Final = "fixture-" + uuid4().hex
    exact_value: Final = "9007199254740993.000000000000000000000000001"
    account: Final = Account(id="ui-" + uuid4().hex, provider="heygen", name="Exact billing " + uuid4().hex, kind="api")
    checked: Final = datetime.now(UTC)
    if not repository.import_batch(
        ImportBatch(
            accounts=(account,),
            observations=(
                Observation(
                    account_id=account.id,
                    external_id="wallet",
                    metric="balance",
                    window="API-Wallet",
                    status="known",
                    source="provider_api",
                    observed_at=checked,
                    collected_at=checked,
                    value=Decimal(exact_value),
                    unit="USD",
                ),
                Observation(
                    account_id=account.id,
                    external_id="cap",
                    metric="spending_cap",
                    window="Anbieterperiode",
                    status="known",
                    source="provider_api",
                    observed_at=checked,
                    collected_at=checked,
                    value=Decimal("12.34"),
                    unit="USD",
                ),
            ),
        )
    ).ok:
        raise RuntimeError("Exact billing fixture import failed")
    now: Final = datetime(2026, 1, 15, tzinfo=UTC)
    records: Final = tuple(
        GatewayRecord(
            source_key=f"request-{index:03d}",
            provider=provider,
            model='<img src=x onerror="window.unsafe=true">' if index == 0 else "fixture-model",
            occurred_at=now,
            status="unknown" if index == 1 else "succeeded",
            amount_usd=Decimal("0.123456789123456789") if index == 0 else None if index == 1 else Decimal(0),
            input_tokens=None if index == 1 else 17,
            output_tokens=None if index == 1 else 3,
        )
        for index in range(51)
    )
    gateway: Final = GatewayRepository(dsn)

    def import_scan(sequence: int, values: tuple[GatewayRecord, ...]) -> None:
        result: Final = gateway.import_batch(
            GatewayBatch(
                scan=GatewayScan(
                    scan_id=uuid4().hex,
                    source_id=source,
                    expected_sequence=sequence,
                    checked_at=datetime.now(UTC),
                    state="ok",
                    available_rows=51,
                    scanned_rows=len(values),
                ),
                records=values,
            )
        )
        if result.status != "applied":
            raise RuntimeError("Browser fixture import failed")

    import_scan(0, records)
    import_scan(1, (records[0].model_copy(update={"amount_usd": Decimal("0.2")}),))
    import_scan(2, (records[0],))
    server: Final = uvicorn.Server(
        uvicorn.Config(
            create_app(repository, usage_reader=PostgresUsageReader(dsn)),
            log_level="error",
            access_log=False,
        )
    )
    proof: Final = Path(os.environ.get("CUBE_CONTROL_BROWSER_PROOF", "/tmp/cubellm-usage-proof"))
    proof.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        thread: Final = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser: Final = playwright.chromium.launch(
                    headless=True,
                    executable_path=os.environ.get("CUBE_CONTROL_BROWSER_EXECUTABLE"),
                )
                page: Final = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(f"http://127.0.0.1:{listener.getsockname()[1]}")
                account_card: Final = page.locator("#accounts .card").filter(has_text=account.name)
                expect(account_card).to_contain_text(exact_value + " USD")
                expect(account_card).to_contain_text("Anbieter-Ausgabenlimit")
                page.locator("#usage-period").fill("2026-01")
                page.locator("#usage-provider").fill(provider)
                page.get_by_role("button", name="Aufrufe laden", exact=True).click()
                expect(page.locator("#usage-rows tr")).to_have_count(50)
                expect(page.locator("#usage-totals")).to_contain_text("0.123456789123456789")
                expect(page.locator("#usage-totals")).to_contain_text("1 Kosten unbekannt")
                expect(page.locator("#usage-rows tr").first).to_contain_text("Konto unbekannt")
                expect(page.locator("#usage-rows img")).to_have_count(0)
                if page.evaluate("Boolean(window.unsafe)"):
                    raise AssertionError("Untrusted model name executed")
                if page.evaluate("usageTime('2026-02-01T00:30:00+01:00')") != "2026-01-31T23:30:00.000Z":
                    raise AssertionError("Usage timestamp is not displayed in UTC")
                page.locator("#usage-rows summary").first.click()
                page.get_by_role("button", name="Korrekturverlauf laden").first.click()
                expect(page.locator("#usage-rows pre").first).to_contain_text("Version 3")
                expect(page.locator("#usage-rows pre").first).to_contain_text("Gateway-USD: 0.2")
                page.locator(".usage-section").screenshot(path=str(proof / "desktop.png"))
                page.get_by_role("button", name="Weitere Aufrufe").click()
                expect(page.locator("#usage-rows tr")).to_have_count(51)
                expect(page.locator("#usage-next")).to_be_hidden()
                page.set_viewport_size({"width": 390, "height": 844})
                if page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
                    raise AssertionError("Usage overview overflows mobile viewport")
                page.locator(".usage-section").screenshot(path=str(proof / "mobile.png"))
                page.get_by_role("button", name="Aufrufe laden", exact=True).click()
                expect(page.locator("#usage-rows tr")).to_have_count(50)
                import_scan(3, records)
                page.get_by_role("button", name="Weitere Aufrufe").click()
                expect(page.locator("#usage-status-message")).to_contain_text("Erfassung hat sich geändert")
                expect(page.locator("#usage-next")).to_be_hidden()
                page.route("**/api/v1/usage?*", lambda route: route.fulfill(status=503, body="unavailable"))
                page.get_by_role("button", name="Aufrufe laden", exact=True).click()
                expect(page.locator("#usage-status-message")).to_contain_text("letzten erfolgreichen Abruf")
                expect(page.locator("#usage-rows tr")).to_have_count(50)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("Local browser test server did not stop")
    print("Browser checks passed: exact decimals, unknowns, correction history, pagination, 390px, XSS and outages")


if __name__ == "__main__":
    main()
