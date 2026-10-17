---
created: 2026-10-17T05:21:41Z
kind: adr
number: 1110
status: accepted
tags: [install, launcher, plugin, postgresql, setup, storage]
title: The plugin runs its own PostgreSQL from pinned theseus binaries
---
# ADR-1110: The plugin runs its own PostgreSQL from pinned theseus binaries

## Status

accepted

## Context

Amends ADR-1104 decision 1 (selection order); keeps ADR-1108 (Setup installs, no hook does). Replaces the pg0 design of PR #748 (issue #744), which never merged.

Issue #744 asks for a PostgreSQL the plugin provisions itself, with pgvector and pg_trgm, on a Mac that has no Homebrew (issue #740: the org-managed work Mac). PR #748 embedded pg0 v0.15.2 (vectorize-io/pg0), the latest release (2026-09-14; no later release, its fix pg0#42 unreviewed). Measured 2026-10-17 under `sandbox-exec` with `/opt/homebrew` and `/usr/local` unreadable: pg0's PostgreSQL 18.1.0 loads `/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib` (arm64; `/usr/local/opt/openssl@3` on x86_64) and `pg0 start` dies in dyld; the PR wrote its state before the start, so the failure was permanent. The review of 2026-10-16 also found: hooks ignored `CLAUDE_PLUGIN_OPTION_DATABASE_URL` (two stores), a stale `postmaster.pid` with a reused PID blocked the server (`PermissionError` read as alive), and the password went through argv.

theseus-rs/postgresql-binaries 18.4.0 and later bundle OpenSSL: `otool -L bin/postgres` of 18.6.0 (aarch64 and x86_64 apple-darwin) names only `/usr/lib/libxml2.2.dylib`, `@loader_path/../lib/libssl.3.dylib`, `@loader_path/../lib/libcrypto.3.dylib`, `/usr/lib/libz.1.dylib` and libSystem; 3 of 125 Mach-O files reference a build path, all under `pgxs/src/test`. pgvector_compiled v0.18.320 (pgvector 0.8.5) `vector.dylib` needs only libSystem. The Linux GNU 18.6.0 archives are not self-contained: `postgres` needs the distribution's `libxml2.so.2 libssl.so.3 libcrypto.so.3 libgssapi_krb5.so.2 libzstd.so.1 liblz4.so.1 libz.so.1` and glibc 2.34 (objdump `NEEDED`, version references); musl builds also need ICU 74. The Windows archive bundles its DLLs except `VCRUNTIME140.dll`. theseus commit 78f15cec ("bundle required runtime dependencies", 2026-10-04) is in no release.

## Decision

