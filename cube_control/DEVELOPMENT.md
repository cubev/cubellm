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


## Native named account binding acceptance

Generic HTTP routes now reference the existing native named credential store and
require matching account metadata and an explicit allowed-origin list. Requests
resolve the selected credential once; an immutable server-held snapshot supplies
the account, origin and provenance to the native spend writer. Client metadata
cannot assert those fields or mark provider cost as verified. Application identity
continues to come from the actual authenticated key snapshot. Credential names,
values and the internal binding object do not enter stored spend metadata.

Before the privacy prerequisite, four masking regressions reproduced disclosure
through arbitrary field names, nested/nonstring values and zero visible-character
settings. The masking helper now honors its all-values switch. Credential list,
name and model reads mask every value with zero visible characters. ASGI checks
cover list/name reads, retention of nonsecret account metadata and unchanged
underlying values.

The final focused account/privacy/transport selection passes 44 tests (925 deselected).
It covers missing/mismatched credentials, SecretStr and environment rotation,
origin allowlists/default-port normalization, immutable per-request selection,
route retargeting, unsafe configuration, client overrides, a JSON-envelope query
credential bypass, and the actual native HTTP relay plus asynchronous spend
callback. A combined native security/capability/provenance selection passes 111
tests (1153 deselected), including six redirect/isolation cases for GET, JSON,
raw, multipart, streaming JSON and streaming multipart. These cases deliberately
use a pooled client carrying another account's authentication, cookies, default
headers and query credentials. Bound requests inherit none of them and do not
follow the response redirect. Removing request isolation deliberately makes all six
regressions fail; restoring it makes all six pass. Another 37 adjacent native
route, multipart, raw-body and existing application snapshot tests pass (482
deselected).

One initial combined run exposed an observer counting a revived callback from
an unrelated earlier test. The observer now selects its fixture application's
records, and the combined rerun passes. Existing mocked-worker unawaited-coroutine
warnings and an unrelated Langfuse fixture's export retries remain visible;
no real telemetry destination or complete native suite is claimed. Repository
conftests and network sockets are disabled for unit acceptance. The combined
selection excludes the previously identified synchronous Transcribe fixture and
external httpbin tests.

The new account module, its complete mapped tests, native capabilities and mapped
capability tests pass strict typing. The new relay fixture and function bodies
also pass isolated strict checking; the entire older relay test module was not
claimed type-clean. Pure tests for the new account module mirror it under
`tests/unit/proxy/pass_through_endpoints`; relay regressions extend the existing
mapped pass-through test file. Changed source and focused tests pass Ruff;
an exact diagnostic comparison finds no additions in modified legacy files.
The older logging test module retains its 11 baseline diagnostics. No lint/type
budget was changed. The native package now requires a Rust/maturin build with its
workspace and toolchain, which are absent from this checkout/environment; no
native wheel/image build or production compatibility is claimed. Companion code,
schema and browser code did not change, and their suites were not rerun here.

A separate authorized read-only proof sends two actual GitHub GETs, /user and
/rate_limit, through the native relay and asynchronous spend callback. The first
observes the token owner's identity; the second captures quota resources with the
owner-based account identifier. Both retain application/account/origin metadata
and explicitly unverified provider cost. The existing CLI token remains only in
memory; the private report contains minimal identity, quota and request metadata.
Gateway authentication is a development fixture, not native database-backed key
authentication. No provider token, private inventory identity or report is
committed. This is not central enrollment, invoice reconciliation or deployment.

Production native/companion services, credentials, routes and applications are
unchanged. Production artifact compatibility, account-by-account enrollment,
non-LLM application grants, provider-specific usage/billing reconciliation and
actual LAN/Tailscale activation remain open under the full project goal.


## Native-to-audit account provenance acceptance

Native credentials can now carry `cube_provider_id` in addition to their account
and allowed origins. The resolver captures that label with the same request-time
credential snapshot. The shared metadata helper clears client provider claims,
and the spend writer independently derives `provider_account_provider` from the
actual binding object. The new native capability flag is
`passthrough_account_provider: credential_metadata_v1`.

The companion owns a shared whitelist SQL projection for native spend rows. Its
`native-gateway-projection` CLI exports the versioned column contract, and
`project-native-gateway` validates the resulting rows using the existing scan
preparation, rejected-row counts and import journal semantics. Account leaves
require the server source marker. Recorded target and origin must agree. Explicit
captured provider labels support custom API domains without model/provider guessing;
known vendor origins cannot be relabeled. Unsupported or contradictory rows are
rejected individually. Catalogue matching still prevents cross-account imports.
Bound generic HTTP placeholder token/cache zeros become unknown, and provider cost
remains explicitly unverified. Gateway-reported amounts retain their separate basis.

