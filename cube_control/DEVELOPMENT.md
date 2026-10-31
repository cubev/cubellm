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

Prioritize the gateway audit surface and reliable application/account attribution. Expose request-level records, reporting periods, model and counter details, missing values and correction history. Provider billing and gateway-reported charges remain separate. Application identity must come from authenticated gateway identity, and account identity from a confirmed server-controlled deployment mapping. Model names, arbitrary client metadata and the existence of one account at a provider do not prove account attribution

Media job record management now uses stable command identities, expected revisions and immutable history. Submitted reservations remain held past their local deadline. Provider submission, polling and billing reconciliation remain separate open work; record management alone is not a functioning HeyGen integration

## Inventory evidence

The existing inventory includes stored external credentials, reference-only groups, templates, application session secrets, encrypted material and public client configuration. A stored value is not automatically a verified API account. Preserve evidence and distinguish actual external credentials, credential components, archives, public identifiers and local secrets before routing anything. Keep private inventory counts, account metadata and credential locations outside this repository

## Local acceptance for the request explorer

The request audit is implemented and has been tested against a temporary PostgreSQL instance with a dedicated non-administrator test role. Historical UTC periods, all filters, decimal precision, missing counters, management/provider classification, retries, correction history and HTTP validation are covered by the regression suite. An actual Chromium run checks the UI, a 390px viewport, injection handling, corrections, pagination and failure states. Build and type checks use the repository's locked dependency versions. This is local feature acceptance, not evidence that every application or provider is already routed through the gateway

The next integration must project authenticated application identity at request time and confirmed deployment/account identity, then deploy readers before enabling the new producer. Do not infer historical identity from a key's current project assignment or guess an account from the selected model

## Local acceptance for media records

Eight media tests cover concurrent edits, immutable retry identity, imported baselines, atomic rejection of collector overwrites, reservation expiry, cross-account isolation, paginated HTTP reads, exact decimals and actual CLI subprocesses. The full companion suite passes 86 tests with a dedicated PostgreSQL database and no skips on this test host. Chromium and source-error browser regressions still pass. No vendor job was created and no live billing claim follows from these fixtures

## Provider precision and account isolation

OpenRouter collection now supports explicit independent account IDs. Both provider readers retain numeric JSON precision, reject unsafe key files and avoid exporting credentials. HeyGen v3 billing modes, null versus zero, credit resets, billing-mode changes, HTTP failures and bounded streaming reads are covered locally. No live HeyGen account report or routed application has been verified. The current static overview preserves supplied decimal strings; its optional remaining-budget calculation is labelled rounded

The provider increment passes 96 companion tests on the isolated PostgreSQL host.
The Chromium regression now also exercises a long account name, an exact amount
beyond JavaScript integer precision, the spending-cap display and a 390px layout.
New provider, CLI and model modules pass strict type checks. The unchanged ten
strict-type diagnostics in the existing store module were reproduced against its
previous committed version; this increment does not claim a clean repository-wide
type check or a complete native gateway test run
