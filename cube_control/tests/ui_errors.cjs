const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({
 document: {getElementById: () => ({addEventListener() {}}), querySelectorAll: () => []},
 fetch: () => new Promise(() => {}), setInterval: () => 0
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/cube_control/static/app.js'), 'utf8'), context);
const now = Date.UTC(2026, 0, 1);
const error = {reading: {status: 'error', window: 'all', metric: 'balance', source: 'import',
 observed_at: new Date(now - 10000).toISOString()}};
const good = {reading: {...error.reading, status: 'known', window: 'Gekauft',
 observed_at: new Date(now - 5000).toISOString()}};
assert.equal(context.unresolvedError(error, [error], now), true);
assert.equal(context.unresolvedError(error, [error, good], now), false);
for (const override of [
 {source: 'manual'}, {metric: 'usage'}, {status: 'error'},
 {observed_at: error.reading.observed_at}, {observed_at: 'invalid'},
 {observed_at: new Date(now + 61000).toISOString()}
]) {
 assert.equal(context.unresolvedError(error, [error, {reading: {...good.reading, ...override}}], now), true);
}
assert.equal(context.unresolvedError({reading: {...error.reading, window: 'Other'}}, [good], now), true);
assert.equal(context.unresolvedError({reading: {...error.reading, status: 'not_configured'}}, [good], now), false);
assert.equal(context.unresolvedError(good, [good], now), false);
assert.equal(context.unresolvedError({reading:{...error.reading,snapshot_scope:'quotas'}},
 [{reading:{...good.reading,snapshot_scope:'billing'}}],now),true);
assert.equal(context.unresolvedError({reading:{...error.reading,snapshot_scope:'quotas'}},
 [{reading:{...good.reading,snapshot_scope:'quotas'}}],now),false);
assert.equal(context.number('0.00000000000000000000000000001', 'USD'), '0.00000000000000000000000000001 USD');
assert.equal(context.number('9007199254740993.00001', 'credits'), '9007199254740993.00001 credits');
assert.equal(context.number('-0.00001', 'EUR'), '-0.00001 EUR');
assert.equal(context.number('0', 'USD'), '0 USD');
assert.equal(context.number(null, 'USD'), 'unbekannt');
console.log('UI source-wide error recovery, ties, independent sources and clock-skew checks passed');