An additive account_evidence record reaches usage pages and immutable correction
history. Origin, source, captured provider and cost status appear in the browser.
The new optional fields are omitted when absent, including nested absent provider
labels; legacy record/batch serialization remains byte-compatible. A PostgreSQL
regression seeds an already-confirmed scan with the original fingerprint and
verifies duplicate acknowledgment. Deliberately adding the null field makes that
regression fail; restoring field exclusion makes it pass. A model invariant rejects
account evidence without an account or with a conflicting record provider.

The full companion suite passes 147 tests against isolated PostgreSQL. Native
projection tests execute the real SQL over temporary native-shaped tables, verify
arbitrary provider labels, untrusted scalar/target refusal, missing measurements,
secret-free output, CLI contract export/projection without database configuration,
actual catalogue-checked imports, filtered usage HTTP responses and correction
history. The native combined security/capability/provenance selection passes 115
tests (1153 deselected), including invalid-provider and snapshot cases. The actual
native relay/callback fixture retains the vault's provider label and the real
outbound URL even when the request supplies forged provider and api_base values.
An initial URL assertion omitted the fixture's default query; it was corrected to
include that query. No URL-forgery vulnerability or fix is claimed from that assertion.

Chromium checks pass desktop and 390px views, explicit captured-provider/account
proof in current rows and history, absent proof on old rows, exact decimal values,
unknowns, pagination, stale snapshots, XSS refusal and outages. Node source-error
checks also pass. The source distribution and companion wheel build; a fresh
installation of that wheel exports the SQL contract, projects a custom-provider
row with unknown counters and includes the new UI asset. Changed runtime modules,
new projection tests, browser harness, native account module and mapped pure/capability
tests pass strict typing and Ruff. New native relay fixture bodies also pass isolated
strict checking. No repository-wide native typing claim or budget change is made.

The companion declares Pydantic >=2.12 for field-level exclude_if. This preserves
the record's API schema as well as legacy serialization. No database schema rewrite
is required. No native gateway wheel/image was built in this step, and no production
service, provider token, gateway route, key, collector or application was changed.
The existing resident producer still uses its older projection until a coordinated
native/companion/source activation. Actual enrollment, vendor billing/usage parsers,
real media jobs, LAN/Tailscale, rotation and verified distributed revocation remain
open under the full all-account goal.


## Native SDK release artifact acceptance

The pinned Rust workspace is now checked out and builds with Rust 1.98.0 and
Maturin 1.15.0. The Rustup installer and backend wheel were checksum-verified,
and the release build used the unchanged Cargo lockfile and release profile.
The result is a cp310-abi3 manylinux_2_39_x86_64 SDK wheel with a 39.58 MB native
extension, below the existing 40 MB limit. Release stripping, importability,
absence of the panic test hook and tokenizer vocabulary checks pass

