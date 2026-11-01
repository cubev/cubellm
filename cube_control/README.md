# CubeLLM Control

A separate account and operations service alongside the existing LiteLLM gateway, with no changes to gateway request handling or its database

The account catalogue covers OpenAI, Anthropic, OpenRouter, xAI, HeyGen and Suno. API accounts and subscriptions are separate records. A catalogue entry does not claim a working provider integration

## Data contracts

Observations carry source, observation time, collection time, reporting period, unit, status and freshness. Unknown values have no numeric amount. Credits, ratios, tokens and currencies cannot be substituted for each other. Provider-reported spending and observed proxy requests remain separate values, so overlapping data is never summed

A source-wide outage hides older measurements from that account, source and metric, including selected monthly spending. A newer successful report restores availability. An independent current source can supply monthly spending while the preferred source is unavailable

Imports are transactional and idempotent. Reusing an observation or request identity with different contents rejects the whole batch. Media jobs follow explicit reservation, submission and finalization states; terminal jobs cannot be reopened and expired reservations are excluded from available-work calculations

The HTTP service is read-only by default. Observation imports use only the CLI over an already authenticated management connection. Account changes use the same CLI or the opt-in browser management described below, which exposes account commands and nothing else. Provider credentials never enter observation payloads

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

The HTTP port binds to loopback by default. `CONTROL_BIND_ADDRESS` can explicitly select a trusted management interface. Unless browser management is explicitly enabled there are no mutating HTTP routes, and there is never a browser command execution endpoint. Do not expose the unprotected read interface to the public internet

The database uses an isolated Unix socket and PostgreSQL peer authentication. Both images define the application operating-system identity with UID 10001. The application database role is not a superuser and cannot create roles or databases. The database container has no network, no TCP listener, and no published port. `POSTGRES_HOST_AUTH_METHOD` is used only to satisfy the upstream image's passwordless initialization check; the active HBA file permits local peer authentication and rejects all TCP authentication

The deployment process must resolve `CONTROL_PYTHON_BASE` to an image digest and set a unique `CONTROL_RELEASE` before building a release. The checked-in PostgreSQL base is pinned by digest

## API

`GET /healthz` verifies database readiness. `GET /api/v1/summary` exposes source-qualified observations and separate proxy totals. `GET /api/v1/fleet` exposes the last imported host inventory with per-host timestamps. `GET /api/v1/inventory` exposes the account inventory described below. `/` is the browser overview

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
transport. The opt-in browser management sends the same versioned commands

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

## HeyGen account reader

`collect-heygen --config /private/heygen.env` reads `HEYGEN_API_KEY` from a private
regular file owned by the current user. Symlinks, special files, permissive
modes, duplicate assignments and oversized configs are rejected before any
provider request. The reader only calls `GET https://api.heygen.com/v3/users/me`,
with redirects and environment proxies disabled and a bounded response body.
HeyGen documents `account:read` for this endpoint. The collector never generates
videos, rotates keys, changes billing or transmits profile fields into the import

`--account-id` and `--account-name` keep multiple accounts separate. Register
custom accounts before importing failure-only reports. API failures preserve
unknown values and fixed reporting windows, so old wallet or credit-pool values
are superseded when the billing type changes. The read-only CLI emits an import
batch; it does not install a collector or change the production database

Wallet balances preserve their reported USD or credit unit. Subscription premium
and add-on credit pools stay separate, with independent reset times. Metered
usage and its spending cap remain provider-period measurements with no fabricated
month. `spending_cap` is a separate currency metric, not a monthly manual budget.
Inactive or null fields stay unknown; zero is a known value. A successful profile
response verifies API access, not per-request usage or invoice reconciliation

