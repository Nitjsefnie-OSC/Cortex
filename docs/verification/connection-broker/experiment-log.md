# Connection-broker study: experiment log (ADR-1112)

Append-only. One variable per entry; failures are recorded like successes.
New entries are appended by `python scripts/repro_connection_broker.py
<measurement>` or by hand at the bottom; earlier entries are never edited.
Times are UTC unless marked. Entries 1 to 8 were written by hand on
2026-10-20/21 from the commands run in the investigating session; the times
given are those the commands printed (Europe/Paris where PostgreSQL printed
local time).

## Control environment

- Developer server: PostgreSQL 17.9 (Homebrew), `max_connections = 100`, the
  connecting role is a superuser (`rolsuper = true`).
- Baseline cluster: a private cluster from the same binaries, created per run
  in a pytest temporary directory (`tests_py/_pg_private_cluster.py`):
  `initdb -A trust`, TCP 127.0.0.1 on a free port, no Unix socket,
  `max_connections = 20`, `fsync = off`. Never the live store.
- Test database for increment 0: the per-process throwaway database the suite
  already creates (`tests_py/_pg_throwaway_db.py`).

## Entries

### 1. 2026-10-21 00:00:48 +02:00: live store, read-only probe

- Variable: none (observation). One read-only connection
  (`default_transaction_read_only=on`, `statement_timeout=3000`).
- `schema_meta`: `ddl_hash` prefix `c867454173d6227e`, `applied_at`
  2026-10-20 23:59:31 +02:00 (77 s before the read).
- `pg_stat_activity` on `cortex`: 21 idle, 1 active, 0 idle in transaction.

### 2. 2026-10-21 ~00:05 +02:00: DDL fingerprint of each installed tree

- Variable: code tree. `sha256("\n".join(get_all_ddl()))`, first 16 hex.
- Claude Code plugin cache `hypermnesia-mcp/4.23.5` (built 2026-10-16):
  `44002703f29f0c2d`. This worktree at `df3a4479`: `44002703f29f0c2d`.
- Codex uv tool `hypermnesia-mcp` 4.23.5 (wheel
  `dist/codex-hooks-20261002/hypermnesia_mcp-4.23.5-py3-none-any.whl`,
  installed 2026-10-02): `c867454173d6227e`. Same version label, different
  `pg_schema.py` (ADR-1106/1109 recall-function changes absent).

### 3. Increment-0 tests before the fix (worktree at `df3a4479`)

- `pytest tests_py/infrastructure/test_pg_no_runtime_ddl_no_idle_tx.py`:
  4 failed, 1 passed in 11.70 s.
- Failed: store open on a foreign revision still alive after 10 s behind an
  idle reader (queued DDL); `apply_schema` missing (ImportError); 1 backend
  `idle in transaction` while the decay stream was paused between chunks; a
  row deleted mid-pass was still streamed (`7 in [4, 5, 6, 7]`, cursor
  snapshot).
- Passed: the PostgreSQL fact. `ALTER TABLE memories ADD COLUMN IF NOT EXISTS
  is_team_decision ...` on an existing column fails with `LockNotAvailable`
  under `lock_timeout = 500ms` while another session holds an open
  transaction that read `memories`.

### 4. Failed approach: per-database CONNECTION LIMIT on the shared server

- Variable: saturation mechanism. Idea: `ALTER DATABASE <throwaway> CONNECTION
  LIMIT n` to saturate one database without touching the others.
- Rejected before running: the role is a superuser and PostgreSQL does not
  apply a database connection limit to superusers. Replaced by the private
  cluster of the control environment.

### 5. Baseline, three runs (private cluster, `max_connections = 20`, 8 children)

| run | backends at peak | connections held per blocked child | refused or timed out | outcomes |
|---|---|---|---|---|
| a | 17 | 2, 4, 4, 4 | 4 of 8 | 3 PoolTimeout (5.00 s), 1 `FATAL: sorry, too many clients already` |
| b | 16 | 2, 3, 4, 4 | 4 of 8 | 4 PoolTimeout |
| c | 15 | 4, 4, 4 | 5 of 8 | 5 PoolTimeout |

