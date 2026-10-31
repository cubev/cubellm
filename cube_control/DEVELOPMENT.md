# CubeLLM development and acceptance

Scope reconciled on 2026-10-31 against this service, the gateway code, ServiceInventory and the existing CubeUsageBar operations status. The owner explicitly requires every API account to be managed, monitored and supplied to applications across LAN and Tailscale, with precise, inspectable usage. This includes non-LLM APIs and resumes the previously deferred HeyGen API work. An implemented protocol or passing local fixture is not proof of provider authorization, a live billing value, or mobile connectivity

| Capability | Verified baseline | Remaining work and acceptance |
| --- | --- | --- |
| Account catalogue and manual management | Transactional CLI, revisions, creation, field provenance and desktop handoff exist | Preserve source distinctions and exercise database concurrency tests |
| Current measurements | Source-qualified freshness, manual overrides, separate provider and proxy totals | Outage-masked spending fixed in c6b760d; maintain API and browser regression coverage |
| Gateway ingestion | Passive scans, nullable counters, provider attribution and idempotent history exist | Verify existing client routes; complete project attribution and Realtime session usage before claiming full traffic coverage |
| Provider collectors | OpenRouter collector here; other existing collectors and live-source notes in CubeUsageBar | Add native collectors where supported without duplicating or replacing verified sources; missing permissions remain explicit |
| Media jobs | Versioned local commands, immutable history, GET-only status/history and separate unit reservations pass real PostgreSQL tests | Connect authoritative provider status and billing; verify uncertain submissions against actual vendor jobs |
| Deployment | Existing build, backup, rollback and activation watchdog tools in CubeUsageBar | Run updated artifact tests before a rollout; retain gateway and database isolation |
| Browser overview | Account filters, fleet, gateway totals and native editing links exist | Cover refresh failures, rollover, source outages and job monitoring without presenting cached values as current |
| Mobile access and persistent collection | Trusted bridges, resident collectors and tested private-edge preparation exist | Verify actual off-LAN activation, app authentication and independent sources; do not claim a desktop source works while its host is off |
| HeyGen API | V3 read-only wallet, separate subscription pools and metered usage/cap reader passes local HTTP tests | Obtain existing authorized API access, verify live account behavior, add routing and per-job usage reconciliation |
| Anthropic individual-account admin billing | Existing source documents unsupported account type | Keep unavailable; prior account-conversion deferral remains in force |

## Verification baseline

Run `PYTHONPATH=src python -m unittest discover -s tests -v` in this directory. Database tests require a dedicated database named `cubecontrol_test`, owned by a non-administrator application role. Never run them against either production database

Run `node tests/ui_errors.cjs` for the browser source-error regression. Run Ruff on changed Python files, review the diff, and record which live checks could actually run. Commit completed increments separately. Keep production activation separate from local acceptance and use the existing build, database-test, backup and guarded activation gates

## Current increment

Prioritize the gateway audit surface and reliable application/account attribution. Expose request-level records, reporting periods, model and counter details, missing values and correction history. Provider billing and gateway-reported charges remain separate. Application identity must come from an explicit authenticated key binding, and account identity from a confirmed server-controlled deployment mapping. Model names, arbitrary client metadata and the existence of one account at a provider do not prove account attribution

Media job record management now uses stable command identities, expected revisions and immutable history. Submitted reservations remain held past their local deadline. Provider submission, polling and billing reconciliation remain separate open work; record management alone is not a functioning HeyGen integration

## Inventory evidence

The existing inventory includes stored external credentials, reference-only groups, templates, application session secrets, encrypted material and public client configuration. A stored value is not automatically a verified API account. Preserve evidence and distinguish actual external credentials, credential components, archives, public identifiers and local secrets before routing anything. Keep private inventory counts, account metadata and credential locations outside this repository

## Local acceptance for the request explorer

The request audit is implemented and has been tested against a temporary PostgreSQL instance with a dedicated non-administrator test role. Historical UTC periods, all filters, decimal precision, missing counters, management/provider classification, retries, correction history and HTTP validation are covered by the regression suite. An actual Chromium run checks the UI, a 390px viewport, injection handling, corrections, pagination and failure states. Build and type checks use the repository's locked dependency versions. This is local feature acceptance, not evidence that every application or provider is already routed through the gateway

The next integration must project authenticated application identity at request time and confirmed deployment/account identity, then deploy readers before enabling the new producer. Do not infer historical identity from a key's current project assignment or guess an account from the selected model

## Local acceptance for media records

Eight media tests cover concurrent edits, immutable retry identity, imported baselines, atomic rejection of collector overwrites, reservation expiry, cross-account isolation, paginated HTTP reads, exact decimals and actual CLI subprocesses. The full companion suite passes 86 tests with a dedicated PostgreSQL database and no skips on this test host. Chromium and source-error browser regressions still pass. No vendor job was created and no live billing claim follows from these fixtures

## Provider precision and account isolation

OpenRouter collection now supports explicit independent account IDs. Both provider readers retain numeric JSON precision, reject unsafe key files and avoid exporting credentials. HeyGen v3 billing modes, null versus zero, credit resets, billing-mode changes, HTTP failures and bounded streaming reads are covered locally. No live HeyGen account report or routed application has been verified. The current static overview preserves supplied decimal strings; current budget remainders are calculated on the server with exact decimal arithmetic

