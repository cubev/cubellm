import json
import os
import socket
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from time import monotonic
from typing import Final
from uuid import uuid4

import test_credential_inventory as credential_fixtures
import uvicorn
from inventory_fixtures import bound, gateway_inventory, snapshot
from playwright.sync_api import Page, Route, expect, sync_playwright
from pydantic import SecretStr, TypeAdapter

from cube_control.api import create_app
from cube_control.browser_session import private_authority
from cube_control.credential_binding import CredentialBindingCommand
from cube_control.credential_inventory import public_reference
from cube_control.gateway_inventory import GatewayInventoryAttempt, InventoryCredential
from cube_control.models import Account, ImportBatch, ManualChange, ManualCommand
from cube_control.store import PostgresRepository

TOKEN: Final = "synthetic-browser-token-" + uuid4().hex
OPERATOR: Final = "ui-operator"
JSON_OBJECT: Final = TypeAdapter(dict[str, object])
ACCOUNT_LIST: Final = TypeAdapter(list[dict[str, object]])
TEXT: Final = TypeAdapter(str)
WHOLE: Final = TypeAdapter(int)
FLAG: Final = TypeAdapter(bool)
SOCKET_ADDRESS: Final = TypeAdapter(tuple[str, int])


def settled(page: Page, text: str) -> None:
    expect(page.locator("#management-status")).to_contain_text(text)
    expect(page.locator("#refresh")).to_be_enabled()


def held_routes(page: Page, held: list[Route], count: int) -> None:
    deadline: Final = monotonic() + 15
    while len(held) < count and monotonic() < deadline:
        page.wait_for_timeout(50)
    if len(held) < count:
        for route in held:
            route.abort()
        raise AssertionError(f"expected {count} held requests, saw {len(held)}")


def stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=15)
    if thread.is_alive():
        raise RuntimeError("browser test server did not stop")