- A blocked child holds its persistent connection plus pooled connections
  that grow toward `POOL_INTERACTIVE_MAX = 8` while its request waits.

### 6. Increment-0 tests after the fix

- Same file plus the touched suites (`test_migrate.py`,
  `test_pg_recall_ddl_upgrade.py`, `test_pg_schema_provision_ready.py`,
  `test_sqlite_parity_197.py`, `test_hook_receipts.py`): 51 passed.

### 7. 2026-10-21 00:18:32 +02:00: live store, read-only probe again

- `schema_meta`: `ddl_hash` prefix `44002703f29f0c2d`, `applied_at`
  2026-10-21 00:16:57 +02:00. Between entries 1 and 7 (17 min) the recorded
  revision flipped from the Codex tree to the Claude tree: at least two full
  DDL re-applications on the live store, each taking AccessExclusive locks.
- 22 client backends; per Cortex process 3 to 4 TCP connections (`lsof`), six
  processes holding connections, two detached `session_lifecycle
  --consolidate` processes alive for 1 h 26 min and 2 h 50 min.

### 8. Not measured

- Which statement the 2 h `ALTER TABLE` of 2026-10-20 was, and which process
  issued it: `pg_locks` was not captured during the incident.

### 2026-10-20T22:23:56+00:00 baseline

- head: `df3a4479fbd0207de6e0bade919f4de09da4273e` (dirty)
- command: `python scripts/repro_connection_broker.py baseline`
- pytest exit code: 0 (1 passed in 29.23s)
- measured: `{"backends_at_peak": 15, "children": 8, "max_connections": 20, "outcomes": ["PoolTimeout: couldn't get a connection after 5.00 sec", "PoolTimeout: couldn't get a connection after 5.00 sec", "PoolTimeout: couldn't get a connection after 5.00 sec", "PoolTimeout: couldn't get a connection after 5.00 sec", "PoolTimeout: couldn't get a connection after 5.00 sec"], "per_blocked_child": [4, 4, 4], "refused_or_timed_out": 5}`

### 2026-10-20T22:24:00+00:00 increment0

- head: `df3a4479fbd0207de6e0bade919f4de09da4273e` (dirty)
- command: `python scripts/repro_connection_broker.py increment0`
- pytest exit code: 0 (5 passed in 1.79s)
- measured: `{}`

### 20261020T223155Z lock chain

- head: `df3a4479fbd0207de6e0bade919f4de09da4273e`
- command: `python scripts/repro_lock_chain.py`
- evidence: `docs/verification/connection-broker/evidence/lock-chain-20261020T223155Z.json`
- stream/main: `{'tree': 'main', 'consumer_ready': True, 'consumer_write_done_within_deadlock_window': False, 'released_by': 'consumer process killed by the harness', 'alter': {'at': 16658.7436365}, 'readers_completed': 3, 'readers_waited_s': 3.138, 'readers_completed_before_release': 0}`
  chain at capture:
  - `main-consolidation` idle
  - `main-consolidation` idle in transaction
  - `main-consolidation` idle
  - `ddl` active, waits Lock, blocked by ['main-consolidation']
  - `reader-0` active, waits Lock, blocked by ['ddl']
  - `reader-1` active, waits Lock, blocked by ['ddl']
  - `reader-2` active, waits Lock, blocked by ['ddl']
- open/main: `{'tree': 'main', 'opened_while_reader_idle': None, 'store_done': True, 'readers_completed': 3, 'readers_waited_s': 0.168, 'readers_completed_before_release': 0, 'ddl_statements_logged': 98, 'ddl_sample': ['2026-10-21 00:31:47.270 CEST [19964] main-store-open LOG:  statement: CREATE EXTENSION IF NOT EXISTS vector;', '2026-10-21 00:31:47.271 CEST [19964] main-store-open LOG:  statement: CREATE EXTENSION IF NOT EXISTS pg_trgm;', '2026-10-21 00:31:47.271 CEST [19964] main-store-open LOG:  statement: CREATE TABLE IF NOT EXISTS memories ('], 'recorded_revision_after': '44002703f29f0c2d'}`
  chain at capture:
  - `blocker` idle in transaction
  - `main-store-open` active, waits Lock, blocked by ['blocker']
  - `reader-0` active, waits Lock, blocked by ['main-store-open']
  - `reader-1` active, waits Lock, blocked by ['main-store-open']
  - `reader-2` active, waits Lock, blocked by ['main-store-open']
