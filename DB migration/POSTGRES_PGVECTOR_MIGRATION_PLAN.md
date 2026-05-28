# PostgreSQL + pgvector Migration Plan

## 1. Objective

Migrate VIP from SQLite to PostgreSQL with pgvector in a safe, reversible, production-ready way that supports:

- continuous data growth
- increasing concurrent users and writes
- more sophisticated assistant search workloads
- long-term operational scalability

This plan is designed to avoid big-bang risk and preserve rollback options until confidence is high.

---

## 2. Scope and Non-Goals

### In scope

- database platform migration from SQLite to PostgreSQL
- vector storage migration to pgvector
- schema, data, and query compatibility changes
- cutover, validation, monitoring, and rollback strategy

### Out of scope

- major product feature redesign
- full search architecture split (for now)
- graph database adoption in this phase

---

## 3. Target Architecture (Phase 1)

- Primary OLTP and analytics store: PostgreSQL 16+
- Vector storage: pgvector extension in same PostgreSQL instance
- Application: backend routes and pipeline use repository layer compatible with PostgreSQL
- Migration system: Alembic (or Flyway) for schema lifecycle
- Optional connection broker: PgBouncer

### Why this target

- highest delivery confidence
- strongest transactional semantics for identity operations
- allows sophisticated assistant SQL and relationship-heavy queries
- minimal moving parts versus adding separate vector engine immediately

---

## 4. Migration Principles

1. Separate platform migration from feature changes.
2. Validate every step with measurable gates.
3. Keep rollback path open through stabilization period.
4. Prefer iterative compatibility layers over massive rewrites.
5. Treat vector correctness and ranking parity as first-class acceptance criteria.

---

## 5. Roles and Responsibilities

- Migration lead: overall timeline and go/no-go decisions
- Backend lead: query compatibility and repository abstraction
- Data engineer: ETL tooling and data validation checks
- Infra lead: PostgreSQL provisioning, backups, observability
- QA lead: parity test suites and non-functional testing
- Release manager: cutover runbook execution and communication

---

## 6. Phase Plan

## Phase 0: Program Setup

### Tasks

1. Define migration success criteria:
   - API latency budget (P95/P99)
   - ingest throughput target
   - assistant query latency target
   - zero data-loss requirement
2. Create risk register and mitigations.
3. Define environment ladder:
   - local PostgreSQL
   - staging with production-like data volume
   - production PostgreSQL
4. Approve migration RFC.

### Exit criteria

- owners, timeline, and acceptance metrics are approved
- go/no-go authority is clear

---

## Phase 1: Schema and Data Model Design

### Tasks

1. Inventory current SQLite schema and implicit constraints.
2. Produce PostgreSQL DDL with explicit constraints and indexes.
3. Decide profile strategy:
   - shared tables with profile_id (recommended)
   - or separate schema per profile
4. Define data type mapping:
   - datetime text to timestamptz
   - JSON text to jsonb where needed
   - numeric and boolean normalization
5. Define vector columns and indexes:
   - vector(dim)
   - HNSW index plan for nearest-neighbor paths
6. Define partitioning candidates (if needed for growth):
   - media_files, embeddings, events/audit tables

### Exit criteria

- reviewed ERD and SQL DDL approved
- index strategy for top query patterns agreed

---

## Phase 2: Application Compatibility Layer

### Tasks

1. Introduce a database access abstraction to isolate SQL dialect differences.
2. Replace SQLite-specific SQL patterns with PostgreSQL-compatible forms.
3. Ensure transactional behavior is explicit and tested.
4. Add retry strategy for transient PostgreSQL errors:
   - deadlocks
   - serialization failures
5. Move schema evolution to migration framework (Alembic or Flyway).
6. Add PostgreSQL integration tests in CI.

### Tech stack changes required

- Python PostgreSQL driver (asyncpg or psycopg)
- SQLAlchemy dialect updates if used
- connection pool configuration per process
- optional PgBouncer for session pooling
- migration tooling and CI jobs for up/down validation

### Exit criteria

- backend test suite passes against PostgreSQL in CI
- critical routes verified on both engines in staging

---

## Phase 3: Infrastructure Provisioning

### Tasks

1. Provision PostgreSQL 16+ (managed preferred for first migration).
2. Enable pgvector extension.
3. Configure:
   - HA/replication
   - PITR and backups
   - storage autoscaling policy
   - TLS and secrets management
4. Set up observability:
   - slow query logging
   - lock and wait metrics
   - connection utilization
   - replication lag

### Exit criteria

- restore drill validated
- performance smoke tests pass

---

## Phase 4: ETL and Verification Tooling

### Tasks

1. Build extractor from SQLite snapshot.
2. Build transformer:
   - normalize timestamps
   - normalize booleans
   - parse/clean JSON
   - convert vector blobs to pgvector format
3. Build loader using bulk COPY where possible.
4. Build validation harness:
   - row counts per table
   - FK/constraint integrity
   - sampled checksums
   - business invariants
5. Make ETL rerunnable and idempotent for dry runs.