The existing Linux verifier previously rejected every manylinux filename even
when all its other checks passed. It now accepts Linux x86_64 and standardized
manylinux tags, including compressed aliases, and compares their declared libc
floor with the numeric GLIBC requirements observed by readelf. ABI, architecture,
metadata, debug and size checks remain enforced. Eight new acceptance cases fail
before the fix; all 28 verifier cases pass afterward. Nine rejection cases cover
understated libc versions, missing measurements and unsupported platforms.
Bypassing the libc comparison makes five of those regressions fail. Compatibility
tag definitions follow [PEP 600](https://peps.python.org/pep-0600/)

The new account policy imported typing.Self despite the SDK's Python 3.10 floor.
It now uses typing_extensions.Self, already supplied by the SDK dependencies.
Strict checking of that module targeting Python 3.10 changes from four errors
to none. This is a source compatibility check, not a claim that every SDK route
was executed on Python 3.10

A fresh environment installs the wheel while reusing the isolated test dependency
set. Its interpreter runs with -I; a provenance check before collection and after
tests requires both the SDK and native module to reside in that installed
environment. All 115 selected account, capability, identity and gateway security
tests pass there. The actual native tokenizer suite passes 17 tests with network
sockets disabled and checksum-verified reference data prepared beforehand. A
minimal asynchronous counter times out in the restricted command runner and
passes in a normal local process; the native suite uses that normal environment.
No Rust runtime change is inferred or introduced for this runner difference

The repository's standalone native wheel route check also passes against its
local recording server, covering synchronous and asynchronous requests, rate
limits, 32 concurrent calls and cancellation by SIGINT. These use synthetic
credentials and make no real provider requests. Changed verifier code and tests
pass strict typing and Ruff. No lint/type budget was modified

A read-only inspection of the existing gateway observes CPython 3.13, x86_64 and
a GIL-enabled interpreter. Its deployed schema file and all 189 applied migration
checksums match this checkout, with no unfinished, rolled-back or missing
migration and all required spend projection columns present. No account records,
provider credentials or spend rows were read, and no schema was applied. These
checks establish the inspected schema prerequisites, not complete runtime parity

The SDK wheel contains no compiled native Admin UI bundle. A complete gateway
image with a UI built from the same source and execution on its target Python
3.13 runtime remains open. No production image, service, collector, application
or credential was changed. The full all-account, precise-usage and LAN/Tailscale
objective remains active


## Native gateway image build corrections

Regenerating the dashboard API types adds the account credential schema and
Cube Control capability endpoint to the native Admin UI contract. The lazy
OpenAPI fragments regenerate without a diff. The UI builds with Node 24.19.0
from the pinned image, including TypeScript checking and the static export

The full Docker build exposed two existing dependency problems. The unqualified
Rust package resolved to 1.99, while rust-toolchain.toml specifies 1.98.0. The
builder now reads that version from the committed file and verifies the official
Rust distribution against its published SHA-256 sidecar before installation.
Both rustc and cargo report 1.98.0 in the build. BusyBox tar cannot directly
decode this valid Rust XZ archive; explicit xz decompression followed by tar
succeeds against the same verified bytes

PgBouncer 1.25.2 fails to compile against the current OpenSSL 4 headers because
it accesses an opaque ASN.1 structure. The Dockerfile pins PgBouncer 1.26.1 and
its official source checksum. The documented upstream fix addresses that exact
compiler failure, and the new binary builds against the existing OpenSSL 4
packages. See the [PgBouncer changelog](https://www.pgbouncer.org/changelog.html)

The complete x86_64 image builds and runs with CPython 3.13.18 and glibc 2.44.
Its installed SDK contains the rebuilt UI export, with 728 files including 184
JavaScript files. The native extension is 39,630,848 bytes, below the unchanged
40 MB limit, and does not expose the panic test hook. The actual PgBouncer binary
reports 1.26.1 with OpenSSL 4.0.3; Prisma selects and executes its baked query
engine with networking disabled

An independent test image adds only offline, version-pinned test dependencies,
never SDK source. The installed SDK/native provenance, packaged UI, native async
counter and the existing account/capability/relay/privacy selection pass 118 tests
on Python 3.13, with 1153 unrelated tests deselected. The existing PgBouncer suite
passes 65 tests. Network sockets are disabled except required local sockets, and
the containers have no external network. Existing AsyncMock warnings and a blocked
fake telemetry exporter remain visible; no real provider traffic is claimed

A separate probe runs the original runtime image through its actual production
entrypoint with a synthetic local key and no provider keys or database URL. Live
HTTP checks pass for health, authenticated capability flags, denial of unauthenticated
capability access, the Admin UI HTML and an exported JavaScript asset. This proves
local startup and these HTTP contracts, not database writes, provider billing,
real account ownership or production deployment

These corrections and the image proof do not complete account enrollment,
application method/account grants, authoritative provider usage and billing,
LAN/Tailscale activation, credential rotation or distributed revocation. No
production image, service, provider credential, collector or application has changed


## Request-time application account grants

Named HTTP account routes now require an explicit application grant before the
gateway resolves a provider credential. Each grant matches the authenticated
application ID, native application-key hash, account, exact proxy path and HTTP
method. Binding the native key hash prevents self-service application metadata
from adopting another key's access. Client JSON identity/hash claims do not supply
this authenticated value. The existing native route permissions still apply

Missing grants deny callers with 403, including administrator keys. Existing
named-account routes must receive explicit grants before this version is
activated. Grant configuration rejects ambiguous paths and repeated or implicit
methods. Retargeting a policy to another account requires updating its grants.
The ASGI route helper supplies the path with deployment root stripping; Host
headers do not select the grant path. Configuration writes retain the existing
proxy-admin gate. Native capabilities expose the versioned grant contract, and
the dashboard schema is regenerated from the backend

The initial missing-grant regression returned 503 from credential lookup before
the fix and now returns 403 first. The combined native account, capability,
credential, relay, logging and provenance selection passes 164 tests, with 1153
unrelated tests deselected. Eighteen ASGI/recording-transport combinations verify
application ID, actual key hash, method and exact path, including forged request
metadata and zero upstream calls after denial. A previous test event shared an
application ID with the audit fixture; the new matrix now owns a separate ID,
preserving the audit fixture's exact-one-record assertion

All nine targeted security mutations are detected, covering application/key/path/
method checks, default denial, account retargeting, path/method validation and the
runtime gate. Restored source passes the combined selection. Strict Python 3.10
checking of the pure policy, capability module and mapped policy tests passes;
Ruff passes for every changed Python file. Existing AsyncMock and blocked fake
telemetry warnings remain visible. No lint/type budget changed

Direct dashboard tsc reports 957 diagnostics both with the previous generated
schema and with this schema, at identical locations and error codes. One diagnostic
differs in its truncated type display, so exact diagnostic text identity is not
claimed. The generated schema is restored after comparison and includes the
required key_hash. This is not a clean repository-wide UI type check

This is source acceptance. The previously built image still represents a0724de
and does not include these new grants. Automated non-LLM key/route provisioning,
dynamic-resource scopes, real account/application acceptance, the next packaged
image and coordinated native/control/collector activation remain open. No live
route, application key, provider credential or production service was changed
