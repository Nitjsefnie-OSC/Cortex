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
