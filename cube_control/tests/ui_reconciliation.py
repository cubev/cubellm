import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import uuid4

import uvicorn
from playwright.sync_api import expect, sync_playwright

from cube_control.api import create_app
from cube_control.gateway_store import GatewayRepository
from cube_control.models import (
    Account,
    GatewayBatch,
    GatewayRecord,
    GatewayScan,
    ImportBatch,
    Observation,
    SpendCoverage,
)
from cube_control.store import PostgresRepository

START: Final = datetime(2026, 9, 1, tzinfo=UTC)
END: Final = datetime(2026, 10, 1, tzinfo=UTC)


def remove_fixtures(repository: PostgresRepository, source: str, account_id: str) -> None:
    with repository.connect() as connection:
        for table in ("gateway_changes", "gateway_records", "gateway_scans"):
            connection.execute(f"DELETE FROM cube_control.{table} WHERE source_id=%s", (source,))
        connection.execute("DELETE FROM cube_control.observations WHERE account_id=%s", (account_id,))
        connection.execute("DELETE FROM cube_control.accounts WHERE id=%s", (account_id,))


def store_month(repository: PostgresRepository, gateway: GatewayRepository, source: str, account: Account) -> None:
    statement: Final = Observation(
        account_id=account.id,
        external_id="september-invoice",
        metric="spend",
        window="Monatsabrechnung",
        status="known",
        source="import",
        observed_at=END + timedelta(days=2),
        collected_at=END + timedelta(days=2),
        value=Decimal("4"),
        unit="USD",
        period="2026-09",
        coverage=SpendCoverage(start=START, end=END, basis="final_billing_statement", scope="account_total"),
    )
    if not repository.import_batch(ImportBatch(accounts=(account,), observations=(statement,))).ok:
        raise RuntimeError("Browser fixture import failed")
    records: Final = tuple(
        GatewayRecord(
            source_key=key,
            provider=account.provider,
            account_id=account.id,
            call_type=call_type,
            model="fixture-model",
            occurred_at=START + timedelta(days=3),
            status="succeeded",
            amount_usd=Decimal(amount),
        )
        for key, call_type, amount in (
            ("first", "acompletion", "0.000000000000000001"),
            ("second", "acompletion", "2.5"),
            ("listing", "/v1/models", "9"),
        )
    )
    imported: Final = gateway.import_batch(
        GatewayBatch(
            scan=GatewayScan(
                scan_id=uuid4().hex,
                source_id=source,
                expected_sequence=0,
                checked_at=END + timedelta(days=1),
                state="ok",
                available_rows=len(records),
                scanned_rows=len(records),
                coverage_start=START,
            ),
            records=records,
        )
    )
    if imported.status != "applied":
        raise RuntimeError("Browser gateway fixture import failed")