- stream/branch: `{'tree': 'branch', 'consumer_ready': True, 'consumer_write_done_within_deadlock_window': True, 'released_by': 'consumer finished its pass', 'alter': {'at': 16664.256566375}, 'readers_completed': 3, 'readers_waited_s': 0.006, 'readers_completed_before_release': 3}`
  chain at capture:
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `ddl` idle
- open/branch: `{'tree': 'branch', 'opened_while_reader_idle': 'OPENED', 'store_done': True, 'readers_completed': 3, 'readers_waited_s': 0.008, 'readers_completed_before_release': 3, 'ddl_statements_logged': 0, 'ddl_sample': [], 'recorded_revision_after': 'another-install-'}`
  chain at capture:
  - `blocker` idle in transaction
- flipflop/main+other: `{'order': ['main', 'other', 'main', 'other'], 'steps': [{'tree': 'main', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'main', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}]}`
- flipflop/branch+other: `{'order': ['branch', 'other', 'branch', 'other'], 'steps': [{'tree': 'branch', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'branch', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': 'c867454173d6227e'}]}`

### 20261020T223237Z lock chain

- head: `df3a4479fbd0207de6e0bade919f4de09da4273e`
- command: `python scripts/repro_lock_chain.py`
- evidence: `docs/verification/connection-broker/evidence/lock-chain-20261020T223237Z.json`
- stream/main: `{'tree': 'main', 'consumer_ready': True, 'consumer_write_done_within_deadlock_window': False, 'released_by': 'consumer process killed by the harness', 'alter': {'at': 16699.748722875}, 'readers_completed': 3, 'readers_waited_s': 3.143, 'readers_completed_before_release': 0}`
  chain at capture:
  - `main-consolidation` idle
  - `main-consolidation` idle in transaction
  - `main-consolidation` idle
  - `ddl` active, waits Lock, blocked by ['main-consolidation']
  - `reader-0` active, waits Lock, blocked by ['ddl']
  - `reader-1` active, waits Lock, blocked by ['ddl']
  - `reader-2` active, waits Lock, blocked by ['ddl']
  after the consumer's own write:
  - `main-consolidation` idle
  - `main-consolidation` idle in transaction
  - `main-consolidation` idle
  - `ddl` active, waits Lock, blocked by ['main-consolidation']
  - `reader-0` active, waits Lock, blocked by ['ddl']
  - `reader-1` active, waits Lock, blocked by ['ddl']
  - `reader-2` active, waits Lock, blocked by ['ddl']
  - `main-consolidation` active, waits Lock, blocked by ['ddl']
  - `main-consolidation` idle
  - `main-consolidation` idle
- open/main: `{'tree': 'main', 'opened_while_reader_idle': None, 'store_done': True, 'readers_completed': 3, 'readers_waited_s': 0.173, 'readers_completed_before_release': 0, 'ddl_statements_logged': 98, 'ddl_sample': ['2026-10-21 00:32:28.384 CEST [25688] main-store-open LOG:  statement: CREATE EXTENSION IF NOT EXISTS vector;', '2026-10-21 00:32:28.385 CEST [25688] main-store-open LOG:  statement: CREATE EXTENSION IF NOT EXISTS pg_trgm;', '2026-10-21 00:32:28.385 CEST [25688] main-store-open LOG:  statement: CREATE TABLE IF NOT EXISTS memories ('], 'recorded_revision_after': '44002703f29f0c2d'}`
  chain at capture:
  - `blocker` idle in transaction
  - `main-store-open` active, waits Lock, blocked by ['blocker']
  - `reader-0` active, waits Lock, blocked by ['main-store-open']
  - `reader-1` active, waits Lock, blocked by ['main-store-open']
  - `reader-2` active, waits Lock, blocked by ['main-store-open']