1. **One system, no pg0.** Setup downloads `postgresql-18.6.0-<triple>.tar.gz` (theseus) and `pgvector-<triple>-pg18.tar.gz` (pgvector_compiled v0.18.320), keeps them only when their sha256 equals the pinned digest, lays pgvector's `lib/` and `share/extension/` over the PostgreSQL tree and stamps it last (`scripts/launcher_pg_assets.py`). Pinned triples: aarch64 and x86_64 apple-darwin, aarch64 and x86_64 unknown-linux-gnu. Any other host (native Windows, musl, other CPUs) raises `UnsupportedError` and keeps SQLite.
2. **Cortex drives PostgreSQL itself.** Under `<plugin data>/pg`: `initdb --pwfile` (the password never reaches argv), `scram-sha-256`, UTF8, C locale, `listen_addresses = '127.0.0.1'`, no unix socket; `pg_ctl start -w` on a free port chosen per start and `pg_ctl stop -m fast -w`, owned by one detached supervisor per root with the three `flock` files of PR #748 (clients, start, supervisor; no timer). `postmaster.pid` is classified by identity: a dead PID or one of another user is stale; a live PID of this user is this cluster's postmaster when its executable is the tree's `bin/postgres` or, under an emulator that reports another executable, when its recorded port accepts loopback connections; a stale file is removed before a start, never on stop (`scripts/launcher_pg_server.py`). Hooks never install: they only start an installed server (ADR-1108).
3. **Selected only after it proved itself (staged).** Setup's step (`scripts/launcher_pg_provision.py`) installs, initialises, starts, creates the `cortex` database, `vector` and `pg_trgm`, checks pgvector's iterative scan and applies the schema; `setup_db._init_schema` now fails on any failed statement. Only then it writes `provisioned.json` and, for an eligible store, the marker `{"backend": "managed", "root": "<plugin data>/pg"}`. A failure writes no selection: the store stays SQLite and the cause is printed to `setup.log`. A store already managed that fails makes Setup fail.
4. **Selection order** (`scripts/launcher_pg_select.py`, `launcher_backend.apply`): (a) a configured database always wins: `database_url` as `DATABASE_URL`, or for every hook and Setup `CLAUDE_PLUGIN_OPTION_DATABASE_URL` mapped onto it first, or an explicit `CORTEX_*` backend setting; nothing managed starts. (b) A `managed` marker attaches the launch to that root and sets `DATABASE_URL` and `CORTEX_MEMORY_STORE_BACKEND=postgresql`; a server that cannot start stops the launch with "Cortex not ready" and its cause, never a silent switch. (c) Otherwise ADR-1104 unchanged: the marker, or the first-launch decision, SQLite by default. Outside the launcher a `managed` marker is an error naming it.
5. **No migration in this change.** A store is eligible when nothing configures one and none exists: no marker and no earlier Cortex state, or the launcher's own first-launch SQLite marker with no SQLite file at `CORTEX_MEMORY_DB_PATH` / `CORTEX_MEMORY_SQLITE_FALLBACK_PATH` (or their default). An existing SQLite store keeps its marker; a later importer moves it and flips the marker to `managed`, the value this change defines.

## Consequences

Positive: on macOS (arm64, and x86_64 under Rosetta) the managed server starts with Homebrew unreadable, `CREATE EXTENSION vector` and `pg_trgm` succeed, and it stops when its last client exits (measured 2026-10-17, `scripts/locked_mac_env/managed_pg_e2e.py` under `no_homebrew.sb`). On Linux GNU it works where the seven libraries are installed (measured: Debian 12 with `libxml2`, Ubuntu 24.04, both CPUs). A failed provision never locks a user out: the next Setup retries while the store is still eligible.

Negative, known: Linux distributions without `libxml2.so.2` (measured: Debian 12 slim without it; Ubuntu 25.10, which ships only `libxml2.so.16`) or without OpenSSL 3 stay on SQLite, with the loader error in `setup.log`, until a theseus release bundles them (78f15cec). Native Windows stays on SQLite: the lifecycle uses POSIX `flock`, and the archive needs the MSVC runtime. A fresh install that skips `claude --init-only` gets the SQLite store at its first launch and keeps it until the importer exists. Setup downloads about 13 MB from GitHub releases once per pinned version; an old tree is not removed when the pin moves. The managed root lives in plugin data: uninstalling the plugin removes the store with it.

Scope of the measurements: macOS arm64 natively and x86_64 under Rosetta on one Mac (macOS 26.6.2); Linux aarch64 natively in containers and Linux x86_64 under qemu emulation on that Mac. The CI job `managed-pg` repeats the proof on macos-latest, ubuntu-latest and ubuntu-24.04-arm, and `install-e2e` checks that a fresh install's first session names the PostgreSQL store. The emulated run is what showed that `/proc/<pid>/exe` names the emulator, hence the loopback-port identity fallback of decision 2. Setup checks eligibility again right before it writes the `managed` marker, so a first launch that chose SQLite while the server was being provisioned keeps its choice.

Other readers of the marker: `mcp_server/__main__.py` and `mcp_server/hooks/entry.py` resolve the backend at import without the plugin launcher (direct `hypermnesia-mcp` launches and the Codex plugin, `plugins/hypermnesia-mcp-codex/scripts/runtime.py`). With a `managed` marker and no `DATABASE_URL` they raise `BackendMarkerError` naming the marker instead of opening another store. The marker is global (`~/.claude/methodology`) while the managed server lives in one plugin's data directory; whether other hosts attach to it is left open.
