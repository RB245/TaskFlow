# TaskFlow

A task management API with background notifications, a cache-backed task feed, and operational endpoints.

FastAPI · PostgreSQL 16 · Redis 7 · Celery (worker + beat) · SQLAlchemy 2.0 · Alembic

---

## Contents

- [Quick start](#quick-start)
- [Running the tests](#running-the-tests)
- [API](#api)
- [Architecture](#architecture)
- [Where the depth went: caching](#where-the-depth-went-caching)
- [Authentication](#authentication)
- [Authorization](#authorization)
- [Background jobs](#background-jobs)
- [Operations](#operations)
- [Deployment](#deployment)
- [Assumptions I made](#assumptions-i-made)
- [Tradeoffs and what I'd do with more time](#tradeoffs-and-what-id-do-with-more-time)

---

## Quick start

From a clean clone, one command:

```bash
docker compose up --build
```

That brings up Postgres, Redis, a one-shot migration job, the API, a Celery worker and Celery beat. The API waits for migrations to finish, so the first request already hits a migrated schema.

- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>
- Metrics: <http://localhost:8000/metrics>

**Port already in use?** Every published port is overridable, because 5432/6379/8000 are commonly occupied:

```bash
DB_PORT=55433 REDIS_PORT=56380 API_PORT=8001 docker compose up --build
```

**Secrets.** `JWT_SECRET` defaults to a development placeholder so `up` works with no setup. For anything deployed, set a real one:

```bash
export JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(64))")
```

### Five-minute tour

```bash
# 1. Register
curl -sX POST localhost:8000/api/v1/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"ada@example.com","full_name":"Ada","password":"correct-horse-battery"}'

TOKEN=...   # tokens.access_token from the response above

# 2. Create a project
curl -sX POST localhost:8000/api/v1/projects \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"Apollo"}'

# 3. Create a task that is already overdue
curl -sX POST localhost:8000/api/v1/tasks \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"project_id":"<PROJECT_ID>","title":"Ship it","due_date":"2020-01-01T00:00:00Z"}'

# 4. Within 60s, beat sweeps and the worker creates the notification
curl -s localhost:8000/api/v1/notifications -H "authorization: Bearer $TOKEN"
```

### Running without Docker

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env

docker compose -f docker-compose.test.yml up -d     # just Postgres + Redis
alembic upgrade head
uvicorn app.main:app --reload
```

The worker and beat are separate processes:

```bash
celery -A app.workers.celery_app.celery_app worker --loglevel=info
celery -A app.workers.celery_app.celery_app beat   --loglevel=info
```

A `Makefile` wraps the common commands — `make help` lists them.

---

## Running the tests

```bash
docker compose -f docker-compose.test.yml up -d    # or: make services
pytest                                             # or: make test
```

98 tests, 95% line coverage.

```
tests/test_auth.py      authentication, hashing, token rotation and replay
tests/test_authz.py     authorization boundaries, written from the attacker's side
tests/test_projects.py  project CRUD and membership
tests/test_tasks.py     task CRUD, every filter, sorting and pagination
tests/test_cache.py     cache correctness and degradation      <- the important one
tests/test_jobs.py      background job triggering and idempotency
tests/test_ops.py       health, readiness, metrics, request ids
```

Tests run against a **real** Postgres and a **real** Redis. The cache tests depend on genuine `INCR`/`MGET` semantics and on how a pipeline behaves when the connection fails; a fake would happily let a broken invalidation strategy pass. The schema is built by running the actual Alembic migrations, so a migration that has drifted from the models fails in CI rather than at deploy time.

---

## API

All endpoints are under `/api/v1` except the operational ones.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/auth/signup` | Returns user + token pair |
| `POST` | `/auth/login` | |
| `POST` | `/auth/refresh` | Rotates the refresh token |
| `POST` | `/auth/logout` | Revokes a refresh token; idempotent |
| `GET` | `/auth/me` | |
| `GET` | `/projects` | Paginated; only projects you belong to |
| `POST` | `/projects` | Creator becomes owner |
| `GET` | `/projects/{id}` | Includes the member list |
| `PATCH` | `/projects/{id}` | Owner only |
| `DELETE` | `/projects/{id}` | Owner only; cascades to tasks |
| `POST` | `/projects/{id}/members` | Owner only; by email |
| `DELETE` | `/projects/{id}/members/{user_id}` | Owner only |
| `GET` | `/tasks` | **Filtered, paginated, cache-backed** |
| `POST` | `/tasks` | |
| `GET` | `/tasks/{id}` | |
| `PATCH` | `/tasks/{id}` | Triggers notifications and invalidation |
| `DELETE` | `/tasks/{id}` | |
| `GET` | `/notifications` | Yours only; `?unread_only=true` |
| `GET` | `/notifications/unread-count` | |
| `POST` | `/notifications/{id}/read` | |
| `GET` | `/health` | Liveness |
| `GET` | `/health/ready` | Readiness; checks DB and Redis |
| `GET` | `/metrics` | Prometheus text format |

### `GET /tasks` parameters

| Parameter | Type | Notes |
|---|---|---|
| `project_id` | uuid | Omit to search every project you belong to |
| `status` | enum, repeatable | `todo` / `in_progress` / `done` |
| `assignee_id` | uuid | |
| `unassigned` | bool | Mutually exclusive with `assignee_id` |
| `due_from`, `due_to` | datetime | Inclusive; URL-encode the `+00:00` offset |
| `q` | string | Case-insensitive title match |
| `sort` | enum | `due_date`, `-due_date`, `created_at`, `-created_at`, `updated_at`, `-updated_at`, `title`, `-title` |
| `page`, `page_size` | int | `page_size` capped at 100 |

Unknown query parameters are **rejected with a 422**, not ignored. Silently returning everything because someone typed `?staus=done` is the kind of bug that reaches production.

---

## Architecture

```
            ┌──────────────┐
  client ──▶│  FastAPI     │──────── reads/writes ──────▶┌────────────┐
            │  (uvicorn)   │                             │ PostgreSQL │
            │              │◀── cache get / version ────▶┌────────────┐
            └──────┬───────┘                             │   Redis    │
                   │  enqueue (after commit)             │ cache + ⏃  │
                   ▼                                     └─────┬──────┘
            ┌──────────────┐                                   │
            │ Celery worker│◀────── broker ────────────────────┘
            │              │────── writes notifications ──▶ PostgreSQL
            └──────────────┘
                   ▲
            ┌──────┴───────┐
            │ Celery beat  │  every 60s: scan for overdue tasks
            └──────────────┘
```

```
app/
  main.py              app factory, middleware, exception handlers
  core/
    config.py          env-driven settings (one image, three roles)
    security.py        argon2id + JWT + refresh-token hashing
    logging.py         JSON logs with a request-id contextvar
    metrics.py         Prometheus collectors
    redis.py           shared connection pool
    errors.py          the app's error vocabulary
  db/                  declarative base, engine, session scopes
  models/              User, Project, ProjectMember, Task, Notification, RefreshToken
  schemas/             Pydantic request/response models
  api/
    deps.py            auth + pagination dependencies
    ops.py             health, readiness, metrics
    v1/                auth, projects, tasks, notifications routers
  services/
    authz.py           the single place project access is decided
    cache.py           versioned-key cache + degradation
    task_service.py    the feed, and the writes that invalidate it
    events.py          post-commit enqueue
    ...
  workers/
    celery_app.py      Celery config + beat schedule
    tasks.py           the three background jobs
```

**Layering.** Routers do HTTP (parse, validate, serialise). Services hold the rules and are the only thing that touches the database. Celery jobs call the same services the API does, which is why authorization and idempotency cannot drift between the two paths.

**One image, three roles.** API, worker and beat are the same container with a different command. Nothing can skew between them.

**Sync SQLAlchemy, not async.** The honest reason: this workload is a handful of indexed queries per request, and async would buy concurrency I can't demonstrate a need for while making the Celery side (which is sync anyway) awkward. I'd rather write that down than add a layer I can't justify.

---

## Where the depth went: caching

The brief says stale reads after a status change will be treated as a bug, so I started at invalidation and worked backwards. Full reasoning lives at the top of `app/services/cache.py`.

### Versioned keys, not key deletion

The obvious approach — `SCAN` for `tasks:*` and `DEL` the matches — is wrong twice over. It is O(keyspace) on every write, and it is **not atomic**: a reader that fetched a key microseconds before the delete can still write its stale result back under that key afterwards.

Instead every project carries a monotonic counter:

```
tf:1:projver:<project_id>   →   integer, no TTL
```

and the `(project_id, version)` pairs for every project in scope are folded into the cache key:

```
key = tf:1:tasks:<sha1( scope_versions + normalised_filters + page )>
```

Invalidation is then a single `INCR` per affected project — O(1), atomic, and it makes every previously cached entry **unreachable** rather than merely scheduled for deletion. There is no window in which an old key can be found.

### What goes in the key

Everything that can change the result set: the scope versions, every filter value, the sort, and the page coordinates. Statuses are sorted and datetimes normalised to UTC, so `?status=todo&status=done` and `?status=done&status=todo` share one entry.

The invariant to preserve when adding a filter: **nothing may be resolved from the caller's identity after the fingerprint is computed.**

### Keyed by project set, not by user

Two members of the same project asking the same question must get the same answer, so per-user entries would only halve the hit rate. Authorization runs *before* the cache is consulted, so a non-member never reaches a lookup at all.

A nice consequence: membership changes need no invalidation. Adding Bob to a project changes his *scope*, which is part of the key, so his next request is simply a different key. There's a test for exactly this.

### Write ordering

Every write does the same four things in the same order:

1. authorize
2. mutate and **commit**
3. invalidate every affected project
4. enqueue notifications

Steps 3 and 4 are after the commit for different reasons. Invalidating first leaves a window where a concurrent reader repopulates the cache from pre-commit state and the entry then looks fresh. Enqueuing first lets the worker open its own connection and read a row that isn't visible yet.

Moving a task between projects invalidates **both**.

### Failure behaviour

A cache that can go stale is worse than no cache.

| Failure | Response |
|---|---|
| Redis unreachable on read | Serve from Postgres, enter degraded window |
| Redis unreachable on write | The write still succeeds — refusing a user's update because the cache is down is the wrong trade |
| **`INCR` fails after a commit** | `cache_invalidation_failures_total` increments and reads bypass Redis for at least the payload TTL |
| Corrupt/old-format entry | Treated as a miss, not a 500 |

That third row is the dangerous one: the write landed in Postgres but entries under the un-bumped version are still physically reachable. The degraded window is what stops them being served, and it lasts long enough for them to expire. `GET /health/ready` reports `cache_degraded` so this is visible from outside.

### On eviction

Version keys are written with **no TTL**, and Redis runs `maxmemory-policy volatile-lru`, which only ever evicts keys that *have* a TTL. The counters therefore cannot be dropped under memory pressure. This is a correctness setting, not a tuning knob — it's commented as such in `docker-compose.yml` and `deploy/render.yaml`.

Even if a counter were somehow lost, a counter restarting from zero produces a *different* fingerprint from the live one, so the result is a miss, not a stale hit.

### The tests

A caching suite has one classic way of being worthless: if the cache is never actually hit, every "is it stale?" assertion passes trivially. So `tests/test_cache.py` asserts hits against the Prometheus counters first, and only then do the staleness tests mean anything. It covers status change, create, delete, reassignment, cross-project moves (both sides), due-date edits, key normalisation, pagination in the key, empty-result caching, corrupt entries, Redis entirely down, and the failed-invalidation path.

---

## Authentication

**JWT access tokens + rotating refresh tokens.**

Why JWT over server-side sessions: the API, the worker and beat scale independently, and a shared session store would be the only thing forcing them to coordinate. A stateless access token costs one signature verification on the hot path.

The honest cost of stateless tokens is revocation, so:

- Access tokens are short-lived (15 min).
- Refresh tokens are opaque 384-bit random strings; only their **SHA-256** is stored. A database dump does not hand out live sessions.
- Refresh tokens are **single-use**. Presenting one that was already rotated is the signature of a replayed stolen token, so the entire family is revoked — the attacker and the legitimate holder both lose access, which is the correct failure direction.
- `get_current_user` does one database lookup per request, which is what makes a deactivated account stop working immediately instead of up to 15 minutes later.

**Passwords** use argon2id (`argon2-cffi`), with `check_needs_rehash` so cost parameters can be raised later and existing users upgrade transparently on next login. Only length is enforced (min 12); composition rules measurably push users toward weaker, more predictable passwords, and NIST SP 800-63B advises against them.

Details that are easy to get wrong and are handled:

- An unknown email still pays a dummy argon2 verify, so login timing cannot enumerate registered addresses, and the error body is byte-identical to a wrong password.
- `UserPublic` has no password field at all, so a hash cannot leak by someone adding `from_attributes` to the wrong model. There's a test asserting no response body ever contains it.
- Validation errors are reshaped to drop Pydantic's echo of the rejected input — otherwise a failed `/auth/login` puts the submitted password in the response body *and* the logs.
- `jwt.decode` pins a single algorithm; accepting a caller-influenced list is how `alg: none` and HS/RS confusion attacks get in.

---

## Authorization

`app/services/authz.py` is the single place access to a project is decided. Routers, services and Celery jobs all go through it, so there is one rule to audit rather than a check copy-pasted into every handler.

- **Non-members get 404, not 403.** A 403 confirms the id exists, which turns the id space into an oracle. A test asserts a hidden project and a nonexistent one return byte-identical responses.
- **Members who aren't owners get 403** on owner-only actions (rename, delete, manage membership) — they already legitimately know the project exists, so hiding it would be noise rather than security.
- **The owner is stored as a `ProjectMember` row** with `role=owner`, so the read path asks exactly one question: "is there a membership?" An `or you're the owner` branch is precisely where this class of bug hides.
- **Assignees must be members.** That's a 422, not a 404 — the caller can legitimately see the project, so the problem is the value they sent.
- **Moving a task** requires membership of the destination too, otherwise a member of A could push work into project B.

`tests/test_authz.py` is written from the attacker's side: Carol belongs to nothing, and every request she can construct is checked.

---

## Background jobs

Notification creation never happens in the request cycle — the API only enqueues.

| Trigger | Job | Recipient |
|---|---|---|
| Due date passes while not `done` | `taskflow.scan_overdue_tasks` (beat, every 60s) | Assignee, or the project owner if unassigned |
| Task reassigned (or created already assigned to someone else) | `taskflow.notify_task_reassigned` | New assignee |
| Status changed | `taskflow.notify_task_status_changed` | Assignee, or the project owner |

Nobody is notified about a change they made themselves.

Delivery is simulated as the brief allows: a row in `notifications` plus a structured `notification.delivered` log line. A real transport slots into `_deliver()` without touching anything else.

### Idempotency

`task_acks_late` is on, so a worker killed mid-job gets its message redelivered, and beat can double-tick across a restart. Rather than defend against that in Python — where two concurrent workers race straight past a check-then-insert — every create goes through `ON CONFLICT DO NOTHING` against `UNIQUE(task_id, type, dedupe_key)`.

The dedupe key is computed by the **caller** and passed in, so a retry recomputes the same key. Calling `now()` inside the worker would make every retry look like a fresh event. Keys are chosen so genuinely distinct events still get through: the overdue key is the due date, so rescheduling a task and letting the new deadline lapse notifies again.

### Why a sweep, not a per-task countdown

A countdown scheduled at write time would need cancelling and rescheduling on every due-date edit, and would silently lose pending work if the broker were ever flushed. A periodic scan over an indexed predicate is cheaper to reason about and self-healing after downtime. It uses `FOR UPDATE SKIP LOCKED` so several workers can sweep in parallel without blocking or double-handling.

The overdue scan deliberately does **not** invalidate the cache: `overdue_notified_at` isn't part of the task representation, so no cached list can have become wrong.

---

## Operations

**`GET /health`** is liveness and checks nothing external. If it reported unhealthy while Postgres was down, the orchestrator would restart every API container during a database blip — destroying the capacity that would otherwise serve cached reads and recover.

**`GET /health/ready`** checks Postgres and Redis, and hinges on Postgres alone: Redis being down degrades the service but doesn't stop it serving. It also reports `cache_degraded`.

**`GET /metrics`** exposes `http_requests_total`, `http_errors_total`, `http_request_duration_seconds`, `http_requests_in_progress`, the cache counters, `notifications_created_total` / `notifications_suppressed_total`, and `celery_tasks_total`.

HTTP series are labelled with the **route template** (`/api/v1/tasks/{task_id}`), never the raw path — labelling by path mints a permanent time series per task id and takes down the thing that's supposed to be observing the service. There's a test asserting no id appears in the output. `/metrics` excludes itself, so a scrape doesn't change the numbers it reports.

**Logging** is structured JSON throughout, including from Celery. A request id is minted (or honoured from `X-Request-ID`), attached to every log line via a contextvar, propagated into Celery message headers, and returned on the response — so a delivered notification can be traced back to the request that caused it.

---

## Deployment

**I chose the documented/scripted path, not a live URL.** Nothing here has been executed against a live host.

Two paths are provided:

- **`deploy/render.yaml`** — a Render Blueprint defining all five pieces (web service, Celery worker, Celery beat, managed Postgres, managed Redis). Migrations run as a `preDeployCommand`, `JWT_SECRET` is generated by Render rather than committed, and the Redis `maxmemoryPolicy` is pinned to `volatile-lru` for the correctness reason described above.
- **`deploy/deploy.sh`** — deploys to any Linux VM using the repo's own `docker-compose.yml`. Secrets live in a root-owned env file outside the repo; the script refuses to deploy without it rather than falling back to the development `JWT_SECRET`. It verifies `/health/ready` afterwards, not `/health`, since liveness would pass even with the database unreachable.

**What I did verify locally:** `docker compose up` brings the whole stack to healthy, migrations run once and the rest wait for them, and the full flow works end to end against a real broker — a task created with a past due date produced `task_reassigned`, `task_status_changed` and (after a beat tick) `task_overdue` notifications, with `cache_invalidation_failures_total` at zero.

### CI

`.github/workflows/ci.yml` runs three jobs: **lint** (`ruff check`, `ruff format --check`, `mypy app`), **test** (pytest with real Postgres and Redis service containers, plus `alembic check` to catch models that have drifted from migrations), and **docker** (build the image and assert it can import the app). Not run live — the config is the deliverable.

---

## Assumptions I made

1. **Projects support explicit, owner-controlled membership.** The brief says a user must not see another user's project, but also wants an *assignee* and *reassignment* notifications. Strictly single-owner projects would mean you can only ever assign tasks to yourself, making the notification feature vestigial. So membership is opt-in and granted by the owner; everyone else is treated as if the project does not exist. The "no cross-user access" rule still holds strictly for non-members.
2. **Status changes also produce notifications.** The brief mentions this in its introduction but not in the numbered requirement. It's cheap and clearly intended, so it's included.
3. **`GET /tasks` without `project_id`** returns tasks across every project you belong to, rather than being an error.
4. **Creating a task already assigned to someone else** sends that person a `task_reassigned` notification. From the recipient's side it's indistinguishable from a reassignment, and silently assigning work to someone without telling them would be odd.
5. **Self-actions don't notify.** Assigning a task to yourself, or changing the status of a task assigned to you, notifies nobody.
6. **Membership is granted by email**, not user id — an owner knows a colleague's address, and making them look up an id first would require exposing a user-search endpoint, which is a user-enumeration surface with no upside here.
7. **Unassigned overdue tasks notify the project owner.** The task still needs to reach a human.
8. **Timezones:** a naive datetime in a query string or body is interpreted as UTC, which is also how it's compared against the `timestamptz` columns.

---

## Tradeoffs and what I'd do with more time

I spent the time on caching (chosen as the depth area), authorization boundaries, and job idempotency, and I let other things stay at a competent-but-ordinary level. Here's the honest ledger.

### Known gaps I'd fix first

**A dropped enqueue is a lost notification.** `services/events.py` swallows broker transport errors so that a Redis outage doesn't fail a user's task update. That's the right call for the write, but it means the notification is simply gone. It's counted in `job_enqueue_failures_total` rather than hidden. The proper fix is a **transactional outbox**: write the intent to an `outbox` table inside the same transaction as the task, and have a relay process drain it into Celery. That makes notification delivery exactly as durable as the write that caused it. This is the single biggest correctness gap in the system and it's what I'd build next.

**Celery metrics aren't scrapable.** The worker increments `celery_tasks_total` in its own process, which no one scrapes — the counters only surface in tests and in eager mode. A real setup needs either a sidecar exporter per worker or a push gateway. I left the counters in place because they're free and correct, but `/metrics` today effectively means *API* metrics.

**No rate limiting.** `/auth/login` and `/auth/signup` are unthrottled, so password spraying and signup spam are both open. A Redis fixed-window limiter keyed on IP + email is maybe thirty lines and would be the first thing I'd add for a public deployment. The argon2 cost gives some incidental protection, but that's a side effect, not a defence — and it's also a DoS vector, since each attempt costs the server 64MB and real CPU.

**Offset pagination degrades at depth.** `OFFSET 10000` makes Postgres walk 10,000 rows. I chose offset because the feed is explicitly sortable on several columns and keyset pagination with a user-chosen sort means a compound cursor per sort field — real complexity for a depth nobody is likely to reach here. At scale I'd switch to keyset and keep offset only for small pages. Relatedly, `total` runs a second `COUNT(*)` on every request; at scale I'd drop the exact count or make it approximate.

**Title search uses `ILIKE '%term%'`.** A leading wildcard can't use a btree index, so this is a sequential scan. The fix is a `pg_trgm` GIN index, or full-text search if the requirement grows beyond substring matching.

### Deliberate scope cuts

- **No live deployment.** Scripted path only, as stated above.
- **Simulated delivery.** Notifications are a row plus a log line. Real email/webhook delivery would need a transport abstraction, per-user preferences, retry/bounce handling and a digest mechanism so an overdue sweep doesn't produce a notification storm.
- **Two roles only** (owner/member). No per-task permissions, no org layer above projects, no invitation flow — membership is granted directly, which assumes the user already exists.
- **No soft deletes or audit log.** Deleting a project cascades its tasks away permanently. Any real system wants `deleted_at` and an append-only audit trail of who changed what.
- **No WebSocket/SSE push.** Clients poll `/notifications`. An unread-count poll is cheap, but push is the right answer for a UI.
- **No OpenTelemetry tracing.** Request ids propagate into Celery, which gives correlation but not spans or timing across the hop.
- **Single uvicorn worker per container**, because the Prometheus counters live in process memory. Scaling means more containers, or `prometheus_client`'s multiprocess mode.

### Things I'd revisit in the design itself

- **Cache stampede.** When a hot key expires, every concurrent request for it goes to Postgres together. Correctness is unaffected (versioning handles that), but a `SET NX` single-flight lock or probabilistic early expiry would smooth the load spike. I left it out because it adds a failure mode of its own and there was no measurement to justify it.
- **Notification fan-out.** Only the directly affected user is notified. Watchers, mentions or "notify the whole project" would need a subscription model.
- **Beat is a single point of failure.** One beat instance is required (two would double every tick). It's not highly available; a real deployment would use a leader-election scheduler or a locking beat backend.
- **The overdue sweep is a full-table predicate.** It's indexed and batched with `SKIP LOCKED`, which is fine to a few million tasks. Beyond that I'd partition by due date or shard the sweep.

### What I'd want a reviewer to look at

If you only read three files: `app/services/cache.py` for the invalidation argument, `app/services/authz.py` for the access-control model, and `tests/test_cache.py` — particularly `test_failed_invalidation_degrades_instead_of_serving_stale`, which is the test I'd want someone to try to break.