- stream/branch: `{'tree': 'branch', 'consumer_ready': True, 'consumer_write_done_within_deadlock_window': True, 'released_by': 'consumer finished its pass', 'alter': {'at': 16705.400003208}, 'readers_completed': 3, 'readers_waited_s': 0.008, 'readers_completed_before_release': 3}`
  chain at capture:
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `ddl` idle
  after the consumer's own write:
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `ddl` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
- open/branch: `{'tree': 'branch', 'opened_while_reader_idle': 'OPENED', 'store_done': True, 'readers_completed': 3, 'readers_waited_s': 0.009, 'readers_completed_before_release': 3, 'ddl_statements_logged': 0, 'ddl_sample': [], 'recorded_revision_after': 'another-install-'}`
  chain at capture:
  - `blocker` idle in transaction
- flipflop/main+other: `{'order': ['main', 'other', 'main', 'other'], 'steps': [{'tree': 'main', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'main', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}]}`
- flipflop/branch+other: `{'order': ['branch', 'other', 'branch', 'other'], 'steps': [{'tree': 'branch', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'branch', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'other', 'completed': True, 'ddl_statements_logged': 0, 'recorded_revision_after': 'c867454173d6227e'}]}`

### 9. 2026-10-20T22:35Z: note on the two lock-chain entries above (by hand)

