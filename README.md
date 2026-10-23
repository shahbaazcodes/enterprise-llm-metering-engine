# Enterprise LLM Metering Engine

A production-grade **metering and billing plane** for LLM traffic. It sits in
front of any model provider, computes the exact cost of every request, enforces
the tenant's commercial contract (entitlement, spend cap, request rate) and
writes an immutable, auditable ledger.

| Guarantee | How it is delivered |
| --- | --- |
| **Exact money** | Every amount is an `int` count of *micro-cents* (1 USD = 1 000 000 µ¢). No floats, no IEEE-754 drift. |
| **Exactly-once billing** | Redis `SET NX EX` lock + a partial unique index on `idempotency_key`. A retried request is billed once. |
| **Two-speed rejection** | `402 Payment Required` = no entitlement. `429 Too Many Requests` = entitlement ok, quota exhausted. |
| **Atomic quotas** | The check *and* the mutation run inside a server-side Redis Lua script, so they can never interleave. |
| **Anomaly defence** | A per-tenant `IsolationForest` (with a global fallback) flags runaway loops, DDoS spikes and token floods. |
| **Observable** | Prometheus counters/histograms with bounded label cardinality; `tenant` is never a label. |

---

## Repository layout

```
enterprise-llm-metering-engine/
├── app/                         # Production application package
│   ├── routers/                 # FastAPI endpoint routes (generate.py, usage.py, health.py, webhooks.py)
│   ├── __init__.py
│   ├── calculator.py            # Micro-cent token pricing calculator
│   ├── db.py                    # Async SQLAlchemy engine & session pool
│   ├── deps.py                  # FastAPI dependency injection
│   ├── exceptions.py            # Custom HTTP exceptions
│   ├── langchain_interceptor.py # LangChain/LangGraph token interceptor
│   ├── main.py                  # FastAPI app factory
│   ├── metering.py              # Redis rate limiter & quota manager
│   ├── metrics.py               # Prometheus instrumentation
│   ├── ml_guard.py              # IsolationForest anomaly detector
│   ├── models.py                # SQLAlchemy 2.0 ORM ledger models
│   ├── pricing.py               # Price card resolution
│   ├── providers.py             # Simulated LLM provider
│   ├── redis_client.py          # Redis connection pool
│   ├── s3_audit.py              # AWS S3 / LocalStack audit archiver
│   ├── schemas.py               # Pydantic v2 request / response contracts
│   ├── settings.py              # Pydantic v2 configuration
│   └── stripe_service.py        # Stripe webhook verification
├── db/
│   └── migrations/
│       └── 001_init.sql         # PostgreSQL database schema & initial seed
├── eval/                        # Evaluation test suite & harness
│   ├── __init__.py
│   ├── conftest.py              # Shared test fixtures & backend resolver
│   ├── eval.py                  # Capstone matrix runner
│   ├── eval_set.json            # Matrix evaluation scenarios
│   ├── test_calculator.py
│   ├── test_metering.py
│   └── test_stripe.py
├── .env.example
├── .gitignore
├── BUILDLOG.md                  # Development & architectural decision log
├── Dockerfile                   # Multi-stage production image
├── EVIDENCE.md                 # Evaluation verification output
├── capstone.yaml                # Evaluator manifest
├── deploy_aws.sh                # AWS deployment automation script
├── docker-compose.yml           # Local infrastructure stack (Postgres, Redis, LocalStack)
├── pyproject.toml               # Tooling & ruff configuration
└── README.md                    # Architecture & quickstart documentation
```

> **Note on `app/schemas.py`** — the module holds the Pydantic v2 request/response
> contracts. It is shared by `app/routers/*` **and** `app/providers.py`, so it
> lives at the `app/` package level rather than inside `routers/`: a provider
> must never depend on the HTTP layer.

---

## Architecture

```
                      ┌───────────────────────────────┐
   client ──HTTP──▶   │  FastAPI  (app/main.py)       │
                      │  ├── POST /v1/generate        │
                      │  ├── GET  /v1/usage           │
                      │  ├── POST /v1/webhooks/stripe │
                      │  ├── GET  /health             │
                      │  └── GET  /metrics            │
                      └───────┬───────────────┬───────┘
                              │               │
             ┌────────────────▼──┐        ┌───▼───────────────┐
             │ Redis 7           │        │ PostgreSQL 16     │
             │ ├── sliding window│        │ ├── usage_events  │
             │ ├── spend reserve │        │ ├── plans/tenants │
             │ └── idem. lock    │        │ └── price book    │
             └───────────────────┘        └───────────────────┘
```

