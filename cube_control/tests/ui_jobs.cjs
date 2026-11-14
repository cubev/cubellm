const assert = require('node:assert/strict');
const path = require('node:path');
function element() {
 return {children: [], textContent: '', classes: new Set(),
  append(...items) { this.children.push(...items); }, classList: {add(name) { this.owner.classes.add(name); }}};
}
global.document = {getElementById: () => null, createElement() { const e = element(); e.classList.owner = e; return e; }};
const {jobRow, jobSummary} = require(path.join(__dirname, '../src/cube_control/static/jobs.js'));
const job = {account_id: 'heygen-api', job_id: 'video-1', state: 'submitted', reserved_amount: '1.50', unit: 'credits',
 actual_amount: null, expires_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-13T23:00:00Z'};
const overdue = jobRow({job, version: 2, attention: 'provider_status_unverified'});
assert.deepEqual(overdue.children.map(cell => cell.textContent), ['heygen-api\nvideo-1', 'Eingereicht · Version 2',
 '1.50 credits', 'noch offen', '2026-11-14 00:00', '2026-11-13 23:00',
 'Frist abgelaufen: Status beim Anbieter prüfen, Reservierung bleibt gehalten']);
assert.ok(overdue.classes.has('attention'));
const done = jobRow({job: {...job, state: 'finalized', actual_amount: '1.25'}, version: 3, attention: null});
assert.equal(done.children[3].textContent, '1.25 credits');
assert.equal(done.children[6].textContent, '');
assert.ok(!done.classes.has('attention'));
assert.equal(jobRow({job: {...job, state: 'expired'}, version: 2, attention: null}).children[3].textContent, 'kein Verbrauch erfasst');
const xss = jobRow({job: {...job, job_id: '<img src=x onerror=alert(1)>'}, version: 1, attention: null});
assert.equal(xss.children[0].textContent, 'heygen-api\n<img src=x onerror=alert(1)>');
assert.equal(jobSummary({checked_at: '2026-11-14T00:00:00Z', truncated: true, jobs: [{attention: 'reservation_expired'}, {attention: null}]}),
 '2+ Aufträge, Stand 2026-11-14 00:00 UTC, 1 mit Handlungsbedarf.');
assert.equal(jobSummary({checked_at: '2026-11-14T00:00:00Z', truncated: false, jobs: []}), 'Keine Aufträge erfasst.');
console.log('Job monitoring rendering checks passed');
