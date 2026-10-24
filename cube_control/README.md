# CubeLLM Control

A separate account and operations service alongside the existing LiteLLM gateway, with no changes to gateway request handling or its database

The account catalogue covers OpenAI, Anthropic, OpenRouter, xAI, HeyGen and Suno. API accounts and subscriptions are separate records. A catalogue entry does not claim a working provider integration

## Data contracts

Observations carry source, observation time, collection time, reporting period, unit, status and freshness. Unknown values have no numeric amount. Credits, ratios, tokens and currencies cannot be substituted for each other. Provider-reported spending and observed proxy requests remain separate values, so overlapping data is never summed

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

The initial service consumes existing collectors through an authenticated management bridge. Native provider connectors, account-editing authentication, gateway-event ingestion, deployment-job execution and persistent mobile agents remain separate implementation work. Unknown provider balances and subscription states stay unknown until a real source or explicit manual entry is imported

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
