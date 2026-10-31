# CubeLLM Control

A separate account and operations service alongside the existing LiteLLM gateway, with no changes to gateway request handling or its database

The account catalogue covers OpenAI, Anthropic, OpenRouter, xAI, HeyGen and Suno. API accounts and subscriptions are separate records. A catalogue entry does not claim a working provider integration

## Data contracts

Observations carry source, observation time, collection time, reporting period, unit, status and freshness. Unknown values have no numeric amount. Credits, ratios, tokens and currencies cannot be substituted for each other. Provider-reported spending and observed proxy requests remain separate values, so overlapping data is never summed

A source-wide outage hides older measurements from that account, source and metric, including selected monthly spending. A newer successful report restores availability. An independent current source can supply monthly spending while the preferred source is unavailable

Imports are transactional and idempotent. Reusing an observation or request identity with different contents rejects the whole batch. Media jobs follow explicit reservation, submission and finalization states; terminal jobs cannot be reopened and expired reservations are excluded from available-work calculations

The HTTP service is read-only. Account changes and observation imports use the CLI over an already authenticated management connection. Provider credentials never enter observation payloads

## Run

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps .
export CUBE_CONTROL_DATABASE_URL='dbname=cubecontrol user=cubecontrol host=/run/postgresql'
.venv/bin/python -m cube_control init
.venv/bin/python -m cube_control serve
```

The local runtime must supply PostgreSQL. The separate Compose stack supplies its own database and never joins the gateway's database network

```sh
docker compose -p cubellm-control -f compose.yaml up -d --build
```

The HTTP port binds to loopback by default. `CONTROL_BIND_ADDRESS` can explicitly select a trusted management interface. There are no mutating HTTP routes or browser command execution endpoints. Do not expose the unprotected read interface to the public internet

The database uses an isolated Unix socket and PostgreSQL peer authentication. Both images define the application operating-system identity with UID 10001. The application database role is not a superuser and cannot create roles or databases. The database container has no network, no TCP listener, and no published port. `POSTGRES_HOST_AUTH_METHOD` is used only to satisfy the upstream image's passwordless initialization check; the active HBA file permits local peer authentication and rejects all TCP authentication

The deployment process must resolve `CONTROL_PYTHON_BASE` to an image digest and set a unique `CONTROL_RELEASE` before building a release. The checked-in PostgreSQL base is pinned by digest

## API

`GET /healthz` verifies database readiness. `GET /api/v1/summary` exposes source-qualified observations and separate proxy totals. `GET /api/v1/fleet` exposes the last imported host inventory with per-host timestamps. `/` is the browser overview

```sh
python -m cube_control import observations.json
python -m cube_control import - < observations.json
```

Input follows the `ImportBatch` model: accounts, observations, events, jobs and an optional fleet snapshot. Imports reject unknown fields and invalid units without echoing their input. Secrets belong in the provider's runtime credential store, never in imported metadata

## Validation

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

PostgreSQL integration tests additionally require `CUBE_CONTROL_TEST_DATABASE_URL` targeting a dedicated database literally named `cubecontrol_test`. They test transactional rollback, idempotency, concurrent imports and application-role privileges. They must never target the gateway database or the production companion database

## Current boundaries

The service consumes collectors through an authenticated management bridge, supports local account edits and passively ingests gateway events. Provider-native routing, credentials and application rollout still require verification for each account. Deployment and recovery tools live in the existing operations project. Unknown provider balances and subscription states stay unknown until a real source or explicit manual entry is imported

This service uses a separate PostgreSQL instance to keep the live gateway fully isolated and to avoid copying or generating database passwords. No provider API key or new remote access credential is created by this stack

## OpenRouter account collector

`cubellm-control collect-openrouter --config /path/to/private.env` emits an
ImportBatch on stdout, without requiring a database connection. Pipe or import
this metadata through the same trusted management path as other collectors.
The env file stays on the collector host, belongs to its user and has mode 0600.
It contains an existing OPENROUTER_MANAGEMENT_KEY. The collector never creates
keys, changes account settings or forwards credentials across redirects

The read-only /api/v1/credits endpoint requires a management key. Its purchased
credits and cumulative usage are denominated in USD. Remaining balance is their
difference, including a negative balance when overdrawn. Cumulative usage is
labelled as lifetime usage, never as current-month spending. Missing credentials,
permission failures and malformed responses become unavailable observations,
never measured zero values. Invalid response bodies are not logged

Contract checked against the official [credits API](https://openrouter.ai/docs/api/api-reference/credits/get-credits)
and [billing explanation](https://openrouter.ai/support) on 2026-10-24.
Live account verification requires an existing authorized management credential

## Manual account management

The management CLI changes the local account registry, never the vendor's plan,
billing settings or subscription. It uses the existing authenticated management
transport. HTTP remains read-only

Read `data.manual_version` from the account summary before preparing a command.
Send a versioned command to `cubellm-control manage command.json --actor local-owner`:

```json
{
  "schema_version": 1,
  "command_id": "example-user-action-1",
  "account_id": "suno-subscription",
  "expected_version": 0,
  "changes": [
    {"field": "plan", "action": "set", "value": "Existing plan"},
    {"field": "budget_usd", "action": "set", "value": "20.00", "period": "2026-10"}
  ]
}
```

Use a new globally unique command ID for each intended edit. A retry must reuse
the exact command and actor. `duplicate` confirms the original edit without
writing again. `conflict` returns the current version and applies nothing.
A management transport timeout leaves the result unknown, so retry that same
saved command instead of creating a new action

Fields are name, state, plan, note, budget_usd, recurring_usd, balance_usd and
balance_credits. Budgets require a YYYY-MM period; other fields have no period.
USD values use decimal strings with at most two significant decimal places.
Manual financial amounts are limited to an absolute value of 10^15. These
metadata fields must never contain credentials

Each field keeps separate manual provenance and a central account revision.
Collector updates continue and remain available as the automatic account.
Use `{"field":"plan","action":"automatic"}` to release an override. Releasing
a budget also requires its period. The next view uses the latest automatic
value without deleting the manual history. The dashboard shows manual field
age and divergence from the automatic account. Balances expire after 15 minutes,
prices and budgets after 31 days; a stale override remains explicit and does not
silently fall back to a collector value. Previous and future month budgets are
never presented as this month's budget

The immutable command journal lives only in the companion database. New tables
are additive; no gateway schema or data is changed. Commands validate all their
fields before a single transaction, and concurrent editors cannot both apply
the same expected account revision

## Passive gateway import

`project-gateway` validates a whitelist projection and hashes source primary keys
before emitting a GatewayBatch. `import-gateway` updates the current mirror and
records changed projections in a separate append-only history. Neither command
calls a model or changes gateway configuration. The resident producer uses a
read-only PostgreSQL transaction in the existing gateway database; no database
password or provider credential is copied to this application

Only explicit routing provider information or a known API endpoint assigns a
provider. An OpenAI-compatible custom endpoint is not sufficient evidence of
OpenAI billing. Model names never determine attribution. Unassigned calls stay
in the mirror and are reported separately. Cost and token counters are nullable,
and their summaries carry unknown counts alongside known sums. Gateway-reported
zero spend is not verified free usage. Provider reports remain separate

Each source scan has an idempotent ID and expected central sequence. A pending
scan must be retried identically before observing another source snapshot.
Changed content updates one mirror row and appends a history row. A revert A to
B to A therefore works even when source update timestamps tie. No timestamp
selects the winning version; source observations are centrally serialized.
Unchanged scans do not append duplicate changes. Missing rows are not deleted,
because source retention is not evidence of a refunded or cancelled request

The collector pins database instance and table identity. A changed instance
requires explicit operator reconciliation. It pages stored completed rows with
a fixed horizon and rescans the whole stored source on subsequent cycles. Long
scans continue on the next scheduled run. Its status describes stored source
coverage only, never all vendor usage. Direct calls, pruned logs and unavailable
rows cannot be reconstructed from this gateway. Partial and failed imports are
visible. Readers must be deployed and healthy before enabling a new producer

## Request-level usage audit

The browser overview includes a request explorer with UTC month, provider, account,
application, model and status filters. It shows original decimal USD strings,
nullable token counters, source references and correction history. Missing
attribution stays unknown. Gateway-reported charges are not a verified provider
invoice, and observed zero does not prove a free request

`GET /api/v1/usage?period=2026-01&limit=50` returns the matching total, known sums,
unknown counts and a bounded page of source-qualified records. Optional filters
are source_id, provider, account_id, application_id, model, status and traffic_kind. Status is
succeeded, failed or unknown. Limit is between 1 and 200. All filters apply to
both the rows and the totals. Historical months remain queryable independently
of the current overview

traffic_kind separates known provider_request calls, known gateway_management
calls and unknown call types. Administrative requests such as model information
and key listing remain inspectable without being presented as provider requests.
Missing call types and newly encountered endpoints stay unknown until classified

To continue, pass next_cursor as the cursor query parameter with the same filters
and limit. If any source scan advances, HTTP409 requires restarting the query
so pages cannot silently combine different mirror versions. Invalid cursors and
changed filters return HTTP422. Database failures return HTTP503. Cached browser
rows are explicitly labelled as a previous successful result after a failure

`GET /api/v1/usage/SOURCE/REQUEST/history?after=0&limit=50` exposes recorded changes
with their sequence and previous/content hashes. Use the returned source_id and
record.source_key from the explorer. next_after continues a longer history.
An unchanged rescan does not create a correction, and A to B to A remains three
inspectable versions while contributing one current row to totals

Imports can carry confirmed account_id, application_id, deployment_id, call_type,
completed_at and duration_ms. Legacy imports leave these unknown. Only trusted
management projections may supply identity: a model name, a provider total or
arbitrary client metadata is not evidence of an account or authenticated app.
Custom provider identifiers are supported. No provider credential, request body,
response body or raw gateway token belongs in a projection

Claimed accounts must already exist, match the record's provider and be API
accounts. An invalid claim rejects the entire scan before it advances. The
account overview only shows request totals for explicit matching account IDs;
it never copies a provider total to every account at that provider. The provider
total includes assigned requests and must not be added to account subtotals

Gateway record timestamps are canonical UTC values, so a source session timezone
change does not create a false correction. `GET /api/v1/capabilities` advertises
the supported gateway projection fields before a new collector is enabled.
Existing materialized pending batches must finish unchanged before collecting
a new projection

The audit covers retained completed gateway rows, with one row per source and
request reference. Independent sources may overlap. It cannot reconstruct
direct provider calls, removed logs, missing stream completion usage or unobserved
Realtime sessions. Source status and import age remain visible. This endpoint
uses the same private read interface as the rest of the companion

For a real local browser regression, install Playwright in a test environment
and run `PYTHONPATH=src python tests/ui_usage.py` with the dedicated
CUBE_CONTROL_TEST_DATABASE_URL. CUBE_CONTROL_BROWSER_EXECUTABLE can select an
already installed Chromium. The test starts its own loopback HTTP server and
checks decimal precision, unknowns, correction history, pagination, concurrent
scan changes, failed requests, script injection and a 390px viewport. It never
calls a provider. Screenshots default to /tmp/cubellm-usage-proof

## Private Tailnet edge preparation

`prepare-edge` reads a metadata-only specification from stdin and emits a
versioned JSON artifact containing systemd units, Tailnet Serve commands and
matching deactivation commands. It requires no database and never applies the
artifact or changes a running service

```sh
printf '%s' '{"backend_host":"192.168.1.10","paths":["/v1"]}' | python -m cube_control prepare-edge
```

The artifact binds a relay socket to loopback and uses the server's standard
systemd-socket-proxyd to reach the private gateway address. Tailscale Serve can
then terminate HTTPS on the existing Tailnet node without enrolling another
device. No provider credential or master key belongs in this artifact. Gateway
authentication remains required, and Tailnet device identity does not replace
application identity. Public backends, root paths, administration namespaces,
duplicate paths and relay loops are rejected

The generated commands preserve configured API prefixes, use Serve rather than
Funnel and deactivate only their own paths. Activation still requires checking
the existing node/service configuration, file ownership and port availability.
No generated command resets the shared Serve configuration. Gateway key/role
checks, path handling, TLS and access from an actual Tailnet client must be
verified before switching an application

The local regression uses the actual standard relay binary to verify binary
transport, bidirectional traffic, half-close and exclusion of payloads from its
output. It also asks systemd to verify the generated units. These host tests are
skipped where the server relay binary is absent, such as the companion container.
They do not establish live Tailnet connectivity or complete Realtime accounting

## Versioned media job management

`manage-job` reads a JSON command from stdin or a file. It requires a stable
`command_id`, the current `expected_version` and the complete `job` value from
the MediaJob import contract. `--actor` records the local operator. Keep the
command unchanged until its outcome is known: retrying the same command and
actor returns the original revision, while changing an already used command ID
is rejected. This command records lifecycle evidence and does not submit a
provider request or enforce a spending budget

```sh
python -m cube_control manage-job --actor local-owner < job-command.json
python -m cube_control job-history example-api example-job --limit 50 --after 0
```

New jobs start reserved or submitted on a registered API account. Subsequent
commands require the current revision. Unit, original reservation, expiry and
identity stay fixed. Finalized jobs require actual usage. An unsubmitted
reservation can expire after its deadline, while a submitted job keeps its hold
until a confirmed finalization or failure. A timeout or unknown provider result
must remain submitted. Reserved holds can age out of the overview without a
background write; submitted holds never disappear just because local time passes

The companion exposes GET-only `/api/v1/accounts/{account_id}/jobs/{job_id}`
with its current version and the corresponding `/history?after=0&limit=50`.
History pages contain the original command, actor, receipt time and previous
snapshot, including an imported baseline when first managed. `next_after` is an
exclusive revision cursor; old entries remain unchanged when later commands
arrive. Decimal amounts remain strings. Missing jobs return 404; absent readers
or unavailable databases return 503. Import-only jobs have revision zero

Initialization adds the job command table without rewriting existing rows.
Managed jobs reject differing legacy imports atomically, so an old collector
cannot bypass revisions or erase the journal. An identical import is harmless.
For a binary rollback, stop job writers and retain the additive table and journal;
an older importer does not understand this ownership rule. No production schema
or service is changed by local acceptance. Read access uses the existing private
companion interface; the PC audit bridge currently forwards request usage only

The OpenRouter reader accepts `--account-id` and `--account-name` for independent
accounts, each with its own private config file. Existing calls retain the
`openrouter-api` identity. All emitted observations use the explicit account ID;
provider errors do not move measurements to the default account or replace its
account metadata. Register custom accounts before importing failure-only reports

```sh
python -m cube_control collect-openrouter --config /private/account-one.env --account-id account-one --account-name 'Account one'
```