The provider increment passes 96 companion tests on the isolated PostgreSQL host.
The Chromium regression now also exercises a long account name, an exact amount
beyond JavaScript integer precision, the spending-cap display and a 390px layout.
New provider, CLI and model modules pass strict type checks. The unchanged ten
strict-type diagnostics in the existing store module were reproduced against its
previous committed version; this increment does not claim a clean repository-wide
type check or a complete native gateway test run

## Native credential selection boundary

The native proxy request guard now rejects client-supplied
`litellm_credential_name` through its existing default banned-parameter path,
including the nested configuration and metadata locations that guard inspects.
Five regression cases reproduced the previous acceptance. After the fix, 148
related guard tests pass with network sockets disabled. Direct SDK resolution,
server-configured credential resolution and explicit proxy opt-ins are retained

The existing broad `allow_client_side_credentials` switch and a per-deployment
`configurable_clientside_auth_params` grant still permit name selection. Neither
provides a per-credential entitlement check. Scoped application routes must keep
those grants disabled for this parameter and select their credential on the
server. This fix covers the existing guard, not every protocol or custom auth
implementation. The complete native auth module did not finish in this harness;
only the relevant guard subset is claimed as passed. Native gateway changes need
a gateway rollout, independently of a companion release

## GitHub quota acceptance

The read-only GitHub collector preserves independent resource counters and reset
times, with response-header precedence and explicit request/point/unknown units.
Complete provider scopes supersede removed resources and outages. Seven new
collector tests cover null/zero/large integers, malformed data, credential safety,
header conflicts, redirects, account isolation, recovery and the actual CLI

The full companion suite passes 105 tests on the isolated PostgreSQL host. The
Chromium regression includes large GitHub request counts, a zero point balance,
unknown provider units and mobile layout. A real authenticated GET /rate_limit
was followed by an exact metadata-only import and curl readback from the local
HTTP companion backed by the dedicated test database. No provider token was
exported and no production service or account was changed. This verifies quota
observation, not per-request attribution or application routing


## Exact sum acceptance

Two regressions reproduced rounded dashboard totals while the PostgreSQL request
audit retained all digits, and lost account precision under a small caller
Decimal context. Shared exact arithmetic now covers gateway totals, imported
request costs, media reservations, OpenRouter subtraction and derived budget
remainders. Rational-number checks verify cancellation and widely separated
exponents without changing the caller's context. An HTTP regression compares the
account total with the SQL audit total; Chromium verifies the full budget
remainder string and mobile layout. The full companion suite passes 109 tests
on the isolated PostgreSQL host. No provider or production account was changed


## Application binding writer acceptance

Native project management is license-gated in the inspected source. Explicit
key-metadata binding provides application attribution independently of project
creation. A read-only inert-object probe in the deployed gateway confirmed that
authenticated metadata replaces client claims and absence clears them. No key
was created or changed by this probe

The new writer adds only the validated `user_api_key_application_id` scalar to
standard/spend metadata. Persistence derives it from the actual authenticated
key object, rejecting bare client scalars and JSON imitations even when a path
bypasses the pre-call sanitizer. Management target metadata cannot reassign the
calling application's identity. Key metadata is not immutable; issuance policy,
management-route restrictions and cache invalidation remain part of application
provisioning acceptance. No full application rollout is claimed

The local read-only producer now uses this scalar and leaves project-only rows
without an application assignment. No current key-table join is introduced.
Previously reported project-based assignments may be corrected on a future scan;
source history remains retained. This producer is not installed. Its original
project-as-app shortcut is superseded because a project may group several apps

The native identity/redaction subset passes 98 tests with network sockets disabled,
including request-time metadata snapshots after a later key edit. Five temporary-
PostgreSQL producer tests pass. Ruff passes and the shared identifier validator
passes strict type checking. A separate read-only baseline probe confirmed that
the deployed writer does not yet know this field and no stored nonempty value
exists there. This confirms the deployment dependency; the new attribution is not
live. The Opus /simplify consultation was conceptual, not a file or full-path audit


## Application provisioning preflight acceptance

The native admin-only capability endpoint reports the actual running custom-key
and client-credential policies and the installed writer/redaction contracts.
Ten ASGI HTTP tests verify role denial, boolean/string custom-key policy and
absence of configuration secrets. Two new regressions reproduced native virtual
key fields leaking before the fix; nested request bodies and error strings now
redact `key` and `keys`, with provider input and aliases preserved

The combined identity/redaction/capability subset passes 110 tests with ordinary
repository conftests and network sockets disabled (183 deselected). The ordinary
conftest harness stalled and is not claimed as passing. New endpoint and mapped
tests pass strict type checking; Ruff passes for changed endpoint and sanitizer
files. No application credential has been issued and no production service has
been updated by this work


## Scoped application issuer acceptance

