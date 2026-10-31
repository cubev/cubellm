"use strict";
let usageCursor = null, usageParameters = null, usageBusy = false;
const usageStates = {succeeded:"Erfolgreich", failed:"Fehlgeschlagen", unknown:"Unbekannt"};
const usageKinds = {provider_request:"Anbieter-API", gateway_management:"Gateway-Verwaltung", unknown:"Aufrufart unbekannt"};

function usageValue(value) { return value === null || value === undefined ? "unbekannt" : String(value); }
function usageTime(value) { const time = new Date(value); return Number.isFinite(time.getTime()) ? time.toISOString() : "Zeit unbekannt"; }
function usageSourceStale(source) { const age = Date.now() - Date.parse(source.scan.checked_at); return source.scan.state !== "ok" || !Number.isFinite(age) || age < -60000 || age > 900000; }

async function usageFetch(url) {
 const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 20000);
 try {
  const response = await fetch(url, {cache:"no-store", signal:controller.signal});
  if (!response.ok) throw new Error(response.status === 409 ? "changed" : "unavailable");
  return await response.json();
 } finally { clearTimeout(timeout); }
}

async function usageHistory(button, output, row) {
 button.disabled = true;
 try {
  const data = await usageFetch(`/api/v1/usage/${encodeURIComponent(row.source_id)}/${encodeURIComponent(row.record.source_key)}/history?limit=200`);
  output.textContent = data.changes.map(change =>
   `Version ${change.sequence}\nGateway-USD: ${usageValue(change.record.amount_usd)}\nInput: ${usageValue(change.record.input_tokens)} · Output: ${usageValue(change.record.output_tokens)}\nStatus: ${usageStates[change.record.status] || "Unbekannt"}\nKonto: ${usageValue(change.record.account_id)} · Anwendung: ${usageValue(change.record.application_id)}\nHash: ${change.content_hash}\nVorher: ${usageValue(change.previous_hash)}`
  ).join("\n\n") + (data.next_after !== null ? "\n\nWeitere Änderungen vorhanden. Der vollständige Verlauf ist über die API abrufbar." : "");
 } catch { output.textContent = "Verlauf nicht erreichbar. Erneut laden."; }
 finally { button.disabled = false; }
}

function usageRow(row) {
 const r = row.record, tr = element("tr");
 const cell = text => { const td = element("td", "", text); tr.append(td); return td; };
 cell(usageTime(r.occurred_at));
 cell(`${r.provider || "Anbieter unbekannt"} / ${r.model}`);
 cell(`${r.account_id || "Konto unbekannt"} / ${r.application_id || "Anwendung unbekannt"}`);
 cell(`${usageStates[r.status] || "Unbekannt"} / ${r.call_type || "Typ unbekannt"} · ${usageKinds[row.traffic_kind] || "Aufrufart unbekannt"}`);
 cell(usageValue(r.amount_usd));
 cell(`${usageValue(r.input_tokens)} / ${usageValue(r.output_tokens)}`);
 const details = element("details"), summary = element("summary", "", "Nachweis anzeigen");
 details.append(summary,
  element("p", "", `Quelle: ${row.source_id}`),
  element("p", "", `Request-Referenz: ${r.source_key}`),
  element("p", "", `Originalzeit: ${r.occurred_at}`),
  element("p", "", `Deployment: ${usageValue(r.deployment_id)}`),
  element("p", "", `Cache gelesen: ${usageValue(r.cache_read_tokens)} · geschrieben: ${usageValue(r.cache_write_tokens)}`),
  element("p", "", `Abschluss: ${usageValue(r.completed_at)} · Dauer: ${usageValue(r.duration_ms)} ms`),
  element("p", "", `Erstmals in Scan ${row.first_sequence} · zuletzt in Scan ${row.last_sequence}`));
 const history = element("pre"), button = element("button", "", "Korrekturverlauf laden");
 button.type = "button";
 button.addEventListener("click", () => usageHistory(button, history, row));
 details.append(button, history); cell().append(details);
 return tr;
}

function renderUsage(data, append) {
 if (!append) $("usage-rows").replaceChildren();
 for (const row of data.rows) $("usage-rows").append(usageRow(row));
 const metrics = data.metrics;
 $("usage-totals").textContent = `${data.total} gespeicherte Aufrufe: ${data.provider_calls} Anbieter-API, ${data.management_calls} Gateway-Verwaltung, ${data.unknown_traffic_calls} Aufrufart unbekannt · Gateway-USD: ${usageValue(metrics.reported_usd)}${metrics.unknown_cost_calls ? ` (Teilbetrag; ${metrics.unknown_cost_calls} Kosten unbekannt)` : ""} · ${data.unknown_account_calls} ohne Kontozuordnung · ${data.unknown_application_calls} ohne Anwendungszuordnung`;
 $("usage-status-message").textContent = `${data.query.period} (UTC) · ${$("usage-rows").children.length} von ${data.total} Aufrufen angezeigt. ${data.sources.some(usageSourceStale) ? "Erfassung unvollständig oder veraltet." : "Gespeicherter Quellbestand; direkte Anbieteraufrufe sind nicht enthalten."}`;
 usageCursor = data.next_cursor;
 $("usage-next").hidden = !usageCursor;
}

async function loadUsage(append = false) {
 if (usageBusy) return;
 usageBusy = true;
 $("usage-search").disabled = true;
 $("usage-next").disabled = true;
 try {
  if (!append) {
   usageParameters = new URLSearchParams({period:$("usage-period").value});
   for (const [field, id] of [["provider","provider"],["account_id","account"],["application_id","application"],["model","model"],["status","status"],["traffic_kind","traffic"]]) {
    const value = $("usage-" + id).value.trim();
    if (value) usageParameters.set(field, value);
   }
  }
  const parameters = new URLSearchParams(usageParameters);
  if (append && usageCursor) parameters.set("cursor", usageCursor);
  $("usage-status-message").textContent = "Aufrufe werden geladen …";
  renderUsage(await usageFetch("/api/v1/usage?" + parameters), append);
 } catch (error) {
  usageCursor = null;
  $("usage-next").hidden = true;
  $("usage-status-message").textContent = error.message === "changed" ? "Die Erfassung hat sich geändert. Aufrufe neu laden; angezeigte Werte stammen vom vorherigen Abruf." : "Aufrufe nicht erreichbar. Bereits angezeigte Werte stammen vom letzten erfolgreichen Abruf.";
 } finally {
  usageBusy = false;
  $("usage-search").disabled = false;
  $("usage-next").disabled = false;
 }
}

$("usage-period").value = new Date().toISOString().slice(0,7);
$("usage-filters").addEventListener("submit", event => { event.preventDefault(); loadUsage(); });
$("usage-next").addEventListener("click", () => loadUsage(true));