### Exit criteria

- full dry-run migration completes in staging
- validation harness passes within accepted tolerances

---

## Phase 5: Shadow and Parity Validation

### Tasks

1. Load staging PostgreSQL with production-like snapshot.
2. Run parity tests for key product paths:
   - People view and merge suggestions
   - unnamed cluster assignment flows
   - assistant search endpoints
   - writeback queue behavior
3. Compare ranking parity for vector-driven endpoints:
   - top-k overlap and quality checks
4. Tune indexes and query plans using EXPLAIN ANALYZE.

### Exit criteria

- parity thresholds met
- no critical query regressions

---

## Phase 6: Production Cutover

### Strategy

Use controlled cutover with short write-freeze window.

### Tasks

1. Freeze risky schema and feature changes before cutover.
2. Take final SQLite snapshot and run final ETL.
3. Pause writes briefly (maintenance window).
4. Run delta sync (if required).
5. Switch application DB connection to PostgreSQL.
6. Ramp traffic in stages (canary to full).
7. Monitor metrics in real time.

### Go/no-go checks at cutover

- DB connectivity and migrations healthy
- key API probes green
- assistant query latency within threshold
- queue processing functioning

### Exit criteria

- stable operation for defined hypercare period

---

## Phase 7: Hypercare and Decommission

### Tasks

1. Run daily integrity checks during hypercare.
2. Track user-facing errors and query latency trends.
3. Keep SQLite fallback snapshot and rollback tooling until closeout.
4. Decommission legacy SQLite path after rollback window expires.

### Exit criteria

- rollback window closed
- PostgreSQL declared primary and permanent

---

## 7. Detailed Data Validation Checklist

### Structural checks

- table row counts match expected totals
- required foreign keys valid
- nullability and uniqueness constraints satisfied

### Domain checks

- person/cluster/face membership consistency
- photo_count and derived aggregates match
- cannot-link and rejection records migrated correctly
- writeback queue state preserved

### Vector checks

- vector dimension consistency across all rows
- null/invalid vector rate is zero (or accepted tiny threshold)
- nearest-neighbor recall parity against reference dataset

### Assistant checks

- curated natural language queries produce expected entities
- latency and ranking quality remain acceptable

---

## 8. Rollback Plan

### Rollback triggers

- sustained error rate beyond threshold
- major integrity mismatch
- unacceptable assistant search degradation
- critical queue/writeback failures

### Rollback actions

1. Switch app DB config back to SQLite.
2. Restart services and validate health probes.
3. Preserve PostgreSQL state for forensic diff.
4. Perform root-cause and fix before retrying cutover.

### Rollback readiness requirements

- tested rollback command sequence
- clear decision owner and communication channel
- snapshots retained and verified restorable

---

## 9. Risk Register (Initial)

1. SQL dialect incompatibilities causing hidden logic drift.
   - Mitigation: repository abstraction + parity tests.
2. Vector conversion errors from blob formats.
   - Mitigation: strict conversion validators + dimension checks.
3. Query performance regressions under assistant workloads.
   - Mitigation: benchmark suite + index tuning + materialized views.
4. Cutover-time data drift.
   - Mitigation: short write-freeze and final delta sync.
5. Connection saturation in production.
   - Mitigation: pool sizing, PgBouncer, alerting.

---

## 10. Operational SLOs and Metrics

Track at minimum:

- API P95/P99 latency by route
- assistant endpoint latency and timeout rate
- DB CPU, IOPS, buffer cache hit ratio
- lock waits and deadlock counts
- slow query volume
- queue lag and writeback throughput
- vector query latency and top-k quality drift

---

## 11. Post-Migration Optimization Backlog

1. Evaluate whether pgvector remains sufficient at next scale tier.
2. Add read replicas for assistant-heavy read paths.
3. Introduce materialized views for expensive relationship queries.
4. Consider optional search engine split (OpenSearch) for advanced text retrieval.
5. Consider optional graph projection for relationship analytics (not primary OLTP).

---

## 12. Suggested Delivery Timeline (Example: 6 Weeks)

- Week 1: Phase 0 and Phase 1
- Week 2-3: Phase 2 and infra prep
- Week 4: ETL tooling and first full dry run
- Week 5: parity fixes and second dry run
- Week 6: production cutover and hypercare start

Adjust based on dataset complexity, team bandwidth, and release windows.

---

## 13. Go-Live Checklist

- [ ] PostgreSQL infra and backups validated
- [ ] pgvector enabled and indexed
- [ ] migration scripts versioned and repeatable
- [ ] parity test suite passed
- [ ] rollback drill executed successfully
- [ ] production cutover runbook approved
- [ ] on-call staffing scheduled for hypercare
- [ ] stakeholder communication prepared

---

## 14. Immediate Next Actions

1. Approve profile/tenant model for PostgreSQL.
2. Freeze schema contracts and publish target DDL draft.
3. Implement repository abstraction for SQLite/PostgreSQL dual support.
4. Build first ETL dry-run against staging and run parity baseline.
