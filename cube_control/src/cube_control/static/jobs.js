"use strict";
const jobStates = {reserved:"Reserviert", submitted:"Eingereicht", finalized:"Abgeschlossen", failed:"Fehlgeschlagen", expired:"Verfallen"};
const jobAttention = {
 provider_status_unverified: "Frist abgelaufen: Status beim Anbieter prüfen, Reservierung bleibt gehalten",
 reservation_expired: "Reservierung abgelaufen, nicht eingereicht"
};

function jobTime(value) { const time = new Date(value); return Number.isFinite(time.getTime()) ? time.toISOString().slice(0, 16).replace("T", " ") : "Zeit unbekannt"; }
function jobAmount(value, unit) { return value === null || value === undefined ? "unbekannt" : `${value} ${unit}`; }

function jobRow(view) {
 const job = view.job, row = document.createElement("tr");
 const cells = [
  `${job.account_id}\n${job.job_id}`,
  `${jobStates[job.state] || "Unbekannt"} · Version ${view.version}`,
  jobAmount(job.reserved_amount, job.unit),
  job.state === "finalized" ? jobAmount(job.actual_amount, job.unit)
   : job.state === "reserved" || job.state === "submitted" ? "noch offen" : "kein Verbrauch erfasst",
  jobTime(job.expires_at),
  jobTime(job.updated_at),
  view.attention ? jobAttention[view.attention] || "Prüfen" : ""
 ];
 for (const text of cells) { const cell = document.createElement("td"); cell.textContent = text; row.append(cell); }
 if (view.attention) row.classList.add("attention");
 return row;
}

function jobSummary(data) {
 const open = data.jobs.filter(view => view.attention).length;
 if (!data.jobs.length) return "Keine Aufträge erfasst.";
 return `${data.jobs.length}${data.truncated ? "+" : ""} Aufträge, Stand ${jobTime(data.checked_at)} UTC` + (open ? `, ${open} mit Handlungsbedarf.` : ", keiner mit Handlungsbedarf.");
}

async function loadJobs(event) {
 if (event) event.preventDefault();
 const status = document.getElementById("jobs-status"), rows = document.getElementById("jobs-rows");
 const account = document.getElementById("jobs-account").value.trim();
 const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 20000);
 status.textContent = "Aufträge werden geladen …";
 try {
  const response = await fetch(`/api/v1/jobs?limit=200${account ? `&account_id=${encodeURIComponent(account)}` : ""}`, {cache:"no-store", signal:controller.signal});
  if (!response.ok) throw new Error("unavailable");
  const data = await response.json();
  rows.replaceChildren(...data.jobs.map(jobRow));
  status.textContent = jobSummary(data);
 } catch (_) {
  rows.replaceChildren();
  status.textContent = "Aufträge nicht verfügbar; es werden keine alten Werte als aktuell gezeigt.";
 } finally { clearTimeout(timeout); }
}

if (typeof document !== "undefined" && document.getElementById("jobs-filters")) {
 document.getElementById("jobs-filters").addEventListener("submit", loadJobs);
 loadJobs();
}
if (typeof module !== "undefined") module.exports = {jobRow, jobSummary, jobAttention};