The new prepare/provision CLI uses private per-application durable state, a
stable lock, dedicated internal users and explicit model/route scopes. Readback
verifies team membership and rejects privilege or credential override paths.
Fourteen tests exercise the actual controller and CLI, including timeouts after
commit, unresolved not-found outcomes, conflicting scope, later permission edits,
partial/privileged membership, competing controllers, symlinks, private file
modes, missing preflight and global/per-model override denial

The full companion suite passes 123 tests on isolated PostgreSQL. Controller
behavior tests cover 95% of its statements. New controller,
CLI and tests pass strict type checking and Ruff. Wheel and source distributions
build. A separate inert transport probe validates the generated user/key requests
against native NewUserRequest and GenerateKeyRequest and verifies that native
key-type processing preserves the explicit routes. Native route checks accept
the allowed inference path and reject management and unrelated inference paths.
A separate native authenticated-key enforcement probe passes one allowed route
and six denials, including a misleading path prefix.
These checks do not execute the native database-backed creation endpoints or
contact a provider, production gateway or application

No production key was issued. Credential delivery, rotation, revocation and
non-LLM grants are not claimed complete. The current deployed gateway lacks the
new capability endpoint and writer. An attempted conceptual Opus consultation
was rejected by automatic approval review before execution; no external review
is claimed for this controller


## Local credential delivery acceptance

The delivery command reuses the native scope inspection with creation disabled.
It writes only a verified scoped application key to a separate private file;
publication cannot replace another credential. Eight new behavior tests cover
private/idempotent handoff, missing users/keys without POSTs, later permission
changes, symlink/conflicting file refusal, public or locked destinations, matching
but unprotected files, and gateway timeout diagnostics. One of these tests runs
the actual CLI against a local HTTP fixture with administrative authentication
and checks the resulting private file and secret-free output

This is not a remote application rollout or provider-account verification.
Rotation and revocation remain open. The inspected native /key/block implementation
updates the row before cache invalidation; a revoked database row alone would not
prove that every cached credential was invalidated. Revocation acceptance must
cover this failure window rather than infer effective denial from readback alone

The full companion suite passes 131 tests on isolated PostgreSQL, including the
local HTTP CLI test. The application controller's statement coverage remains
95%. Changed controller, CLI and mapped tests pass strict type checking and
Ruff; wheel and source distributions build. No native source changed in this
step, and the existing native subset was not rerun. No production service,
application, gateway key or provider account was changed


## Recorded application revocation acceptance

The new revoke command persists intent before contacting the gateway and disables
preparation, provisioning and delivery from that state. Ownership checks use the
managed application operation, user and team. Blocking sends only the key hash
and is not prevented by widened model permissions or unsafe creation policies.
A journaled attempt is never repeated after a timeout; subsequent calls reconcile
its readback. The result distinguishes blocked/deleted database state from
runtime enforcement, which remains explicitly not_verified

Six new behavior tests cover normal/idempotent blocking and the local interlock,
timeout after commit, uncertain outcomes without replay, foreign ownership,
emergency blocking with changed permissions and unsafe policies, durable intent
during outages, and absent/archived key results. The local HTTP CLI test now
performs delivery followed by revocation, checking administrative authentication,
secret-free output, the stored result and exactly one hash-only block request

The full companion suite passes 137 tests on isolated PostgreSQL. A final focused
run covers the modified prepare interlock. Changed controller, CLI and tests pass
strict type checking and Ruff. A separate inert contract probe validates the
actual block request against native BlockKeyRequest and is_valid_api_key.
The existing native cache-eviction failure regression passes with repository
conftests and network sockets disabled (392 tests deselected), confirming the
best-effort limitation. No full native suite or live gateway revocation is claimed


## Generic pass-through provenance and header isolation acceptance

Before the fix, two request-to-spend regressions lost a valid authenticated
application ID, and eight header regressions reproduced configured header
replacement or mutation across requests. The corrected builder uses the shared
authenticated snapshot helper. The header helper returns a new mapping, preserves
configured values case-insensitively and retains unconfigured public options

The combined native capability, identity, spend redaction and pass-through subset
passes 145 tests with repository conftests and network sockets disabled (1043
deselected). This includes the actual spend payload writer after a later key
metadata edit, both client metadata carriers, absence of binding, forgery refusal,
private metadata exclusion, all new header cases and adjacent callback/trace and
budget regressions. A broader keyword selection also selected an unrelated
Transcribe synchronous TestClient test which hit its deadline; that class is
explicitly excluded from this acceptance claim. No complete native suite is claimed

Optional Google authentication, OpenTelemetry and Langfuse test dependencies were
installed only in the isolated native environment using the repository lockfile
versions and constraints. The header utility and capability endpoint pass strict
type checking. The new regression function bodies also pass isolated strict
checking, with explicit legacy-boundary annotations. These checks do not establish
that the entire legacy test modules pass strict typing. Ruff passes the changed
source, capability tests and generic pass-through test module. The older provider
test module has 39 pre-existing lint diagnostics; an exact baseline comparison
found no additions. No lint/type budget was edited

No companion code, database schema, production gateway, route, credential or
application was changed. Provider-account selection and actual non-LLM application
wiring remain open. Native gateway rollout and live protocol verification are
required before these protections can be relied on in production
