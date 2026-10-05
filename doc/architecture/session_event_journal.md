# Session Event Journal

`EventEnvelope` in `src/ai_game_player/event_journal.py` is the versioned session-event contract. Each record carries an event/session ID, a contiguous one-based session sequence, a UTC timestamp, a process-monotonic timestamp, event type/status, optional frame/turn/snapshot/correlation IDs, a compact JSON payload, and typed artifact references. `config/session_event.schema.json` describes schema version 1. Large media or model output belongs in the content-addressed Artifact Store; only its `ArtifactReference` is recorded here.

## Storage and recovery

The v1 backend is one SQLite database in WAL mode per session, with one active `EventJournal` writer instance per database. The writer lock serializes concurrent calls through that instance; separate writer instances must not target the same path. Each append inserts one compact envelope and commits its own transaction with `synchronous=FULL`. `flush()` and `close()` additionally request a full WAL checkpoint. Large media and model output are not stored in the database; use `ArtifactReference` to point to the content-addressed Artifact Store.

SQLite provides atomic transaction recovery: an interrupted, uncommitted append is rolled back by SQLite, while committed events remain available. Opening an existing database validates the database and envelope schema versions, session identity, event IDs, and contiguous sequence numbers. A malformed committed record fails closed and is never silently skipped. Reopening without an explicit session ID resumes the session in database metadata. Start a new session at a new database path (or reject a mismatching session ID) rather than mixing sessions in one database.

`tools/benchmark_event_journal.py` measures the production SQLite implementation against a JSONL reference over a default 1,800-event workload (30 minutes at one event per second). Both use the same envelope contract; by default, the reference flushes and calls `fsync` for every event, while SQLite commits each append with `synchronous=FULL`. The benchmark includes p50/p95 append latency, throughput, full-record validation time on reopen, and stored bytes.

Three local Windows runs (CPython 3.14, 128-byte payload) recovered all 1,800 events. Median throughput was about 1,055 events/s for SQLite WAL and 1,178 events/s for the JSONL reference; median p95 append latency was 1.21 ms and 1.05 ms, respectively. Median reopen validation took 43 ms for SQLite and 39 ms for JSONL; median storage was 1.15 MB and 0.88 MB. SQLite was not selected for peak throughput: it was selected for SQLite's transaction-level rollback of interrupted appends, with measured throughput still over 1,000 times the assumed one-event-per-second workload. Results are local sizing evidence, not a cross-machine performance guarantee.

This journal is not yet wired into `GameSessionController`, legacy Trace/History migration, query APIs, or timeline UI; those remain separate integration work.