- Both runs are the same experiment on the same code; between them only the
  summary printer changed (it now also prints the chain after the consumer's
  own write, which the first run's evidence JSON already contains under
  `after_write`). Results agree: `stream/main` forms the client-closed cycle
  (consumer `idle in transaction` -> `ALTER` waits -> readers and the
  consumer's own `UPDATE` wait on the `ALTER`), released only by killing the
  consumer; `open/main` logs 98 DDL statements and its first waiting
  statement is the ADR-1083 `ADD COLUMN IF NOT EXISTS`; the branch variants
  form no chain and log no DDL; `flipflop/main+other` logs 0, 98, 98, 98 DDL
  statements and `flipflop/branch+other` 0, 98, 0, 0.

### 20261020T230256Z lock chain

- head: `8b1e2d2b9819646d5d2a661152805248613f2544`
- command: `python scripts/repro_lock_chain.py`
- evidence: `docs/verification/connection-broker/evidence/lock-chain-20261020T230256Z.json`
- stream/main: `{'tree': 'main', 'consumer_ready': True, 'consumer_write_done_within_deadlock_window': False, 'released_by': 'consumer process killed by the harness', 'alter': {'at': 18519.786166166}, 'readers_completed': 3, 'readers_waited_s': 3.132, 'readers_completed_before_release': 0}`
  chain at capture:
  - `main-consolidation` idle
  - `main-consolidation` idle in transaction
  - `main-consolidation` idle
  - `ddl` active, waits Lock, blocked by ['main-consolidation']
  - `reader-0` active, waits Lock, blocked by ['ddl']
  - `reader-1` active, waits Lock, blocked by ['ddl']
  - `reader-2` active, waits Lock, blocked by ['ddl']
  after the consumer's own write:
  - `main-consolidation` idle
  - `main-consolidation` idle in transaction
  - `main-consolidation` idle
  - `ddl` active, waits Lock, blocked by ['main-consolidation']
  - `reader-0` active, waits Lock, blocked by ['ddl']
  - `reader-1` active, waits Lock, blocked by ['ddl']
  - `reader-2` active, waits Lock, blocked by ['ddl']
  - `main-consolidation` idle
  - `main-consolidation` active, waits Lock, blocked by ['ddl']
  - `main-consolidation` idle
- open/main: `{'tree': 'main', 'opened_while_reader_idle': None, 'store_outcome': 'DONE', 'readers_completed': 3, 'readers_waited_s': 0.162, 'readers_completed_before_release': 0, 'ddl_statements_logged': 98, 'ddl_sample': ['2026-10-21 01:02:48.289 CEST [43384] main-store-open LOG:  statement: CREATE EXTENSION IF NOT EXISTS vector;', '2026-10-21 01:02:48.289 CEST [43384] main-store-open LOG:  statement: CREATE EXTENSION IF NOT EXISTS pg_trgm;', '2026-10-21 01:02:48.289 CEST [43384] main-store-open LOG:  statement: CREATE TABLE IF NOT EXISTS memories ('], 'recorded_revision_after': '44002703f29f0c2d'}`
  chain at capture:
  - `blocker` idle in transaction
  - `main-store-open` active, waits Lock, blocked by ['blocker']
  - `reader-0` active, waits Lock, blocked by ['main-store-open']
  - `reader-1` active, waits Lock, blocked by ['main-store-open']
  - `reader-2` active, waits Lock, blocked by ['main-store-open']
- stream/branch: `{'tree': 'branch', 'consumer_ready': True, 'consumer_write_done_within_deadlock_window': True, 'released_by': 'consumer finished its pass', 'alter': {'at': 18525.297148458}, 'readers_completed': 3, 'readers_waited_s': 0.007, 'readers_completed_before_release': 3}`
  chain at capture:
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `ddl` idle
  after the consumer's own write:
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `ddl` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
  - `branch-consolidation` idle
- open/branch: `{'tree': 'branch', 'opened_while_reader_idle': 'REFUSED SchemaTamperedError', 'store_outcome': 'REFUSED SchemaTamperedError', 'readers_completed': 3, 'readers_waited_s': 0.009, 'readers_completed_before_release': 3, 'ddl_statements_logged': 0, 'ddl_sample': [], 'recorded_revision_after': 'another-install-'}`
  chain at capture:
  - `blocker` idle in transaction
- flipflop/main+other: `{'order': ['main', 'other', 'main', 'other'], 'steps': [{'tree': 'main', 'outcome': 'DONE', 'ddl_statements_logged': 0, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'outcome': 'DONE', 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'main', 'outcome': 'DONE', 'ddl_statements_logged': 98, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'outcome': 'DONE', 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}]}`
- flipflop/branch+other: `{'order': ['branch', 'other', 'branch', 'other'], 'steps': [{'tree': 'branch', 'outcome': 'DONE', 'ddl_statements_logged': 0, 'recorded_revision_after': '44002703f29f0c2d'}, {'tree': 'other', 'outcome': 'DONE', 'ddl_statements_logged': 98, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'branch', 'outcome': 'REFUSED SchemaTamperedError', 'ddl_statements_logged': 0, 'recorded_revision_after': 'c867454173d6227e'}, {'tree': 'other', 'outcome': 'DONE', 'ddl_statements_logged': 0, 'recorded_revision_after': 'c867454173d6227e'}]}`

### 10. 2026-10-20T23:10Z: increment 1 measurements (by hand)

- Variable: increment 1 code (versioned schema, refusal, atomic migration).
- `pytest tests_py/infrastructure/test_pg_migrations.py`: 15 passed (state
  table of refusals, first migration, newer schema refused and untouched,
  failed statement records nothing then retried, pre-versioning database
  adopted, two concurrent migrations apply once, failed state read never a
  first run).
- Audit probe D on a private cluster (`test_pg_role_probes.py`): owner role
  without superuser; `CREATE EXTENSION vector` fails; `MigrationFailedError`;
  `schema_version` and `schema_meta` both absent; `python -m mcp_server.migrate`
  exits 1 with "was not recorded". On main the same probe recorded the
  fingerprint after 58 swallowed failures (audit E13).
- Measured before writing increment 1 (scratch script, private cluster): the
  full DDL set applied on a fresh database and re-applied over itself with 0
  failing statements, so "record only when every statement succeeded" does not
  block a normal migration.
- Lock-chain rerun (entry above): `open/branch` refuses with
  `SchemaTamperedError` and logs 0 DDL; `flipflop/branch+other` 0, 98, 0, 0 with
  the branch's second open refused.
- CI job `broker-measurements` simulated locally with
  `CORTEX_TEST_DATABASE_URL=postgresql://127.0.0.1:1/unused`: the saturation
  and role-probe tests ran on private clusters only, 2 passed.

### 2026-10-20T23:22:44+00:00 DDL census

- command: `python scripts/repro_ddl_census.py`
- statements: `99`
- before/superuser/fresh: `{"failed": 0, "by_error": {}}`
- before/superuser/rerun: `{"failed": 0, "by_error": {}}`
- before/owner-noext/fresh: `{"failed": 58, "by_error": {"UndefinedTable: relation \"memories\" does not exist": 23, "UndefinedTable: relation \"wiki.pages\" does not exist": 8, "UndefinedObject: type \"vector\" does not exist": 5, "UndefinedTable: relation \"wiki.concepts\" does not exist": 3, "UndefinedTable: relation \"wiki.claim_events\" does not exist": 3, "UndefinedTable: relation \"wiki.links\" does not exist": 3, "UndefinedTable: relation \"wiki.citations\" does not exist": 3, "UndefinedTable: relation \"stage_transitions\" does not exist": 2, "UndefinedObject: type memories does not exist": 2, "UndefinedObject: type vector does not exist": 2, "InsufficientPrivilege: permission denied to create extension \"vector\"": 1, "UndefinedTable: relation \"memory_entities\" does not exist": 1, "UndefinedTable: relation \"wiki.drafts\" does not exist": 1, "UndefinedTable: relation \"wiki.page_sources\" does not exist": 1}}`
- before/owner-noext/rerun: `{"failed": 58, "by_error": {"UndefinedTable: relation \"memories\" does not exist": 23, "UndefinedTable: relation \"wiki.pages\" does not exist": 8, "UndefinedObject: type \"vector\" does not exist": 5, "UndefinedTable: relation \"wiki.concepts\" does not exist": 3, "UndefinedTable: relation \"wiki.claim_events\" does not exist": 3, "UndefinedTable: relation \"wiki.links\" does not exist": 3, "UndefinedTable: relation \"wiki.citations\" does not exist": 3, "UndefinedTable: relation \"stage_transitions\" does not exist": 2, "UndefinedObject: type memories does not exist": 2, "InsufficientPrivilege: permission denied to create extension \"vector\"": 1, "UndefinedTable: relation \"memory_entities\" does not exist": 1, "UndefinedTable: relation \"wiki.drafts\" does not exist": 1, "UndefinedTable: relation \"wiki.page_sources\" does not exist": 1, "InsufficientPrivilege: permission denied to set parameter \"pg_trgm.similarity_threshold\"": 1, "UndefinedObject: type vector does not exist": 1}}`
- before/owner-ext/fresh: `{"failed": 0, "by_error": {}}`
- before/owner-ext/rerun: `{"failed": 1, "by_error": {"InsufficientPrivilege: permission denied to set parameter \"pg_trgm.similarity_threshold\"": 1}}`
- after/superuser/fresh: `{"failed": 0, "applied": 99}`
- after/superuser/rerun: `{"failed": 0, "applied": 99}`
- after/owner-noext/fresh: `{"failed": 0, "refused_before_any_statement": "MigrationPrerequisiteError"}`
- after/owner-noext/rerun: `{"failed": 0, "refused_before_any_statement": "MigrationPrerequisiteError"}`
- after/owner-ext/fresh: `{"failed": 0, "applied": 99}`
- after/owner-ext/rerun: `{"failed": 0, "applied": 99}`

### 11. 2026-10-20T23:30Z: root causes of the failing schema statements (by hand)

- Variable: database setup (role and extensions), runner (main's loop vs
  increment 1). Raw counts: the DDL census entry above
  (`python scripts/repro_ddl_census.py`), 99 statements per run.
- Superuser owner: 0 failed, fresh and re-run, with both runners.
- Non-superuser owner, no extensions (audit probe D): 58 failed with main's
  loop. One root cause: `CREATE EXTENSION IF NOT EXISTS vector` raises
  `InsufficientPrivilege` (pgvector is not a trusted extension). The other 57
  are its consequences: the `memories` table needs the `vector` type
  (`UndefinedObject`), and every table with a foreign key to `memories` or
  `wiki.pages`, every index on them and every function over them follows
  (`UndefinedTable`). Fix: a prerequisite step before any statement creates a
  missing extension when the role can, and otherwise stops with
  `MigrationPrerequisiteError` naming the one administrator command. After:
  0 statements run, 0 failed, nothing recorded.
- Non-superuser owner, extensions created by an administrator: fresh 0
  failed, but the re-run failed 1 statement with main's loop:
  `recall_memories()` declares `SET pg_trgm.similarity_threshold`
  (pg_schema.py:1201), and a non-superuser may set that parameter only once
  pg_trgm's library has defined it in the session. On a fresh database an
  earlier trigram index creation loads the library; on a re-run every
  `CREATE INDEX IF NOT EXISTS` is skipped, the library is never loaded, the
  parameter is a placeholder, and the function statement is refused (an
  ordering dependency on session state). Fix: the prerequisite step calls
  `similarity('', '')`, which loads the library, before the statements. After:
  0 failed, fresh and re-run.
- Tests that pin it: `tests_py/infrastructure/test_pg_role_probes.py`
  (zero failures fresh and on a full re-run, superuser and non-superuser
  owner; probe D refused before any statement) and
  `tests_py/infrastructure/test_pg_migrations.py`.

### store funnel census 2026-10-21T01:22Z (`python scripts/repro_store_funnel.py`)

- Variable: the code tree (main vs working tree); no database.
- main: `{"connection_access_lines": 59, "connection_access_files": 42, "driver_importers_outside_diagnostics": 3}`
- working tree: `{"connection_access_lines": 0, "connection_access_files": 0, "driver_importers_outside_diagnostics": 0}`
- main files: handlers/anchor.py, handlers/backfill_helpers.py, handlers/calibration.py, handlers/codebase_analyze.py, handlers/codebase_analyze_helpers.py, handlers/consolidation/cascade.py, handlers/consolidation/memify.py, handlers/consolidation/memify_derive.py, handlers/consolidation/memory_dedup_exact_pass.py, handlers/consolidation/memory_domain_backfill_pass.py, handlers/consolidation/memory_reheat_pass.py, handlers/consolidation/near_dup_calibration_pass.py, handlers/consolidation/sleep.py, handlers/consolidation/wiki_backlog_pass.py, handlers/consolidation/wiki_citation_seed_pass.py, handlers/consolidation/wiki_domain_backfill_pass.py, handlers/consolidation/wiki_source_backfill_pass.py, handlers/consolidation/write_class_backfill_pass.py, handlers/curate_wiki_uncited.py, handlers/forget.py, handlers/get_grooming_health.py, handlers/get_rules.py, handlers/ingest_codebase.py, handlers/ingest_findings.py, handlers/lesson_promotion.py, handlers/predict.py, handlers/remember_helpers.py, handlers/resolve_prediction.py, handlers/wiki_compile.py, handlers/wiki_consolidate.py, handlers/wiki_curate.py, handlers/wiki_emerge.py, handlers/wiki_extract.py, handlers/wiki_migrate.py, handlers/wiki_read.py, handlers/wiki_refine.py, handlers/wiki_resolve.py, handlers/wiki_synthesize.py, handlers/wiki_view.py, handlers/wiki_write.py, hooks/agent_briefing_role.py, hooks/agent_briefing_sqlite.py
- main driver importers: hooks/agent_briefing_query.py, hooks/auto_recall.py, hooks/session_start_db.py

### 12. 2026-10-21T01:25Z: increment 2 closed (funnel)

- Measurement: the store funnel census above (`python scripts/repro_store_funnel.py`): 59 connection-reaching lines in 42 files and 3 driver importers on `main`, 0 and 0 on the branch.
- Side measurement while migrating the hook tests (scratch database, dropped after): a hook process that used the shared store without closing it waited 5 s per pool worker thread at exit, 20.7 s for `auto_recall` with 0.57 s of work; with `close_shared_store_on_exit` around `main()`, 0.44 s.
- Schema: version 2 (`backfill_log` in the DDL); a version-1 database is refused at runtime and migrated by the explicit step (`test_pg_migrations.py`, 16 passed).
- Full suite: 10246 passed, 22 skipped (`pytest tests_py`, 545 s).

### 13. 2026-10-21T02:10Z: increment 3, the frame decoder under mutation (before the field checks)

- Command: `python scripts/repro_broker_frame_fuzz.py --no-log --inputs 20000` (seed 1112) against the first decoder, which trusted the JSON type of every tagged field.
- Result: 234 escapes in 20000 inputs. Grouped over 100000 inputs: `AttributeError` from `uuid.UUID` on a non-string (1047), `decimal.InvalidOperation` (97), `OverflowError` from `timedelta` and `Decimal` on huge or infinite numbers (57), `SyntaxError` from `numpy.dtype(",f4")` (2). Each one is a connection thread dying on a stack trace instead of a clean refusal.
- Fix: every tagged field is type-checked before it is built (`broker_codec._field`); text constructors map their errors to `CodecError`; arrays accept only twelve named numeric dtypes and a shape of non-negative integers; base64 is strict. The four reproducers are committed under `fuzz/corpus/fuzz_broker_frame/repro-*` and replayed by `tests_py/fuzz`.
- After: 0 escapes in 200000 inputs (seed 1112) and in 400000 inputs for each of seeds 1 to 5; the entry below is the logged run.

### broker frame fuzz 2026-10-21T01:32Z (`python scripts/repro_broker_frame_fuzz.py --seed 1112`)

- `{"inputs": 200000, "refused": 163366, "accepted": 36634, "escapes": 0, "seconds": 2.3, "first_escape": null}`

### 14. 2026-10-21T02:40Z: increment 3 closed (broker process)

- Broker tests: 67 unit (codec, frames, queues, server and client over sockets) plus 4 endpoint and 5 real-process tests on the test database, all passing; the pipe tests run in the `test-windows` job only (3 skipped here, on macOS).
- Full suite: 10326 passed, 25 skipped (`pytest tests_py`, 517 s).

### 2026-10-21T01:48:28+00:00 bounds

- head: `fa8165b06ce6c14d8d77558b32d5081be93caf02` (dirty)
- command: `python scripts/repro_connection_broker.py bounds`
- pytest exit code: 0 (2 passed in 0.68s)
- measured: `{"batch": {"application_name": "cortex-store-batch", "idle_in_transaction_session_timeout": "30s", "lock_timeout": "30min", "statement_timeout": "30min"}, "direct": {"application_name": "cortex-store-direct", "idle_in_transaction_session_timeout": "30s", "lock_timeout": "30s", "statement_timeout": "30s"}, "interactive": {"application_name": "cortex-store-interactive", "idle_in_transaction_session_timeout": "30s", "lock_timeout": "30s", "statement_timeout": "30s"}}`

### 2026-10-21T01:49:34+00:00 priority

- head: `fa8165b06ce6c14d8d77558b32d5081be93caf02` (dirty)
- command: `python scripts/repro_connection_broker.py priority`
- pytest exit code: 0 (1 passed in 65.05s (0:01:05))
- measured: `{"background": {"heavy_class": "background", "heavy_pages_served": 54, "max_ms": 357.3, "p50_ms": 1.5, "p95_ms": 91.2, "point_reads": 443}, "brokers": 1, "single_class": {"heavy_class": "interactive", "heavy_pages_served": 66, "max_ms": 7024.7, "p50_ms": 5968.4, "p95_ms": 4912.1, "point_reads": 2}}`

- Note on the entry above: its `single_class.p95_ms` (4912.1, below the p50) came from a wrong percentile index on 2 samples (`int(0.95 * (n - 1))` picks the first). Fixed to the nearest-rank index; the next entry is the corrected run.

### 2026-10-21T01:50:57+00:00 priority

- head: `fa8165b06ce6c14d8d77558b32d5081be93caf02` (dirty)
- command: `python scripts/repro_connection_broker.py priority`
- pytest exit code: 0 (1 passed in 70.58s (0:01:10))
- measured: `{"background": {"heavy_class": "background", "heavy_pages_served": 54, "max_ms": 225.4, "p50_ms": 1.6, "p95_ms": 107.6, "point_reads": 329}, "brokers": 1, "single_class": {"heavy_class": "interactive", "heavy_pages_served": 82, "max_ms": 9165.0, "p50_ms": 8452.2, "p95_ms": 9165.0, "point_reads": 2}}`

### 15. 2026-10-21T03:30Z: increment 4 closed (request classes, session bounds, A5)

- A5 and probe A: the `priority` and `bounds` entries above. The first full-suite run after naming store connections failed one test: the saturation baseline identified its children by `PGAPPNAME`, which the store now overrides. Store connections are named `cortex-store-<pool>-<pid>` and the baseline maps backends to children by PID; it reproduces as before (4 of 8 refused or timed out, blocked children holding 3 to 4 connections).
- Full suite: 10329 passed, 26 skipped (`pytest tests_py`, 524 s).