### Request pipeline (`POST /v1/generate`)

Each stage is a hard gate; the request stops at the first one that fails.

```
tenant auth ─▶ idempotency lock ─▶ entitlement (402)
   ─▶ rate limit (429) ─▶ spend reservation (429) ─▶ provider call
   ─▶ cost settlement ─▶ anomaly guard ─▶ ledger write ─▶ response
```

Money is **reserved before** the provider is called and **settled after**, so
a tenant can never exceed its cap even if the provider returns more tokens
than estimated.

### Money model

```
1 USD = 1_000_000 micro-cents
prices are quoted per 1_000_000 tokens (the industry billing unit)

$2.50 / 1M input tokens  ->  2_500_000 micro-cents / 1M
1_000 input tokens       ->  1000 * 2_500_000 / 1_000_000  =  2_500 µ¢
```

Each cost component is evaluated as a single rational number and rounded
**exactly once** (round-half-up, away from zero). `app/calculator.py` is pure —
no I/O, no database, no floats — which makes it trivially unit-testable and
impossible to accidentally introduce rounding drift.

### Concurrency model

Every state transition that must be race-free executes *inside Redis*:

| Script | Guarantees |
| --- | --- |
| `LUA_SLIDING_WINDOW` | Prunes the expired entries of a sorted set and appends the current request in one atomic step (true sliding-window log, O(log n), sub-millisecond). |
| `LUA_RESERVE_SPEND` | Reads period spend + reservations, compares against the plan cap and reserves the amount atomically. |
| `LUA_SETTLE_SPEND` | Releases the reservation and books the actual cost atomically. |
| `LUA_RELEASE_LOCK` | Compare-and-delete: a slow request can never delete a lock already handed to someone else. |

---




## Quickstart

### 1. Requirements

* Python **3.11+** (developed and verified on 3.13)
* Docker + Docker Compose
* `pytest`, `pytest-asyncio`, `fakeredis` + `lupa`, `aiosqlite`, `ruff` (dev extras)

### 2. Install

```bash
cd enterprise-llm-metering-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### 3. Start the local infrastructure

```bash
docker compose up -d
docker compose --profile tools run --rm createbuckets   # provisions the S3 audit bucket
```

This brings up **PostgreSQL 16**, **Redis 7** and **LocalStack** (S3). Host
ports are parameterised via `POSTGRES_PORT` / `REDIS_PORT` / `LOCALSTACK_PORT`
so the stack can coexist with other instances already running on the host.

### 4. Run the service

```bash
uvicorn app.main:app --reload
# or: llm-metering
```

### 5. Run the Streamlit frontend

```bash
streamlit run ui/app.py --server.port 8501
```

The dashboard opens at <http://localhost:8501>. Configure the API base URL in the
sidebar to point at your FastAPI backend (defaults to `http://localhost:8000`).

The application lifespan applies pending SQL migrations (checksum-tracked in
`schema_migrations`) and seeds the plan catalogue and the price book.

### 5. Smoke test

```bash
curl -s localhost:8000/health | jq
# {"status":"ok","database":"up","redis":"up","version":"1.0.0",...}

curl -s -X POST localhost:8000/v1/generate \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: default-tenant' \
  -d '{"prompt":"hello","model":"gpt-4o","simulated_usage":{"input_tokens":1000,"output_tokens":500}}' | jq
```

Interactive API docs: <http://localhost:8000/docs>

---



## Configuration

All configuration is environment-driven and parsed once per process
(`app/settings.py`, Pydantic v2 + `pydantic-settings`). See `.env.example` for
the fully commented template. The most important knobs:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://…` | Async SQLAlchemy DSN. |
| `REDIS_URL` | `redis://localhost:6379/0` | Locks, sliding window, spend counters. |
| `API_KEY_REQUIRED` | `false` | Require `Authorization: Bearer <key>` next to `X-Tenant-ID`. |
| `ALLOW_USAGE_OVERRIDE` | `true` | Let a caller pin token counters (benchmarking). **Force-disabled in production.** |
| `FREE_MONTHLY_LIMIT_MICROCENTS` | `5000000` | Free plan spend cap ($5.00). `0` = unlimited. |
| `PRO_MONTHLY_LIMIT_MICROCENTS` | `500000000` | Pro plan spend cap ($500.00). |
| `CACHED_INPUT_DISCOUNT_BPS` | `2500` | Cached input is billed at 25 % of the fresh input rate. |
| `ML_GUARD_ENABLED` | `true` | Enable the IsolationForest anomaly detector. |
| `STRIPE_WEBHOOK_SECRET` | — | HMAC secret for the webhook signature. |