def main() -> None:
    expect.set_options(timeout=30000)
    dsn: Final = os.environ["CUBE_CONTROL_TEST_DATABASE_URL"]
    repository: Final = PostgresRepository(dsn)
    with repository.connect() as connection:
        row: Final = connection.execute("SELECT current_database()").fetchone()
        if row is None or row[0] != "cubecontrol_test":
            raise RuntimeError("Browser tests require the dedicated test database")
    repository.initialize()
    marker: Final = uuid4().hex
    managed: Final = Account(
        id=f"ui-managed-{marker}",
        provider="openai",
        name=f"Managed <img src=x onerror=window.unsafe=1> {marker}",
        kind="api",
    )
    discovered: Final = f"ui-discovered-{marker}"
    collision: Final = Account(
        id=f"binding:{public_reference(f'ui-credentials-{marker}', credential_fixtures.COIN_KEY)}",
        provider="custom-collision-api",
        name=f"Collision {marker}",
        kind="api",
    )
    constructor_owned: Final = all(item.id != "constructor" for item in repository.state().accounts)
    constructor_account: Final = Account(
        id="constructor", provider="custom-probe-api", name=f"Constructor probe {marker}", kind="api"
    )
    gateway: Final = f"http://ui-gateway-{marker}.test"
    credential_source: Final = f"ui-credentials-{marker}"
    orphan_source: Final = f"ui-orphans-{marker}"
    coin_reference: Final = public_reference(credential_source, credential_fixtures.COIN_KEY)
    orphan_reference: Final = public_reference(orphan_source, credential_fixtures.OPENAI_KEY)
    proof: Final = Path(os.environ.get("CUBE_CONTROL_BROWSER_PROOF", f"/tmp/cubellm-ui-management-{marker}"))
    proof.mkdir(parents=True, exist_ok=False)

    def forget() -> None:
        with repository.connect() as connection:
            connection.execute("DELETE FROM cube_control.gateway_inventories WHERE gateway_origin=%s", (gateway,))
            for reference in (coin_reference, orphan_reference):
                connection.execute(
                    "DELETE FROM cube_control.credential_binding_commands WHERE reference=%s", (reference,)
                )
            for source in (credential_source, orphan_source):
                connection.execute("DELETE FROM cube_control.credential_inventories WHERE source_id=%s", (source,))
            for account_id in (managed.id, discovered, collision.id, *(("constructor",) if constructor_owned else ())):
                connection.execute("DELETE FROM cube_control.manual_commands WHERE account_id=%s", (account_id,))
                connection.execute("DELETE FROM cube_control.accounts WHERE id=%s", (account_id,))

    with ExitStack() as cleanup:
        listener: Final = cleanup.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        _, port = SOCKET_ADDRESS.validate_python(listener.getsockname())
        origin: Final = f"http://127.0.0.1:{port}"
        authority: Final = private_authority(SecretStr(TOKEN), origin, OPERATOR)
        assert authority is not None
        server: Final = uvicorn.Server(
            uvicorn.Config(create_app(repository, authority=authority), log_level="error", access_log=False)
        )
        cleanup.callback(forget)
        with ExitStack() as running:
            if not repository.import_batch(
                ImportBatch(accounts=(managed, collision, *((constructor_account,) if constructor_owned else ())))
            ).ok:
                raise AssertionError("fixture import failed")
            first: Final = datetime.now(UTC) - timedelta(minutes=20)
            later: Final = datetime.now(UTC) - timedelta(minutes=1)
            for observed, inventory in (
                (
                    first,
                    gateway_inventory(
                        first,
                        credentials=(
                            bound("database", f"kept-{marker}", discovered),
                            InventoryCredential(origin="runtime", credential_name=f"loose-{marker}", binding="unbound"),
                        ),
                    ),
                ),
                (
                    later,
                    gateway_inventory(
                        later,
                        credentials=(
                            InventoryCredential(origin="runtime", credential_name=f"loose-{marker}", binding="unbound"),
                        ),
                        unavailable=frozenset({("credentials", "database")}),
                    ),
                ),
            ):
                repository.record_gateway_inventory(
                    gateway,
                    GatewayInventoryAttempt(attempted_at=observed, status="collected", message="collected"),
                    snapshot(gateway, observed, inventory),
                )
            for attempted, raw in (
                (
                    datetime.now(UTC) - timedelta(minutes=5),
                    credential_fixtures.standard(scanned=datetime.now(UTC) - timedelta(days=9)),
                ),
                (
                    datetime.now(UTC) - timedelta(minutes=2),
                    credential_fixtures.sources([], [], unreadable=2, scanned=datetime.now(UTC) - timedelta(days=8)),
                ),
            ):
                imported = repository.record_credential_import(
                    credential_source, attempted, credential_fixtures.parsed(raw, credential_source)
                )
                assert imported == "imported", imported
            failed_import: Final = repository.record_credential_import(
                credential_source, datetime.now(UTC), ("unreadable", "Credential inventory files are not readable")
            )
            assert failed_import == "unreadable", failed_import
            orphan_scan: Final = datetime.now(UTC) - timedelta(days=2)
            assert (
                repository.record_credential_import(
                    orphan_source,
                    orphan_scan,
                    credential_fixtures.parsed(credential_fixtures.standard(scanned=orphan_scan), orphan_source),
                )
                == "imported"
            )
            orphaned: Final = repository.bind_credential(
                CredentialBindingCommand(
                    command_id=f"orphan-{marker}",
                    reference=orphan_reference,
                    expected_version=0,
                    action="bind",
                    account_id=managed.id,
                ),
                "fixture-operator",
                datetime.now(UTC),
            )
            assert orphaned.status == "applied", orphaned
            cleared_scan: Final = datetime.now(UTC) - timedelta(days=1)
            assert (
                repository.record_credential_import(
                    orphan_source,
                    datetime.now(UTC),
                    credential_fixtures.parsed(
                        credential_fixtures.sources([], [], scanned=cleared_scan), orphan_source
                    ),
                )
                == "imported"
            )
            thread: Final = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            running.callback(stop, server, thread)
            with sync_playwright() as playwright:
                browser: Final = playwright.chromium.launch(
                    headless=True, executable_path=os.environ.get("CUBE_CONTROL_BROWSER_EXECUTABLE")
                )
                context: Final = browser.new_context(viewport={"width": 1280, "height": 900})
                page: Final = context.new_page()
                page.goto(origin)
                expect(page.locator("#management-session")).to_contain_text("Verwaltungstoken")
                commands: Final = f"{origin}/api/v1/accounts/{managed.id}/commands"
                anonymous: Final = context.request.post(
                    commands, data="{}", headers={"Origin": origin, "Content-Type": "application/json"}
                )
                assert anonymous.status == 401, anonymous.status

                page.locator("#management-token").fill("wrong-synthetic-token")
                page.get_by_role("button", name="Anmelden").click()
                settled(page, "Anmeldung abgelehnt")
                page.locator("#management-token").fill(TOKEN)
                page.get_by_role("button", name="Anmelden").click()
                expect(page.locator("#management-session")).to_contain_text(f"Angemeldet als {OPERATOR}")
                csrf: Final = JSON_OBJECT.validate_json(context.request.get(f"{origin}/api/v1/session").body())[
                    "csrf_token"
                ]
                assert isinstance(csrf, str)
                cross: Final = context.request.post(
                    commands,
                    data="{}",
                    headers={"Origin": "http://evil.example", "Content-Type": "application/json", "X-CSRF-Token": csrf},
                )
                no_csrf: Final = context.request.post(
                    commands, data="{}", headers={"Origin": origin, "Content-Type": "application/json"}
                )
                assert (cross.status, no_csrf.status) == (403, 403), (cross.status, no_csrf.status)

                inventory_box: Final = page.locator("#inventory")
                discovered_card: Final = inventory_box.locator(".inventory-account").filter(has_text=discovered)
                expect(discovered_card).to_contain_text("aus früherem Scan, zuletzt gelesen vor 20 Min")
                own_gateway: Final = inventory_box.locator(".inventory-gateway").filter(has_text=gateway)
                expect(own_gateway).to_contain_text("credentials (database): nicht lesbar")
                expect(inventory_box.locator(".findings")).to_contain_text(f"Zugang ohne Kontobindung · {gateway}")
                expect(inventory_box.locator(".findings")).to_contain_text(f"loose-{marker}")
                print(
                    "page dimensions",
                    TEXT.validate_python(
                        page.evaluate(
                            "JSON.stringify({document: document.documentElement.scrollHeight,"
                            " inventory: document.querySelector('.inventory-section').scrollHeight,"
                            " accounts: document.querySelectorAll('.inventory-account').length})"
                        )
                    ),
                    flush=True,
                )
                discovered_card.screenshot(path=str(proof / "01-discovered-retained.png"))
                credential_card: Final = inventory_box.locator(".credential-source").filter(has_text=credential_source)
                expect(credential_card).to_contain_text("(veraltet)")
                expect(credential_card).to_contain_text("Dateien nicht lesbar")
                retained_cards: Final = inventory_box.locator(".credential-candidate").filter(
                    has_text="aus früherem Scan, zuletzt gesehen vor 9 Tagen"
                )
                expect(retained_cards).to_have_count(5)
                expect(inventory_box).to_contain_text("Gateway: ")
                expect(inventory_box).to_contain_text("Keine bestätigte Zuordnung zu Inventurfunden")
                expect(inventory_box).to_contain_text("Art: API-Schlüssel oder Token")
                expect(credential_card).to_contain_text("Umfang unvollständig")
                expect(credential_card).to_contain_text("davon 5 nur aus früherem Scan")
                expect(inventory_box).to_contain_text("Gefundene Zugangsdaten")
                coin_card: Final = inventory_box.locator(".credential-candidate").filter(
                    has_text=public_reference(credential_source, credential_fixtures.COIN_KEY)
                )
                expect(coin_card).to_contain_text("Ungebunden · kein Anbieterhinweis")
                expect(coin_card).to_contain_text("aus früherem Scan, zuletzt gesehen")
                page_text: Final = page.locator("body").inner_text()
                assert not any(
                    value in page_text
                    for value in (
                        credential_fixtures.OPENAI_KEY,
                        credential_fixtures.COIN_KEY,
                        credential_fixtures.SECRET_PATH,
                        credential_fixtures.ROOT,
                    )
                ), "private credential provenance reached the page"
                coin_card.screenshot(path=str(proof / "06-credential-candidate.png"))

                orphan_card: Final = inventory_box.locator(".credential-orphan").filter(has_text=orphan_reference)
                expect(orphan_card).to_contain_text(f"Zugeordnet zu {managed.id} (Stand 1)")
                expect(orphan_card).to_contain_text(f"Herkunft {orphan_source}")
                expect(orphan_card).to_contain_text("Fund im letzten Scan nicht mehr enthalten")
                expect(page.locator(".binding-form")).to_have_count(0)
                orphan_card.get_by_role("button", name="Umhängen …").click()
                orphan_card.locator(".binding-form input[type=text]").fill("openai-api")
                orphan_card.locator(".binding-form input[type=checkbox]").check()
                orphan_card.locator(".binding-form button[type=submit]").click()
                settled(page, "Gespeichert (Stand 2)")
                moved_orphan: Final = inventory_box.locator(".credential-orphan").filter(has_text=orphan_reference)
                expect(moved_orphan).to_contain_text("Zugeordnet zu openai-api (Stand 2)")
                expect(moved_orphan).to_contain_text(f"Herkunft {orphan_source}")
                expect(page.locator(".binding-form")).to_have_count(0)

                def binding_form(account: str) -> None:
                    card = inventory_box.locator(".credential-candidate").filter(has_text=coin_reference)
                    card.get_by_role("button", name="Zuordnen …").or_(
                        card.get_by_role("button", name="Umhängen …")
                    ).click()
                    expect(page.locator(".binding-form")).to_have_count(1)
                    card.locator(".binding-form input[type=text]").fill(account)
                    card.locator(".binding-form input[type=checkbox]").check()
                    card.locator(".binding-form button[type=submit]").click()

                legacy_command: Final[dict[str, object]] = {
                    "schema_version": 1,
                    "command_id": f"legacy-{marker}",
                    "account_id": collision.id,
                    "expected_version": 0,
                    "changes": [{"field": "note", "action": "set", "value": "legacy pending"}],
                }
                committed_legacy: Final = repository.manage(
                    ManualCommand.model_validate(legacy_command), OPERATOR, datetime.now(UTC)
                )
                assert committed_legacy.status == "applied", committed_legacy
                page.evaluate(
                    "([key, command]) => sessionStorage.setItem('cube-control-pending-commands', JSON.stringify({[key]: command}))",
                    [collision.id, legacy_command],
                )
                page.reload()
                expect(page.locator("#management-pending")).to_contain_text(
                    f"Befehl legacy-{marker} für {collision.id}: Ergebnis unbekannt"
                )
                binding_form(managed.id)
                settled(page, "Gespeichert (Stand 1)")
                page.get_by_role("button", name="Denselben Befehl erneut senden").click()
                settled(page, "Bereits gespeichert (Stand 1)")
                assert [
                    entry.revision
                    for entry in repository.state().manual_entries
                    if entry.command.account_id == collision.id
                ] == [1]
                expect(page.locator("#management-pending")).to_be_empty()
                if constructor_owned:
                    constructor_card = page.locator("#accounts .card").filter(has_text=f"Constructor probe {marker}")
                    constructor_card.get_by_role("button", name="Bearbeiten").click()
                    expect(constructor_card.locator("form.edit-form")).to_be_visible()
                    constructor_card.get_by_role("button", name="Bearbeiten").click()
                coin_bound: Final = inventory_box.locator(".credential-candidate").filter(has_text=coin_reference)
                expect(coin_bound).to_contain_text(f"Vom Bediener zugeordnet zu {managed.id} (Stand 1")
                expect(coin_bound).to_contain_text("Gültigkeit und Anbieterzugehörigkeit ungeprüft")
                outside_binding: Final = repository.bind_credential(
                    CredentialBindingCommand(
                        command_id=f"outside-binding-{marker}",
                        reference=coin_reference,
                        expected_version=1,
                        action="bind",
                        account_id="openai-api",
                    ),
                    "another-operator",
                    datetime.now(UTC),
                )
                assert outside_binding.status == "applied", outside_binding
                binding_form("anthropic-api")
                settled(page, "Zwischenzeitlich geändert (Stand 2)")

                def commit_then_drop(route: Route) -> None:
                    _ = route.fetch()
                    route.abort()

                page.route("**/api/v1/credential-bindings/*/commands", commit_then_drop)
                binding_form(managed.id)
                expect(page.locator("#management-status")).to_contain_text("Ergebnis unbekannt")
                page.unroute("**/api/v1/credential-bindings/*/commands")
                expect(page.locator("#management-pending")).to_contain_text(
                    f"Fund {coin_reference}: Zuordnung zu {managed.id}"
                )
                locked_card: Final = inventory_box.locator(".credential-candidate").filter(has_text=coin_reference)
                expect(locked_card.locator(".binding-form input[type=text]")).to_be_disabled()
                expect(locked_card.locator(".binding-form input[type=text]")).to_have_value(managed.id)
                locked_card.get_by_role("button", name="Umhängen …").click()
                expect(page.locator("#management-status")).to_contain_text("Befehl mit unbekanntem Ergebnis offen")
                expect(page.locator(".binding-form")).to_have_count(1)
                expect(locked_card.locator(".binding-form button[type=submit]")).to_be_disabled()
                pending_binding: Final = TEXT.validate_python(
                    page.evaluate("sessionStorage.getItem('cube-control-pending-commands') || ''")
                )
                page.reload()
                expect(page.locator("#management-pending")).to_contain_text(
                    f"Fund {coin_reference}: Zuordnung zu {managed.id}"
                )
                inventory_box.locator(".credential-candidate").filter(has_text=coin_reference).get_by_role(
                    "button", name="Umhängen …"
                ).click()
                expect(page.locator("#management-status")).to_contain_text("Befehl mit unbekanntem Ergebnis offen")
                expect(page.locator(".binding-form")).to_have_count(0)
                assert (
                    TEXT.validate_python(page.evaluate("sessionStorage.getItem('cube-control-pending-commands') || ''"))
                    == pending_binding
                )
                page.get_by_role("button", name="Abmelden").click()
                settled(page, "Abgemeldet.")
                page.locator("#management-token").fill(TOKEN)
                page.get_by_role("button", name="Anmelden").click()
                expect(page.locator("#management-session")).to_contain_text(f"Angemeldet als {OPERATOR}")
                page.get_by_role("button", name="Denselben Befehl erneut senden").click()
                settled(page, "Bereits gespeichert (Stand 3)")
                binding_journal: Final = [
                    (entry.revision, entry.command.command_id, entry.actor)
                    for entry in repository.state().credential_bindings
                    if entry.command.reference == coin_reference
                ]
                assert [item[0] for item in binding_journal] == [1, 2, 3], binding_journal
                assert binding_journal[-1][1] in pending_binding and binding_journal[-1][2] == OPERATOR, binding_journal

                coin_release: Final = inventory_box.locator(".credential-candidate").filter(has_text=coin_reference)
                coin_release.get_by_role("button", name="Aufheben").click()
                coin_release.get_by_role("button", name="Zuordnung wirklich aufheben?").click()
                settled(page, "Gespeichert (Stand 4)")
                coin_history: Final = inventory_box.locator(".credential-candidate").filter(has_text=coin_reference)
                expect(coin_history).to_contain_text("Ungebunden")
                coin_history.locator(".binding-history summary").click()
                expect(coin_history.locator(".binding-history")).to_contain_text("Zuordnungsverlauf (4 Einträge)")
                expect(coin_history.locator(".binding-history")).to_contain_text(
                    "Stand 2: zugeordnet zu openai-api · another-operator"
                )
                expect(coin_history.locator(".binding-history")).to_contain_text("Stand 4: aufgehoben")
                coin_history.screenshot(path=str(proof / "08-binding-history.png"))

                def record(observed: datetime, unavailable: bool) -> None:
                    repository.record_gateway_inventory(
                        gateway,
                        GatewayInventoryAttempt(attempted_at=observed, status="collected", message="collected"),
                        snapshot(
                            gateway,
                            observed,
                            gateway_inventory(
                                observed,
                                credentials=() if unavailable else (bound("database", f"kept-{marker}", discovered),),
                                unavailable=frozenset({("credentials", "database")}) if unavailable else frozenset(),
                            ),
                        ),
                    )

                record(datetime.now(UTC), unavailable=False)
                page.locator("#refresh").click()
                expect(discovered_card).to_contain_text("im letzten Scan gelesen", timeout=20000)
                expect(own_gateway).to_contain_text("credentials (database): vollständig", timeout=20000)
                timed: Final = context.new_page()
                timed.clock.install()
                timed.goto(origin)
                timed_inventory: Final = timed.locator("#inventory")
                timed_gateway: Final = timed_inventory.locator(".inventory-gateway").filter(has_text=gateway)
                expect(timed_gateway).to_contain_text("credentials (database): vollständig", timeout=20000)
                record(datetime.now(UTC) + timedelta(seconds=1), unavailable=True)
                timed.clock.fast_forward("01:01")
                expect(timed_gateway).to_contain_text("credentials (database): nicht lesbar", timeout=20000)
                expect(timed_inventory.locator(".inventory-account").filter(has_text=discovered)).to_contain_text(
                    "aus früherem Scan, zuletzt gelesen"
                )
                timed_card: Final = timed.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                timed_card.get_by_role("button", name="Bearbeiten").click()
                timed_card.get_by_label("Notiz: Aktion").select_option("set")
                timed_card.locator("label", has_text="Notiz").first.locator("input").fill("scheduled draft")
                record(datetime.now(UTC) + timedelta(seconds=2), unavailable=True)
                timed.clock.fast_forward("01:01")
                expect(timed.locator("#connection")).to_contain_text("Aktualisiert")
                expect(timed_card.locator("label", has_text="Notiz").first.locator("input")).to_have_value(
                    "scheduled draft"
                )
                expect(timed_card.get_by_label("Notiz: Aktion")).to_have_value("set")
                timed.close()
                page.locator("#refresh").click()
                expect(own_gateway).to_contain_text("credentials (database): nicht lesbar", timeout=20000)

                discovered_card.get_by_role("button", name="Konto aufnehmen …").click()
                discovered_card.get_by_label("Anbieter-ID", exact=True).fill("custom-media-api")
                discovered_card.locator("form.enroll-form select").select_option("api")
                discovered_card.get_by_label("Anzeigename", exact=True).fill(f"Discovered {marker}")
                discovered_card.get_by_role("button", name="Konto aufnehmen").click()
                unconfirmed: Final = FLAG.validate_python(
                    discovered_card.locator("form.enroll-form").evaluate("form => form.checkValidity()")
                )
                assert unconfirmed is False, "enrollment form accepted a missing confirmation"
                assert discovered not in [item.id for item in repository.state().accounts]
                discovered_card.locator("input[type=checkbox]").check()
                discovered_card.get_by_role("button", name="Konto aufnehmen").click()
                settled(page, "Gespeichert (Stand 1)")
                expect(page.locator("#accounts")).to_contain_text(f"Discovered {marker}")

                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")

                def edit(changes: tuple[tuple[str, str, str], ...]) -> None:
                    card.get_by_role("button", name="Bearbeiten").click()
                    for label, action, value in changes:
                        card.get_by_label(f"{label}: Aktion").select_option(action)
                        if value:
                            card.locator("label", has_text=label).first.locator("input").fill(value)
                    card.get_by_role("button", name="Speichern").click()

                edit((("Notiz", "set", "browser note"), ("Monatsbudget (USD)", "set", "1234567890123.45")))
                settled(page, "Gespeichert (Stand 1)")
                expect(card).to_contain_text("1234567890123.45")
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                card.get_by_role("button", name="Bearbeiten").click()
                card.get_by_label("Notiz: Aktion").select_option("set")
                card.locator("label", has_text="Notiz").first.locator("input").fill("")
                card.get_by_role("button", name="Speichern").click()
                settled(page, "Gespeichert (Stand 2)")
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                edit((("Notiz", "automatic", ""),))
                settled(page, "Gespeichert (Stand 3)")

                today: Final = datetime.now(UTC)
                previous_month: Final = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
                current_month: Final = today.strftime("%Y-%m")
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                card.get_by_role("button", name="Bearbeiten").click()
                budget_label = card.locator("label", has_text="Monatsbudget (USD)").first
                expect(budget_label).to_contain_text("manuell")
                card.get_by_label("Monat", exact=True).fill(previous_month)
                expect(budget_label).not_to_contain_text("manuell")
                expect(card.get_by_label("Monatsbudget (USD): Aktion").locator("option")).to_have_count(2)
                card.get_by_label("Monatsbudget (USD): Aktion").select_option("set")
                budget_label.locator("input").fill("0.10")
                card.get_by_role("button", name="Speichern").click()
                settled(page, "Gespeichert (Stand 4)")
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                card.get_by_role("button", name="Bearbeiten").click()
                budget_label = card.locator("label", has_text="Monatsbudget (USD)").first
                expect(budget_label.locator("input")).to_have_value("1234567890123.45")
                card.get_by_label("Monat", exact=True).fill(previous_month)
                expect(budget_label).to_contain_text("manuell")
                expect(budget_label.locator("input")).to_have_value("0.10")
                expect(card.get_by_label("Monatsbudget (USD): Aktion").locator("option")).to_have_count(3)
                card.get_by_label("Monat", exact=True).fill(current_month)
                expect(budget_label.locator("input")).to_have_value("1234567890123.45")
                card.get_by_role("button", name="Bearbeiten").click()

                outside: Final = repository.manage(
                    ManualCommand(
                        command_id=f"outside-{marker}",
                        account_id=managed.id,
                        expected_version=4,
                        changes=(ManualChange(field="plan", action="set", value="outside"),),
                    ),
                    "another-operator",
                    datetime.now(UTC),
                )
                assert outside.status == "applied", outside
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                edit((("Tarif", "set", "stale browser"),))
                settled(page, "Zwischenzeitlich geändert (Stand 5)")

                def apply_then_drop(route: Route) -> None:
                    _ = route.fetch()
                    route.abort()

                page.route("**/api/v1/accounts/*/commands", apply_then_drop)
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                edit((("Tarif", "set", "unknown outcome"),))
                settled(page, "Ergebnis unbekannt")
                expect(page.locator("#management-pending")).to_contain_text(managed.id)
                page.unroute("**/api/v1/accounts/*/commands")
                card.get_by_role("button", name="Speichern").click()
                settled(page, "Befehl mit unbekanntem Ergebnis offen")
                page.locator("#management").screenshot(path=str(proof / "02-unknown-outcome.png"))
                page.get_by_role("button", name="Denselben Befehl erneut senden").click()
                settled(page, "Bereits gespeichert (Stand 6)")
                expect(page.locator("#management-pending")).to_be_empty()

                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                card.get_by_role("button", name="Bearbeiten").click()
                card.get_by_label("Tarif: Aktion").select_option("set")
                card.locator("label", has_text="Tarif").first.locator("input").fill("double click")
                card.get_by_role("button", name="Speichern").dblclick()
                settled(page, "Gespeichert (Stand 7)")
                revisions: Final = [
                    entry.revision
                    for entry in repository.state().manual_entries
                    if entry.command.account_id == managed.id
                ]
                assert revisions == [1, 2, 3, 4, 5, 6, 7], revisions

                def apply_then_corrupt(route: Route) -> None:
                    committed: Final = route.fetch()
                    route.fulfill(status=committed.status, body='{"status": "app', content_type="application/json")

                page.route("**/api/v1/accounts/*/commands", apply_then_corrupt)
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                edit((("Tarif", "set", "corrupted reply"),))
                settled(page, "Antwort nicht eindeutig")
                pending_before: Final = TEXT.validate_python(
                    page.evaluate("sessionStorage.getItem('cube-control-pending-commands') || ''")
                )
                page.unroute("**/api/v1/accounts/*/commands")
                page.reload()
                expect(page.locator("#management-pending")).to_contain_text(managed.id)
                assert (
                    TEXT.validate_python(page.evaluate("sessionStorage.getItem('cube-control-pending-commands') || ''"))
                    == pending_before
                )
                page.get_by_role("button", name="Denselben Befehl erneut senden").click()
                settled(page, "Bereits gespeichert (Stand 8)")
                journal: Final = [
                    (entry.revision, entry.command.command_id)
                    for entry in repository.state().manual_entries
                    if entry.command.account_id == managed.id
                ]
                assert [revision for revision, _ in journal] == [1, 2, 3, 4, 5, 6, 7, 8], journal
                assert journal[-1][1] in pending_before, (journal[-1], pending_before)

                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                card.get_by_role("button", name="Bearbeiten").click()
                card.get_by_label("Notiz: Aktion").select_option("set")
                card.locator("label", has_text="Notiz").first.locator("input").fill("unsaved draft")
                page.locator("#refresh").click()
                expect(page.locator("#refresh")).to_be_enabled()
                expect(card.locator("label", has_text="Notiz").first.locator("input")).to_have_value("unsaved draft")
                outside_again: Final = repository.manage(
                    ManualCommand(
                        command_id=f"outside-again-{marker}",
                        account_id=managed.id,
                        expected_version=8,
                        changes=(ManualChange(field="plan", action="set", value="outside again"),),
                    ),
                    "another-operator",
                    datetime.now(UTC),
                )
                assert outside_again.status == "applied", outside_again
                page.locator("#refresh").click()
                expect(card).to_contain_text("Entwurf bezieht sich auf Stand 8; inzwischen Stand 9")
                expect(card.locator("label", has_text="Notiz").first.locator("input")).to_have_value("unsaved draft")
                card.get_by_role("button", name="Speichern").click()
                settled(page, "Zwischenzeitlich geändert (Stand 9)")
                managed_changes: Final = chain.from_iterable(
                    entry.command.changes
                    for entry in repository.state().manual_entries
                    if entry.command.account_id == managed.id
                )
                latest_plan: Final = [change.value for change in managed_changes if change.field == "plan"]
                assert latest_plan[-1] == "outside again", latest_plan

                held_summaries: Final[list[Route]] = []
                page.route("**/api/v1/summary", lambda route: held_summaries.append(route))
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                card.get_by_role("button", name="Bearbeiten").click()
                card.get_by_label("Tarif: Aktion").select_option("set")
                card.locator("label", has_text="Tarif").first.locator("input").fill("before delayed refresh")
                card.get_by_role("button", name="Speichern").click()
                expect(page.locator("#management-status")).to_contain_text("Gespeichert (Stand 10)")
                held_routes(page, held_summaries, 1)
                card.get_by_role("button", name="Bearbeiten").click()
                card.get_by_role("button", name="Bearbeiten").click()
                expect(card.locator("form.edit-form")).to_have_count(0)
                expect(page.locator("#management-status")).to_contain_text("noch aktualisiert")
                for route in held_summaries:
                    route.continue_()
                page.unroute("**/api/v1/summary")
                expect(page.locator("#refresh")).to_be_enabled()
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                edit((("Tarif", "set", "after delayed refresh"),))
                expect(card.locator("form.edit-form")).to_have_count(0)
                settled(page, "Gespeichert (Stand 11)")

                page.route("**/api/v1/summary", lambda route: route.abort())
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                edit((("Tarif", "set", "before failed refresh"),))
                settled(page, "Gespeichert (Stand 12). Die Ansicht ist noch nicht aktualisiert")
                expect(card.get_by_role("button", name="Wird aktualisiert …")).to_be_disabled()
                page.unroute("**/api/v1/summary")
                page.locator("#refresh").click()
                expect(page.locator("#refresh")).to_be_enabled()
                card = page.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed")
                opener: Final = card.get_by_role("button", name="Bearbeiten")
                opener.click()
                expect(card.locator("form.edit-form")).to_be_visible()
                opener.click()
                expect(card.locator("form.edit-form")).to_be_hidden()
                opener.click()
                expect(card.locator("form.edit-form")).to_be_visible()
                expect(card.locator("form.edit-form")).to_have_count(1)
                final_revisions: Final = [
                    entry.revision
                    for entry in repository.state().manual_entries
                    if entry.command.account_id == managed.id
                ]
                assert final_revisions == list(range(1, 13)), final_revisions
                if page.evaluate("Boolean(window.unsafe)"):
                    raise AssertionError("Untrusted account name executed")

                held: Final[list[Route]] = []
                page.route("**/api/v1/inventory", lambda route: held.append(route))
                page.evaluate("void (refresh(), refresh())")
                deadline: Final = monotonic() + 15
                while len(held) < 2 and monotonic() < deadline:
                    page.wait_for_timeout(50)
                if len(held) < 2:
                    for route in held:
                        route.abort()
                    raise AssertionError(f"expected two concurrent inventory requests, saw {len(held)}")
                served: Final = JSON_OBJECT.validate_json(held[1].fetch().body())
                served_accounts: Final = ACCOUNT_LIST.validate_python(served["accounts"])

                def with_account(account_id: str) -> str:
                    return json.dumps(
                        {**served, "accounts": [*served_accounts, {**served_accounts[0], "account_id": account_id}]}
                    )

                held[1].fulfill(body=with_account(f"fresh-{marker}"), content_type="application/json")
                expect(inventory_box).to_contain_text(f"fresh-{marker}")
                held[0].fulfill(body=with_account(f"stale-{marker}"), content_type="application/json")
                page.wait_for_timeout(1000)
                expect(inventory_box).to_contain_text(f"fresh-{marker}")
                expect(inventory_box).not_to_contain_text(f"stale-{marker}")
                page.unroute("**/api/v1/inventory")
                page.route("**/api/v1/inventory", lambda route: route.abort())
                page.evaluate("void refresh()")
                expect(inventory_box).to_contain_text("Inventar konnte nicht aktualisiert werden. Angezeigt: Stand")
                expect(inventory_box).to_contain_text(discovered)
                page.unroute("**/api/v1/inventory")

                copied: Final = next(
                    cookie.get("value", "")
                    for cookie in context.cookies()
                    if cookie.get("name") == "cube_control_session"
                )
                page.route(
                    "**/api/v1/session",
                    lambda route: route.abort() if route.request.method == "DELETE" else route.continue_(),
                )
                page.get_by_role("button", name="Abmelden").click()
                settled(page, "Abmeldung nicht bestätigt")
                expect(page.locator("#management-session")).to_contain_text("Angemeldet als")
                page.unroute("**/api/v1/session")
                page.get_by_role("button", name="Abmelden").click()
                settled(page, "Abgemeldet.")
                replayed: Final = context.request.get(
                    f"{origin}/api/v1/session", headers={"Cookie": f"cube_control_session={copied}"}
                )
                assert replayed.status == 401, replayed.status

                mobile: Final = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
                phone: Final = mobile.new_page()
                phone.goto(origin)
                phone.locator("#management-token").fill(TOKEN)
                phone.get_by_role("button", name="Anmelden").click()
                expect(phone.locator("#management-session")).to_contain_text(f"Angemeldet als {OPERATOR}")
                expect(phone.locator("#inventory")).to_contain_text(discovered)
                overflow: Final = WHOLE.validate_python(
                    phone.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                )
                assert overflow <= 1, overflow
                phone.locator("#management").screenshot(path=str(proof / "03-mobile-management.png"))
                phone.locator(".inventory-account").filter(has_text=discovered).screenshot(
                    path=str(proof / "04-mobile-discovered.png")
                )
                phone.locator("#accounts .card").filter(has_text=marker).filter(has_text="Managed").screenshot(
                    path=str(proof / "05-mobile-managed-card.png")
                )
                phone_candidate: Final = phone.locator(".credential-candidate").filter(
                    has_text=public_reference(credential_source, credential_fixtures.COIN_KEY)
                )
                expect(phone_candidate).to_be_visible()
                phone_candidate.screenshot(path=str(proof / "07-mobile-credential-candidate.png"))
                phone_orphan: Final = phone.locator(".credential-orphan").filter(has_text=orphan_reference)
                expect(phone_orphan).to_contain_text(f"Herkunft {orphan_source}")
                phone_orphan.screenshot(path=str(proof / "09-mobile-binding-orphan.png"))
                browser.close()
        print("browser management acceptance passed")


if __name__ == "__main__":
    main()