def main() -> None:
    dsn: Final = os.environ["CUBE_CONTROL_TEST_DATABASE_URL"]
    repository: Final = PostgresRepository(dsn)
    with repository.connect() as connection:
        row: Final = connection.execute("SELECT current_database()").fetchone()
        if row is None or row[0] != "cubecontrol_test":
            raise RuntimeError("Browser tests require the dedicated test database")
    repository.initialize()
    gateway: Final = GatewayRepository(dsn)
    token: Final = uuid4().hex
    source: Final = "browser-reconcile-" + token
    account: Final = Account(id="ui-reconcile-" + token, provider="reconcile-fixture", name="Abgleich", kind="api")
    remove_fixtures(repository, source, account.id)
    server: Final = uvicorn.Server(
        uvicorn.Config(create_app(repository, gateway_reader=gateway), log_level="error", access_log=False)
    )
    proof: Final = Path(os.environ.get("CUBE_CONTROL_BROWSER_PROOF", "/tmp/cubellm-reconciliation-proof"))
    proof.mkdir(parents=True, exist_ok=True)
    try:
        store_month(repository, gateway, source, account)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            thread: Final = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser: Final = playwright.chromium.launch(
                        headless=True, executable_path=os.environ.get("CUBE_CONTROL_BROWSER_EXECUTABLE")
                    )
                    page: Final = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.goto(f"http://127.0.0.1:{listener.getsockname()[1]}")
                    expect(page.locator("#reconcile-status")).to_have_text("Monat auswählen und Abgleich laden.")
                    page.wait_for_load_state("networkidle")
                    if page.evaluate(
                        "performance.getEntriesByType('resource').some(entry => entry.name.includes('/reconciliation?'))"
                    ):
                        raise AssertionError("Reconciliation loaded before it was requested")
                    page.locator("#reconcile-period").fill("2026-09")
                    page.get_by_role("button", name="Abgleich laden", exact=True).click()
                    expect(page.locator("#reconcile-status")).to_contain_text(" Konten passen, ")
                    if (
                        page.locator("#reconcile-rows tr").count() > 50
                        or page.locator("#reconcile-scans li").count() > 50
                    ):
                        raise AssertionError("Reconciliation view renders more than one bounded page")
                    page.locator("#reconcile-search").fill(token)
                    expect(page.locator("#reconcile-status")).to_contain_text("1 von ")
                    expect(page.locator("#reconcile-sources")).to_contain_text("1 von ")
                    expect(page.locator("#reconcile-sources")).to_contain_text(
                        "Quellen nicht vollständig (gilt für alle Konten)"
                    )
                    own: Final = page.locator("#reconcile-rows tr").filter(has_text=account.id)
                    expect(own).to_have_count(1)
                    expect(own).to_contain_text("1.499999999999999999 USD")
                    expect(own).to_contain_text("Anbieter höher")
                    expect(own).to_contain_text("Anbieter-API: 2.500000000000000001 USD aus 2 Aufrufen")
                    expect(own).to_contain_text("Gateway-Verwaltung: 1 Aufrufe, 9 USD (nicht verglichen)")
                    expect(own).to_contain_text("finale Abrechnung")
                    expect(page.locator("#reconcile-scans")).to_contain_text(f"{source}: vollständig")
                    own.locator("summary").click()
                    expect(own.locator("pre")).to_contain_text("Quelle import")
                    expect(own.locator("pre")).to_contain_text("Status bestätigt")
                    expect(own.locator("pre")).to_contain_text("abgeschlossene Abrechnung (altert nicht)")
                    page.locator("#reconcile-heading").locator("..").screenshot(path=str(proof / "desktop.png"))
                    page.locator("#reconcile-period").fill("2026-11")
                    page.get_by_role("button", name="Abgleich laden", exact=True).click()
                    current: Final = page.locator("#reconcile-rows tr").filter(has_text=account.id)
                    expect(current).to_contain_text("Monat noch nicht abgeschlossen (Zwischenstand)")
                    expect(current).to_contain_text("Keine Anbieterkosten erfasst")
                    expect(current).to_contain_text("Nicht vergleichbar")
                    page.set_viewport_size({"width": 390, "height": 844})
                    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
                        raise AssertionError("Reconciliation overflows mobile viewport")
                    page.locator("#reconcile-heading").locator("..").screenshot(path=str(proof / "mobile.png"))
                    page.route("**/api/v1/reconciliation?*", lambda route: route.fulfill(status=503, body="down"))
                    page.get_by_role("button", name="Abgleich laden", exact=True).click()
                    expect(page.locator("#reconcile-status")).to_contain_text("letzten erfolgreichen Abruf")
                    expect(current).to_have_count(1)
                    page.locator("#reconcile-search").fill("")
                    page.locator("#reconcile-search").fill(token)
                    expect(page.locator("#reconcile-status")).to_contain_text("Letzter Abruf fehlgeschlagen")
                    expect(page.locator("#reconcile-status")).to_contain_text("Stand: 2026-11 (UTC)")
                    expect(current).to_have_count(1)
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError("Local browser test server did not stop")
    finally:
        remove_fixtures(repository, source, account.id)
    print("Browser checks passed: lazy load, exact delta, traffic split, coverage, open month, 390px and outage")


if __name__ == "__main__":
    main()