The contract was checked on 2026-10-31 against HeyGen's
[account profile documentation](https://developers.heygen.com/user-profile) and
[permission scopes](https://developers.heygen.com/docs/api-key-permissions).
Legacy quota conversion is not applied to the v3 credit values. Local tests use
synthetic HTTP responses; a real account report still requires an available
API key with the documented read permission. Deploy a reader accepting the new
metric before enabling any producer that emits it

Provider JSON numbers are decoded directly into Decimal rather than a binary
float. OpenRouter balance subtraction also expands decimal precision to preserve
all supplied digits within its validated input bounds. Original account amounts
and counters display their decimal strings in the overview and request explorer.
Remaining budgets are computed on the server from current reports of the
same currency and period, using exact decimal arithmetic. Long amounts and account names wrap in the mobile layout

## Complete provider snapshots

An observation may declare `snapshot_scope` when its collector emits a complete
report family. All rows in that report use the same `collected_at`. A newer
collection then supersedes the older rows in that account, source and scope,
including resources omitted by the new report. Late older reports remain stored
but cannot become current again. An error snapshot can contain an unavailable
`all` observation and retire the previous family without inventing zero usage

Use this only for complete reports, not incremental events or individual pages.
Independent scopes and unscoped observations retain their separate lifetimes.
Source-wide failures and browser recovery checks use the same scope boundary.
The schema is additive and does not reclassify legacy rows; readers accepting
the field must be deployed before scoped producers are enabled

## GitHub quota reader

`collect-github --config /private/github.env` reads `GITHUB_TOKEN` through the
same private-file checks as the other collectors. Optional `--account-id` and
`--account-name` select an independent catalogue identity. The command only emits
an import batch; it does not mutate GitHub or install a resident collector

The reader calls only `GET https://api.github.com/rate_limit`, using API version
2026-03-10. It preserves each resource's used, limit and remaining counters as
separate measurements, with nulls unknown and explicit zeroes intact. REST core
and search counters use request units, GraphQL uses points, and unclassified
resource families retain provider-unit labels. There is no derived invoice,
monthly spend, request history or per-application attribution in this endpoint

Complete response headers override the corresponding resource's body values.
Malformed or incomplete rate-limit headers make the report unavailable instead
of presenting potentially conflicting counts. Other resource families remain
from the body. Reset timestamps expire their measurements. Complete report scopes
retire removed resources and old outages without affecting other report families

GitHub documents that user quotas can be shared across credentials. Do not add
these snapshots across keys or treat the quota's used counter as this key's own
request count. Response headers on the actual application request govern its
allowance; this reader provides a periodic overview, not admission control

The contract was checked on 2026-10-31 against the official
[rate-limit endpoint](https://docs.github.com/en/rest/rate-limit/rate-limit) and
[GraphQL limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api).
A real authenticated read and exact PostgreSQL/HTTP round trip were verified
locally without exporting the token. This is not a production collector rollout
or evidence that applications already use CubeLLM for GitHub traffic

The shared provider HTTP reader bounds streamed reports, rejects redirects,
disables environment proxies and parses numeric JSON directly into Decimal.
Deploy readers accepting `snapshot_scope`, the quota metrics and their units
before enabling this producer. Register custom accounts before importing
failure-only reports


## Exact monetary aggregation

Gateway totals, imported request totals, reservations and OpenRouter balance
subtraction share exact decimal arithmetic. Calculation precision covers every
supplied coefficient and exponent plus possible carries, independently of the
caller's Decimal context. Original amounts remain unchanged. Unknown request
costs remain counted separately and are never silently converted to known zero

`budget_remaining` is keyed by `source:external_id` within each account and uses
only a current selected spending report and a current budget with the same
currency and period. Missing reports, outages and currency mismatches yield no
remainder. The browser displays the server's decimal string and does not subtract
binary floating-point values. A remainder based on estimated costs stays labelled
as such. Older readers without this field simply omit that optional calculation


## Request-time application identity

The native gateway can bind a key to an explicit application through operator
metadata `cube_application_id`. Only a bounded identifier is accepted. The
pre-call sanitizer overwrites `user_api_key_application_id`, including clearing
client claims when the key has no valid binding. The spend writer independently
derives this scalar from an actual authenticated UserAPIKeyAuth object. A plain
JSON dictionary or a client-supplied scalar cannot establish an application

Only the scalar enters the spend metadata. Full key metadata is not copied into
the audit. The native project grouping stays separate and must not be presented
as proof of an individual application. Binding edits affect later authenticated
requests and do not rewrite stored history or infer identities from today's key
records. A logging path without the authenticated object remains unassigned

This is attribution, not a grant to use models or provider accounts. Application
credentials must have narrowly scoped inference routes and no management routes,
including metadata updates; operators own provisioning and binding changes.
Actual key issuance, application wiring and protocol coverage still need their
own verification. Deploy the gateway writer and verify its stored scalar before
enabling the revised application projection. A companion rollout alone does not
install this native writer

The native `GET /cube_control/capabilities` endpoint requires a proxy administrator.
It reports the installed application identity writer, virtual-key request/error
log protection, whether custom keys are allowed, and the running global client
credential override policy. It returns no credentials or complete configuration.
A provisioning controller must check this endpoint before sending a custom key;
a missing endpoint is an unsupported gateway, not permission to proceed

Stored request bodies and provider error strings redact `key` and `keys` at
all nesting depths, alongside other credential fields. This also removes benign
fields with those names from stored prompt bodies. Original provider input is
unchanged; request costs, counters and the nonsecret `key_alias` remain available.
This protection concerns spend logs, not every external logger or callback


## Application credential provisioning

`prepare-application --state-directory PATH` reads a bounded JSON specification
from stdin and prepares one private operation without contacting the gateway:

```json
{
  "application_id": "example-app",
  "team_id": "existing-team",
  "gateway_origin": "http://127.0.0.1:4000",
  "models": ["example-chat"],
  "routes": ["/v1/chat/completions"]
}
```

Run `provision-application --state-directory PATH --config ADMIN_ENV` on the
gateway host or through a loopback management tunnel. `ADMIN_ENV` must be a
private, current-user-owned file containing `LITELLM_MASTER_KEY`. Neither command
requires the companion database. The administrative origin must be a literal
loopback address with an explicit port. LAN and Tailnet application endpoints
are configured separately

The controller checks native capabilities, existing team and configured model
aliases. Global or selected-model client credential overrides stop issuance.
It creates a dedicated `internal_user`, disables automatic key creation and
invitations, verifies ordinary team membership, and creates one key with the
specified model and inference-route restrictions. It checks the resulting row
again before reporting `provisioned`. Existing unrelated or privileged users,
changed permissions, blocked/expired keys, passthrough grants and key-level
alias/config overrides fail closed. Passthrough routes are accepted only when they
equal the specification's own account routes (see below). It never updates an
existing principal or key

The state directory must be owned by the current user with mode 0700, and its
files must have mode 0600. It contains the application key, never the master or
provider keys. Keep it outside source control and ordinary artifact uploads.
A separate lock and atomic fsynced journal persist each attempt before sending
a mutation. After a timeout, subsequent runs reconcile the same key by its hash;
they never repeat an uncertain creation, even after a not-found response.
Unresolved partial membership needs operator reconciliation. Changing a prepared
scope is rejected. Results and validation errors do not print raw credentials

`provisioned` verifies the native credential and its scope. It does not prove
provider billing-account ownership, request accounting, successful inference,
application wiring or network publication. Remote application installation,
rotation and runtime revocation verification remain separate work. This
command does not provide a generic passthrough permission or transfer provider
credentials to applications. The new native capability endpoint must be deployed
before provisioning can succeed


## Local application credential delivery

Run `deliver-application --state-directory PATH --destination-directory PRIVATE_DIR
--config ADMIN_ENV` after provisioning. The command checks the current native
capabilities, model policies, user, team membership and key scope again using
only management GET requests. It cannot create a missing user or key. If this
verification fails, it delivers no credential

The destination is a separate current-user-owned directory with mode 0700.
The command writes only the scoped application key followed by a newline to
`PRIVATE_DIR/api-key`, with mode 0600. A private temporary file, atomic publication
without replacement and fsync prevent a consumer from reading a partially
written key. Existing identical private files are accepted unchanged. A different
credential, symlink, unprotected file or directory, or concurrent destination
writer causes failure. Neither results nor exception messages contain secrets

Mount this file read-only as an application secret and configure the application's
CubeLLM endpoint separately. The file contains no gateway administrator key,
provider key, account configuration or controller journal. Delivery here is a
local file handoff; it does not install files on remote hosts, change application
configuration, test inference, or establish LAN/Tailnet connectivity. Gateway
authorization still applies when the application makes each request. Later
permission changes require another verification and are not frozen by delivery


## Application credential revocation

`revoke-application --state-directory PATH --config ADMIN_ENV` first records a
revocation request in the private journal. Preparation, provisioning and delivery
from that state are then disabled, including when the gateway is unreachable.
This local interlock does not itself revoke an already delivered credential

The controller checks the native capability contract and retrieves the key by
hash. Its user, team and managed operation metadata must match the prepared
application. It can then call native `/key/block` using only the hash. A changed
model allowlist or unsafe client credential policy does not prevent emergency
blocking of an owned key. Unrelated keys are never blocked. Every block attempt
is journaled before sending, and ambiguous outcomes are reconciled without
repeating the mutation. The command never unblocks, deletes or creates a key

The result separates the stored state from enforcement. `status: blocked` means
that the native key row is recorded as revoked with `blocked: true`.
`status: deleted` means the native deletion archive was returned. Both include
`enforcement: not_verified`: neither a database row nor a successful native block
response proves denial in every worker's authentication cache. CLI exit zero
confirms this recorded state only. Missing keys, conflicting ownership and
unknown outcomes never become a claim of verified runtime denial

The inspected native gateway evicts locally and schedules a best-effort Redis
broadcast. It does not acknowledge every worker, and backend or broadcast failure
can leave stale authentication entries until their TTL. Proving denial across
all serving workers and handling in-flight requests remain deployment acceptance
requirements. Previously delivered files remain in place; the controller does
not silently alter an application's mounted secret. Rotation and controlled
re-enablement require their own lifecycle implementation


## Generic pass-through application attribution

Generic pass-through requests now use the shared authenticated request metadata
snapshot before the spend writer runs. The writer can therefore retain the
application ID from the actual authenticated key, even when a client sends
forged application/authentication metadata. Later edits to the key metadata do
not alter the request snapshot. Full authentication objects and opaque key
metadata stay outside stored spend metadata

Configured outbound headers have case-insensitive precedence over both direct
request headers and stripped `x-pass-*` headers. This protects arbitrary configured
vendor credential and account-selection header names alongside the built-in
protected names. Unconfigured public `x-pass-*` options continue to work. Header
processing leaves both the incoming and configured mappings unchanged, so an
option from one caller cannot persist into a subsequent request

Native capabilities expose `passthrough_application_identity` and
`passthrough_header_isolation` for controllers that need these installed
contracts. The named account binding below adds request-time credential and origin
attribution; live account ownership still requires provider verification.
Query-based credentials, signatures, per-provider usage/cost parsing and actual account enrollment require
their own implementation and acceptance. The generic gateway's configured
per-request price must not be treated as an authoritative provider bill


## Named account credentials for HTTP routes

A generic HTTP route can select an existing native named credential without
putting its value in the route or handing the provider secret to applications.
The credential's nonsecret `credential_info` must contain both `cube_account_id`
and `cube_allowed_origins`, for example:

```json
{"cube_account_id":"example-account","cube_provider_id":"example-provider","cube_allowed_origins":["https://api.example.com"]}
```

Keep the provider value in the native `credential_values` or its supported
`os.environ/...` reference. Register the route using the native pass-through
configuration/management endpoint with an explicit method list:

```json
{
  "path": "/example-account/quota",
  "target": "https://api.example.com/quota",
  "auth": true,
  "methods": ["GET"],
  "account_credential": {
    "account_id": "example-account",
    "credential_name": "example stored credential",
    "headers": [{"name":"Authorization","field":"api_key","prefix":"Bearer "}],
    "application_grants": [{
      "application_id": "example-app",
      "key_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "account_id": "example-account",
      "path": "/example-account/quota",
      "methods": ["GET"]
    }]
  }
}
```

The route and credential account IDs must match. `cube_provider_id` identifies the
provider family independently of the account ID. It is captured from the selected
credential on each request; client metadata cannot replace it. Set it for custom
API domains so attribution does not depend on a fixed list of known vendor hosts. The target authority must be
in the credential's allowed origins; default ports are normalized. HTTPS is
required except on literal loopback addresses. No user information, target query
or fragment, unrestricted header forwarding, routing/framing header overrides,
client `x-pass-*` overrides or implicit method list is accepted. Query parameters
are denied unless individually listed in `allowed_query_parameters`; common
query credential names are refused even in that list. The final merged query is
checked too, including the native JSON request envelope's `query_params`.

Each request resolves the current credential once and captures its account and
origin. Changes affect subsequent requests. The bound HTTP request does not
inherit pooled client authentication, cookies, default headers or parameters and
does not follow redirects. Native credential read endpoints mask all value fields,
including arbitrary vendor field names; bound debug headers expose no characters.

Named account routes require an explicit application grant matching the actual
authenticated key's `cube_application_id` and SHA-256 token hash, the configured
account, exact proxy path and HTTP method. Replace the example hash with the
managed application key's native hash. Grants contain no raw application key.
Binding to that hash prevents another key from inheriting access by setting the
same application metadata. Missing or empty `application_grants` deny every caller,
including administrator keys, with 403 before credential resolution. Existing
account policies must add grants before activating this gateway version. Existing
key/team route permissions are required in addition to this account gate

Grant paths use the actual ASGI route with the deployment root prefix removed.
Host headers and JSON identity claims cannot replace that path or application.
Paths have no wildcard or implicit subpath matching; ambiguous dot, percent-encoded,
duplicate-slash and pattern paths are rejected in configuration. Methods are
explicit, uppercase and unique. A grant names its account, so retargeting a policy
to a different account also requires replacing the old grants. The native
administrator-only configuration update path owns these grants

Stored native spend metadata contains `provider_account_id`,
`provider_account_provider`, `provider_origin`,
`provider_account_source: configured_credential` and `provider_cost_status: unverified`.
The writer derives these from the server-held request snapshot and rejects client
imitations. It keeps the authenticated application identity independently. The
credential name and binding object are not stored. This proves configured
credential selection, not provider ownership or invoice accuracy. Controllers can
check `passthrough_account_binding: configured_credential_v1` and
`credential_values_full_masking` in the native capability response. The additional
`passthrough_account_provider: credential_metadata_v1` flag identifies the captured
provider-label contract. `passthrough_account_grants: application_account_path_method_v1`
identifies the additional request-time application/account/path/method gate

Actual account enrollment, provider-specific usage and
billing reconciliation, signed/query/OAuth credentials, mTLS and WebSocket account
binding remain open. Native JSON envelope/body handling still applies; arbitrary
API payload compatibility needs protocol tests. No production routes or LAN/Tailscale
access are activated by this source change.


## Exact application key paths

Native key `allowed_routes` entries can use `exact:/path` to allow only that
request path. For example, `exact:/vendor/quota` does not allow
`/vendor/quota/child` or `/vendor/quota/`. Existing plain entries retain their
native prefix behavior, and wildcard and named-group entries retain their
existing contracts. `exact:` does not turn a route group into a grant

The versioned capability `application_exact_routes: exact_path_v1` lets controllers
require this support before issuing a restricted key. Exact key paths still need
the native role/passthrough permission and, for named account routes, the separate
application/key/account/path/method grant. They do not grant provider ownership or
promise worker-wide reload. The application controller issues these entries for
its account routes, described in the next sections


## Atomic account grant management

Proxy administrators can inspect an existing named HTTP route with
`GET /cube_control/account_routes/{route_id}` and append a grant with
`POST /cube_control/account_routes/{route_id}/grants`. The descriptor exposes
account, origin, path, methods, existing grants and a configuration fingerprint,
without provider credential values. The capability
`passthrough_account_grant_management: atomic_append_v1` identifies this contract

The POST body contains `expected_fingerprint` from that descriptor and one `grant`
with `application_id`, the actual native `key_hash`, `account_id`, exact `path` and
explicit `methods`. It contains neither the application token nor provider keys.
Changes to routing invalidate the fingerprint and return 409. Other independent
grant additions do not invalidate it. An exact retry makes no database write;
changing methods on the same application/key/path returns 409. The requested
account, concrete path and methods must fit the registered route. A route allowing
subpaths can grant a concrete child path, without introducing wildcard grants

This operation requires database configuration updates, an existing
`general_settings` row and an existing account route. File-owned passthrough
configuration or disabled database updates return 409. It does not create routes,
issue keys, replace grants or delete grants. Concurrent updates lock the native
configuration row and patch only the grant list with PostgreSQL JSONB operations.
The transaction reads back the stored grant before committing; unrelated JSON
values, including exact decimal numbers, are preserved. Object and JSON-string
configuration representations are supported

A successful receipt reports `stored: true` and `enforcement: not_verified`.
The existing native configuration cache is invalidated, but this does not prove
that every worker has reloaded or that a provider request succeeds. Dynamic
resource scopes, a new complete gateway image and real application acceptance
remain open


## Application account routes

An application specification can name existing named HTTP account routes in
addition to, or instead of, inference models and routes. Models and inference
routes still have to be given together, and a specification with neither inference
scope nor account routes is rejected:

```json
{
  "application_id": "example-app",
  "team_id": "existing-team",
  "gateway_origin": "http://127.0.0.1:4000",
  "account_routes": [{
    "route_id": "example-route",
    "account_id": "example-account",
    "expected_fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "path": "/cube_api/example-account/quota",
    "methods": ["GET"]
  }]
}
```

Take `expected_fingerprint` from `GET /cube_control/account_routes/{route_id}`.
Managed account paths must live under the dedicated `/cube_api/` gateway namespace,
so they cannot collide with native administration or inference paths. Paths are
concrete and canonical: no wildcards, percent encoding, dot segments, duplicate
slashes or the bare namespace. Methods are explicit, uppercase and unique. Each
path may appear once. A route with `include_subpath` can be used for several
concrete child paths, each with its own methods, without granting its siblings.
One key can use routes of several accounts, each pinned to its own fingerprint

With account routes, provisioning additionally requires the capabilities
`application_exact_routes`, `passthrough_account_grants`,
`passthrough_account_grant_management`, `passthrough_account_binding`,
`passthrough_header_isolation` and `credential_values_full_masking`. Before any
user or key is created it reads every route descriptor and stops if the route ID,
account, fingerprint, method subset or path boundary differ from the prepared
scope, or if a grant for the prepared key hash already exceeds it. Grants of other
applications and keys on the same route are left untouched

The key gets one `exact:/cube_api/...` entry per account path in `allowed_routes`
and the same paths in `metadata.allowed_passthrough_routes`. After the key row is
verified, the journal records `account_grants_attempted` and the controller appends
missing grants through the atomic grant API. Grants already stored are read back
instead of being posted again. Because that API is idempotent, a timeout during
grant storage only retries the grant operation; it never repeats an uncertain user
or key creation. A final read confirms that every prepared grant is really stored.
Delivery is read only: it delivers nothing if a grant is missing or a stored grant,
fingerprint, key metadata or passthrough list has drifted. Revocation compares the
key's actual metadata, including the passthrough list, before blocking it

Results carry `enforcement: not_verified`, also after storage and delivery. This
shows the stored native state, not worker reload, a successful provider call or
LAN/Tailnet reachability. The journal is size-checked before any temporary state
file is created, so an oversized specification leaves no unreadable state behind.
Create missing routes first with `provision-account-route`, described next


## Account route creation

Proxy administrators can create a named HTTP account route with
`POST /cube_control/account_routes`. The capability
`passthrough_account_route_management: atomic_create_v1` identifies the contract.
The body is `{"route": {...}}` with `id`, a concrete `path` under `/cube_api/`,
`target`, explicit uppercase `methods`, optional `include_subpath` and an
`account_credential` without grants. The gateway stores the route with
`auth: true` and `forward_headers: false`. Grants are appended separately

Before storing, the named credential must be usable for the account and target
origin on the serving gateway: it must exist, carry the matching `cube_account_id`
and allow the origin, and its header fields must resolve. Otherwise the request
returns 422 and nothing is written. Targets with blank or control characters,
also after percent decoding of the host, are rejected. Internal host names,
IPv6 zone identifiers and percent-encoded path data stay valid

A new path must not be captured by, or capture, any other route. The check uses
the real Starlette path patterns of every stored pass-through route and of every
route on the application that serves the request, including parameter routes such
as `/{mcp_server_name}/mcp` and `:path` converters. A subpath route also conflicts
with any pattern that could match one of its children. Patterns that cannot be
evaluated safely, unknown router entries and the same path with other methods are
rejected conservatively. Conflicts return 409 and write nothing

The transaction inserts an empty `general_settings` row when none exists, locks the
row, appends exactly one route with a server-side JSONB update and reads it back
before committing. Unrelated settings and exact decimal values are preserved. An
identical retry returns the existing route with `created: false` and writes nothing,
even after grants were added. A different definition under the same ID conflicts.
The configuration cache is invalidated only after a real creation. The receipt
carries the route descriptor, its fingerprint and `enforcement: not_verified`

From the controller, run `provision-account-route --config ADMIN_ENV` with the
route specification on stdin:

```json
{
  "gateway_origin": "http://127.0.0.1:4000",
  "route_id": "example-route",
  "account_id": "example-account",
  "credential_name": "example stored credential",
  "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
  "target": "https://api.example.com/v1/quota",
  "path": "/cube_api/example-account/quota",
  "methods": ["GET"]
}
```

The command rejects invalid input with exit code 2 before it reads the
administrator file or contacts the gateway. It checks the capability, posts the
definition and verifies that the receipt matches route ID, account, path, methods,
subpath flag, target origin and grant accounts. It prints `created` or `existing`
with the fingerprint to use as `expected_fingerprint` in the application
specification. Definite rejections (400, 409, 422) are errors. Other outcomes are
`uncertain`, and repeating the identical command is safe because creation is
idempotent. The worker that serves the request registers the route when the
configuration reloads; worker-wide registration and provider access are not verified


## Account resource ownership

Some provider APIs create resources such as media jobs or files and then address
them by ID. A named account route can declare resource types so that each
application reaches only the resources it owns. The capability
`passthrough_account_resources: application_ownership_v1` identifies the contract

A resource type belongs to the route and is part of its fingerprint. It has a
stable `name`, one to sixteen `item_paths` and an optional `creation` rule. Each
item path lies below the route path (the route needs `include_subpath`) and has
exactly one whole `{resource_id}` segment between literal segments, for example
`/cube_api/media/jobs/{resource_id}` or `/cube_api/media/jobs/{resource_id}/cancel`.
Item paths must not overlap. A creation rule names a concrete path, explicit
methods and an `id_selector` of one to eight object keys or array indexes that must
lead to one string. Resource IDs are one segment of `[A-Za-z0-9._~:@-]`, at most
256 characters and never `.` or `..`. Routes without resource types keep their
stored form and fingerprint

Ownership is stored per account, type and ID in
`LiteLLM_AccountResourceOwnership`. The owner is the application; the row also
records whether the resource was `created` through the gateway (with the creating
key hash) or `adopted` by an administrator. A request to an item path is allowed
only when the key carries a `resource:<item path>` route, has an active grant for
that exact template and method, and the application owns the resource on that
route. A rotated key with its own grant keeps access. Non-canonical IDs are denied
without a lookup and never fall back to literal grants

After a successful JSON response to a declared creation request, the gateway looks
at every resource type whose creation path and method match and applies each type's
own selector. Types whose selector yields a canonical ID are claimed together in one
transaction; a type whose selector finds no ID stays unowned and does not block the
others. A resource already owned by another application stays with that owner and is
reported as a conflict for that type, while the other claims are still recorded.
Only a storage or readback verification failure rolls back the whole batch. Capture
never replaces the provider response: error statuses, streamed or non-JSON bodies,
a missing key identity or a store failure record nothing and are logged. Without a
row, later item requests for that resource are denied

Administrators bind existing provider resources with
`POST /cube_control/account_routes/{route_id}/resources` and
`{"expected_fingerprint", "resource_type", "resource_id", "application_id"}`. The
fingerprint check, type check and insert run in one locked transaction; a stale
fingerprint or another owner returns 409, an undeclared type 400 and a missing
route 404. `GET .../resources/{resource_type}/{resource_id}` reads the ownership

In the controller, `provision-account-route` accepts `resource_types` and sends
them only to gateways with the ownership capability. Route base and creation paths
must stay concrete. An application scope may name an item template instead of a
concrete path; such a scope becomes a `resource:` key route and passthrough entry
and requires the ownership capability. `adopt-account-resource --config ADMIN_ENV`
reads an adoption specification on stdin:

```json
{
  "gateway_origin": "http://127.0.0.1:4000",
  "route_id": "example-media",
  "expected_fingerprint": "<route fingerprint>",
  "resource_type": "video-job",
  "resource_id": "vid_123",
  "application_id": "example-app"
}
```

It prints `adopted`, `existing`, `conflict`, `error` or `uncertain`. Repeating the
identical command is safe

## Account provenance in the usage audit

The companion exposes the whitelist SQL columns for native spend rows through
`python -m cube_control native-gateway-projection`. Feed rows selected with those
columns to `python -m cube_control project-native-gateway` using the existing scan
and instance-identity envelope. These commands export/project data without opening
a database. They do not replace the collector's read-only transaction, pagination,
completion horizon, instance pinning or durable pending-import journal.

Before enabling this producer mode, verify the native account/provider capability
flags and companion `gateway_native_account_projection: credential_snapshot_v1`.
The existing producer and production releases are not switched by this change.
The older `project-gateway` command remains available. Retry an existing pending
batch unchanged; do not strip or rewrite a pending batch to fit an older reader.

A named-credential row carries `account_evidence` with its source, origin, optional
captured provider label and explicitly unverified provider-cost status. The native
projection accepts only the server-stamped account leaves, checks the origin
against the recorded target and rejects user information and conflicting provider
labels. An explicit captured provider supports arbitrary API domains. For older
bindings without that label, only a known vendor host supplies provider identity;
an unknown custom origin is rejected until its provider is configured. Invalid
rows make the scan partial and are counted rather than blocking the whole page.
An imported account must still exist as a matching API account in the catalogue.

Bound generic HTTP calls are classified as provider API traffic. Their token and
cache counters remain unknown until a protocol-specific measurement parser exists;
native placeholder zeros are not treated as measurements. Gateway-reported amounts
remain separate from provider billing and keep their unverified cost marker.

Usage rows and correction history show the account proof, captured provider label,
target origin and cost status. Historic snapshots retain their original values.
Absent evidence and absent provider labels are omitted when serializing, preserving
legacy batch fingerprints and history payloads. This uses field-level conditional
serialization, so the API retains its typed record schema. The companion now
requires Pydantic 2.12 or later for that field option; see the
[official serialization documentation](https://pydantic.dev/docs/validation/2.12/concepts/serialization/).

These are source/artifact contracts and local acceptance results. Central account
enrollment, source activation, provider ownership/billing verification and application
LAN/Tailscale rollout still require their respective acceptance steps.


## Native artifact verification

The release wheel is built with the repository's pinned Rust/Maturin toolchain
and unchanged release profile. The Linux verifier checks its Python ABI,
architecture, matching wheel metadata and actual ELF glibc requirements, together
with the existing import, stripping, vocabulary and size checks. Run the SDK
acceptance tests from a fresh installed wheel with isolated imports so source
files cannot hide a missing or stale native extension

Native SDK artifact acceptance now includes the account/gateway subset, real
native tokenizer execution and the local recording-server route checks. The
complete gateway image, rebuilt native Admin UI, target-runtime execution and
coordinated production/source activation remain separate required steps

## Gateway account inventory

`python -m cube_control collect-gateway-inventory --config admin.env --gateway-origin http://127.0.0.1:4000` reads the gateway's `GET /cube_control/account_inventory` and stores the result under the canonical gateway origin. The configuration file must be a regular file owned by the current user with no group or other permissions, and must contain `LITELLM_MASTER_KEY`. The origin must be an explicit loopback HTTP origin with a port. Any other origin is rejected without a request and without a stored record. The command prints only the attempt status, a sanitized message and per-source counts, and exits non-zero unless the inventory was collected and stored. A failed store write never reports a collected inventory

Every attempt is stored with its time and status. Each gateway keeps per-source evidence for credentials, routes and deployments, separately for the gateway's runtime view and its database. A source read completely replaces that source's records, so a complete empty source clears obsolete bindings. A partially readable source adds what it read and keeps the remaining known records. An unavailable source keeps every known record. Kept records are marked `retained` and keep the time they were last read. They are never re-stamped as current. Records without a stable identity, such as a route without id or path or a deployment without a model id, never remove a retained record. Older or equal deliveries never replace newer evidence, and concurrent collectors for the same origin are serialized. `latest_collection` only means the record was readable in the latest collection, not that it is fresh or provider-verified

`GET /api/v1/inventory` is read-only. It joins the catalogue, manual entries, media jobs, observations, usage events, gateway records and every collected gateway. Accounts known only from a gateway, a job or a manual entry stay listed with their source and a `not_in_catalogue` gap. Nothing is enrolled, and provider or ownership is never inferred from model names. Each gateway reports `current`, `stale`, `last_attempt_failed` or `never_collected` together with its per-source evidence. Unbound, invalid, conflicting and unattributed records are findings. Provider usage counts as verified only for a current, fresh, known provider reading of the current period. Older readings stay visible as history. Overall coverage is `configured_sources_complete` only when every collected gateway is current with all six sources complete and nothing rejected

Inventory rows written before source-wise evidence existed are read as evidence and are not rewritten. `init` adds the evidence column to an existing table


## Browser account management

Browser management is off unless `serve` is started with a token file and a management origin. The operator name is optional and defaults to the service's operating-system user, but an explicit, stable name is recommended. The service refuses to start if any given value is invalid:

```sh
export CUBE_CONTROL_ADMIN_TOKEN_FILE=/path/to/control.env
export CUBE_CONTROL_MANAGEMENT_ORIGIN=http://127.0.0.1:4001
export CUBE_CONTROL_OPERATOR=local-owner
python -m cube_control serve --host 127.0.0.1 --port 4001
```

The token file follows the same rules as other private configuration: a regular file owned by the service user with no group or other permissions, containing `CUBE_CONTROL_ADMIN_TOKEN` with at least 32 characters. The management origin must be the exact origin the browser uses. Plain `http` is accepted only for a loopback host, so any other host needs `https` and then gets a `Secure` cookie. The operator name, from `CUBE_CONTROL_OPERATOR` or the service user, is recorded as the actor of every browser command. It stays the same across sessions, so retrying an already committed command after signing in again returns `duplicate` instead of a conflict

Signing in exchanges the token for a random session in an HttpOnly, `SameSite=Strict` cookie. Every write needs that session, its CSRF value, the configured `Origin` and `Host`, a same-origin fetch and a JSON body of at most 64 KiB. Failed sign-ins are limited to 10 per 15 minutes, at most 32 sessions are active, and a session lasts 8 hours. Signing out revokes the session on the server. Sessions live in the process, so a restart signs everyone out, and the service must run as a single worker

Writes go through the same versioned commands, revisions and journal as the CLI. Enrolling a discovered account requires an explicit provider ID, account kind, name and confirmation. Nothing is inferred from the inventory. Edits cover every manual field, including clearing plan and note, monthly budgets per month and restoring the automatic value. A command whose result is unknown stays pending with its original ID and payload until it is resent or explicitly discarded, and further edits of that account wait for it. After a confirmed change the account stays locked until the refreshed view shows that revision. Unsaved drafts survive refreshes and keep the revision they were opened on, so an outside change in between surfaces as a conflict
