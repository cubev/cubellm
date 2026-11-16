"use strict";
let reconcileBusy = false, reconcileData = null, reconcileFailed = false;
const reconcileFailure = "Letzter Abruf fehlgeschlagen (Abgleich nicht erreichbar oder Monat ungültig). Angezeigte Werte stammen vom letzten erfolgreichen Abruf.";
const reconcileLimit = 50;
const reconcileOutcomes = {
 consistent_with_provider_total:"Vereinbar: Anbietersumme deckt Gateway-Erfassung",
 gateway_exceeds_provider_total:"Auffällig: Gateway-Erfassung über Anbietersumme",
 not_comparable:"Nicht vergleichbar"
};
const reconcileDirections = {equal:"gleich", provider_higher:"Anbieter höher", gateway_higher:"Gateway höher"};
const reconcileReasons = {
 account_not_in_catalogue:"Konto nicht im Katalog",
 month_not_closed:"Monat noch nicht abgeschlossen (Zwischenstand)",
 provider_missing:"Keine Anbieterkosten erfasst",
 provider_not_current:"Anbieterwert nicht aktuell",
 provider_conflict:"Widersprüchliche Anbieterwerte",
 provider_not_provider_reported:"Wert stammt nicht vom Anbieter",
 provider_coverage_unstructured:"Abdeckung des Anbieterwerts unbekannt (Altbestand)",
 provider_coverage_not_final:"Keine finale Monatsabrechnung",
 provider_coverage_other_window:"Anbieterwert deckt anderen Zeitraum ab",
 currency_mismatch:"Andere Währung als USD",
 gateway_not_loaded:"Gateway-Aufrufe für diesen Monat nicht geladen",
 gateway_missing:"Keine Gateway-Aufrufe erfasst",
 gateway_scan_missing:"Keine Gateway-Erfassung vorhanden",
 gateway_scan_incomplete:"Gateway-Erfassung unvollständig oder fehlerhaft",
 gateway_scan_rejected_rows:"Gateway-Zeilen abgewiesen",
 gateway_scan_clock_skew:"Erfassungszeit liegt in der Zukunft",
 gateway_scan_stale:"Gateway-Erfassung veraltet",
 gateway_scan_before_period_end:"Letzte Erfassung vor Monatsende",
 gateway_coverage_unknown:"Erfassungsbeginn der Quelle unbekannt",
 gateway_coverage_after_period_start:"Quelle erfasst erst nach Monatsbeginn",
 gateway_costs_unknown:"Gateway-Kosten teilweise unbekannt",
 gateway_traffic_unknown:"Aufrufart teilweise unbekannt",
 gateway_provider_mismatch:"Aufrufe mit abweichendem Anbieter"
};
const reconcileScans = {
 complete:"vollständig", partial:"teilweise", error:"Fehler", rejected_rows:"Zeilen abgewiesen", clock_skew:"Zeit in der Zukunft",
 stale:"veraltet", before_period_end:"vor Monatsende", coverage_unknown:"Erfassungsbeginn unbekannt"
};

