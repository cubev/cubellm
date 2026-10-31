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
alias/config overrides fail closed. It never updates an existing principal or key

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
rotation, runtime revocation verification and non-LLM application grants remain
separate work. This
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
Query-based credentials, signatures, application route grants, per-provider usage/cost parsing and actual account enrollment require
their own implementation and acceptance. The generic gateway's configured
per-request price must not be treated as an authoritative provider bill


## Named account credentials for HTTP routes

A generic HTTP route can select an existing native named credential without
putting its value in the route or handing the provider secret to applications.
The credential's nonsecret `credential_info` must contain both `cube_account_id`
and `cube_allowed_origins`, for example:

```json
{"cube_account_id":"example-account","cube_allowed_origins":["https://api.example.com"]}
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
    "headers": [{"name":"Authorization","field":"api_key","prefix":"Bearer "}]
  }
}
```

The route and credential account IDs must match. The target authority must be
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

Stored native spend metadata contains `provider_account_id`, `provider_origin`,
`provider_account_source: configured_credential` and `provider_cost_status: unverified`.
The writer derives these from the server-held request snapshot and rejects client
imitations. It keeps the authenticated application identity independently. The
credential name and binding object are not stored. This proves configured
credential selection, not provider ownership or invoice accuracy. Controllers can
check `passthrough_account_binding: configured_credential_v1` and
`credential_values_full_masking` in the native capability response.

Actual account enrollment, application route grants, provider-specific usage and
billing reconciliation, signed/query/OAuth credentials, mTLS and WebSocket account
binding remain open. Native JSON envelope/body handling still applies; arbitrary
API payload compatibility needs protocol tests. No production routes or LAN/Tailscale
access are activated by this source change.