> **Security:** `Settings.model_post_init` hardens the configuration when
> `ENVIRONMENT=production` — the usage override is switched off and the API key
> becomes mandatory, so a caller can never choose their own price.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/generate` | Billable LLM completion. Honours `Idempotency-Key`. |
| `GET` | `/v1/usage` | Tenant rollup + per-model cost breakdown for a billing period. |
| `POST` | `/v1/webhooks/stripe` | Signature-verified Stripe subscription sync. |
| `GET` | `/health` | Dependency health probe (database + Redis). |
| `GET` | `/metrics` | Prometheus exposition format. |

The usage report is a **reporting view over the immutable ledger**: the same
micro-cent integers charged at request time are re-aggregated, so the report
can never disagree with the invoice.

---

## Testing & evaluation

The suite is hermetic by construction:

* **PostgreSQL** → a throw-away SQLite file built from the ORM metadata.
* **Redis** → `fakeredis` compiled against `lupa`, so the server-side Lua
  scripts execute in a *real* Lua interpreter instead of being stubbed out.
* **Provider** → the deterministic simulator that ships with the service.

```bash
pytest eval/ -v            # full suite
python3 eval/eval.py       # capstone matrix runner -> refreshes EVIDENCE.md
```

The runner maps the pytest results back onto the scenario matrix in
`eval/eval_set.json` and regenerates `EVIDENCE.md`. Probes marked
`live_redis` are additionally verified against the real Redis 7 container
(`docker compose up -d`) to prove the atomicity of the distributed lock on the
production engine rather than on a simulator.

Current status: **44 test cases, 5 probes, 100 % pass rate.**

---

## Observability

Prometheus metrics are defined in `app/metrics.py` with deliberately bounded
label cardinality:

* `route` is the *matched* FastAPI route template, never a raw URL;
* `model` is limited to the models present in the price book;
* `tenant` is **never** used as a label — per-tenant reporting is served by
  the `/v1/usage` API instead.

---

## Deployment

### Container image

The `Dockerfile` is a **multi-stage** build: the builder stage resolves the
(scientifically heavy) dependency tree into a virtualenv, and the runtime stage
carries only that virtualenv — no compiler, no build toolchain, and a
non-root user (`metering`, uid 10001). A `HEALTHCHECK` polls `/health`.

```bash
docker build -t llm-metering:latest .
docker run --rm -p 8000:8000 --env-file .env llm-metering:latest
```

> **Running the container against the compose stack?** `localhost` inside a
> container refers to the container itself. Point the data stores at the host
> with `host.docker.internal` (Docker Desktop / Docker Engine ≥ 20.10):
>
> ```bash
> docker run --rm -p 8000:8000 --env-file .env \
>   -e DATABASE_URL=postgresql+asyncpg://metering:metering@host.docker.internal:55432/metering \
>   -e REDIS_URL=redis://host.docker.internal:56379/0 \
>   -e S3_ENDPOINT_URL=http://host.docker.internal:45466 \
>   llm-metering:latest
> ```

### AWS rollout

`deploy_aws.sh` automates the AWS rollout: it builds the container image,
pushes it to ECR, provisions the supporting resources (RDS, ElastiCache, S3,
Secrets Manager) and drives the ECS Fargate service.

```bash
./deploy_aws.sh plan --dry-run     # review the resolved plan, touch nothing
./deploy_aws.sh all --tag v1.0.0   # build -> push -> migrate -> deploy
./deploy_aws.sh status              # service, RDS and ElastiCache state
./deploy_aws.sh rollback --confirm  # redeploy the previous task definition
```

Every billable or destructive step is a no-op under `--dry-run`, and
`destroy` / `rollback` require an explicit `--confirm`.

---

## Further reading

* `BUILDLOG.md` — the development & architectural decision log (why each
  significant design choice was made).
* `EVIDENCE.md` — the machine-generated evaluation evidence.
* `capstone.yaml` — the evaluator manifest.
* `db/migrations/001_init.sql` — the authoritative PostgreSQL schema.

---