const reconcileStatuses = {known:"bestätigt", manual:"manuell", estimated:"geschätzt", unknown:"unbekannt", error:"Fehler", not_configured:"nicht eingerichtet"};
const reconcileFreshness = {fresh:"aktuell", stale:"veraltet", clock_skew:"Zeit in der Zukunft", expired:"abgelaufen"};
function reconcileAge(item) {
 return item.freshness === "stale" && item.current ? "abgeschlossene Abrechnung (altert nicht)" : reconcileFreshness[item.freshness] || item.freshness;
}
function reconcileValue(value) { return value === null || value === undefined ? "unbekannt" : String(value); }
function reconcileCell(lines) {
 const cell = document.createElement("td");
 for (const line of lines) { const paragraph = document.createElement("p"); paragraph.textContent = line; cell.append(paragraph); }
 return cell;
}
function reconcileCoverage(coverage) {
 if (!coverage) return "Abdeckung: nicht angegeben";
 return `Abdeckung: ${coverage.basis === "final_billing_statement" ? "finale Abrechnung" : "Stand bis heute"} ${coverage.start} bis ${coverage.end}`;
}
function reconcileEvidence(item) {
 return `${reconcileValue(item.value)} ${item.unit || ""} · Quelle ${item.source} · Status ${reconcileStatuses[item.status] || item.status} · beobachtet ${item.observed_at} · ${reconcileAge(item)}${item.current ? "" : " · nicht verwendbar"} · ${reconcileCoverage(item.coverage)}`;
}
function reconcileGateway(gateway) {
 return [
  `Anbieter-API: ${gateway.provider_request_usd} USD aus ${gateway.provider_request_calls} Aufrufen${gateway.unknown_cost_calls ? ` (Teilbetrag; ${gateway.unknown_cost_calls} Kosten unbekannt)` : ""}`,
  `Gateway-Verwaltung: ${gateway.management_calls} Aufrufe, ${gateway.management_usd} USD (nicht verglichen)`,
  `Aufrufart unbekannt: ${gateway.unknown_traffic_calls} Aufrufe, ${gateway.unknown_traffic_usd} USD`,
  `Abweichender Anbieter: ${gateway.provider_mismatch_calls} · Fehlgeschlagen: ${gateway.failures}`,
  `Zeitraum der Aufrufe: ${reconcileValue(gateway.first_call_at)} bis ${reconcileValue(gateway.last_call_at)}`
 ];
}
function reconcileRow(row) {
 const tr = document.createElement("tr");
 const reading = row.provider_reading;
 tr.append(
  reconcileCell([row.account_id, `Anbieter: ${reconcileValue(row.provider)}`]),
  reconcileCell(reading ? [reconcileEvidence(reading)] : ["Kein verwendbarer Anbieterwert"]),
  reconcileCell(reconcileGateway(row.gateway)),
  reconcileCell(row.delta === null ? ["Keine Differenz berechenbar"] : [`${row.delta} USD`, reconcileDirections[row.direction]]),
  reconcileCell([reconcileOutcomes[row.comparison], ...row.reasons.map(reason => reconcileReasons[reason] || reason)])
 );
 const evidence = document.createElement("details"), summary = document.createElement("summary");
 summary.textContent = `${row.provider_evidence.length} Anbieterwerte`;
 const list = document.createElement("pre");
 list.textContent = row.provider_evidence.map(reconcileEvidence).join("\n") || "Keine Anbieterwerte für diesen Monat";
 evidence.append(summary, list);
 const cell = document.createElement("td");
 cell.append(evidence);
 tr.append(cell);
 return tr;
}
function reconcileMatches(text) {
 const query = $("reconcile-search").value.trim().toLowerCase();
 return !query || text.toLowerCase().includes(query);
}
function renderReconciliation() {
 const data = reconcileData;
 if (!data) { $("reconcile-status").textContent = reconcileFailed ? reconcileFailure : "Monat auswählen und Abgleich laden."; return; }
 const rows = data.rows.filter(row => reconcileMatches(`${row.account_id} ${row.provider || ""}`));
 const scans = data.scans.filter(scan => reconcileMatches(scan.source_id));
 $("reconcile-rows").replaceChildren(...rows.slice(0, reconcileLimit).map(reconcileRow));
 $("reconcile-scans").replaceChildren(...scans.slice(0, reconcileLimit).map(scan => {
  const item = document.createElement("li");
  item.textContent = `${scan.source_id}: ${reconcileScans[scan.status] || scan.status} · geprüft ${scan.checked_at} · Erfassung ab ${reconcileValue(scan.coverage_start)} · ${scan.rejected_rows} abgewiesen`;
  return item;
 }));
 const blocked = data.scans.filter(scan => scan.status !== "complete").length;
 $("reconcile-sources").textContent = `${scans.length} von ${data.scans.length} Gateway-Quellen passen, ${Math.min(scans.length, reconcileLimit)} angezeigt · ${blocked} Quellen nicht vollständig (gilt für alle Konten)`;
 $("reconcile-unattributed").textContent = data.unattributed.length
  ? "Ohne Kontozuordnung: " + data.unattributed.map(item => `${item.provider}: Anbieter-API ${item.gateway.provider_request_usd} USD aus ${item.gateway.provider_request_calls} Aufrufen, ${item.gateway.management_calls} Verwaltung, ${item.gateway.unknown_traffic_calls} Aufrufart unbekannt${item.gateway.unknown_cost_calls ? `, ${item.gateway.unknown_cost_calls} Kosten unbekannt` : ""}`).join(" · ")
  : "Keine Gateway-Aufrufe ohne Kontozuordnung";
 $("reconcile-status").textContent = `${reconcileFailed ? reconcileFailure + " Stand: " : ""}${data.period} (UTC) · erstellt ${data.generated_at} · ${rows.length} von ${data.rows.length} Konten passen, ${Math.min(rows.length, reconcileLimit)} angezeigt${rows.length > reconcileLimit ? "; Suche eingrenzen" : ""}${data.gateway_loaded ? "" : " · Gateway-Aufrufe nicht geladen"}`;
}
async function loadReconciliation() {
 if (reconcileBusy) return;
 reconcileBusy = true;
 $("reconcile-load").disabled = true;
 $("reconcile-status").textContent = "Abgleich wird geladen …";
 try {
  reconcileData = await usageFetch("/api/v1/reconciliation?" + new URLSearchParams({period:$("reconcile-period").value}));
  reconcileFailed = false;
  renderReconciliation();
 } catch {
  reconcileFailed = true;
  renderReconciliation();
 } finally {
  reconcileBusy = false;
  $("reconcile-load").disabled = false;
 }
}

$("reconcile-period").value = new Date().toISOString().slice(0,7);
$("reconcile-search").addEventListener("input", renderReconciliation);
$("reconcile-form").addEventListener("submit", event => { event.preventDefault(); loadReconciliation(); });
