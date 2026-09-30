# Canonical Experience Schema

`ExperienceEpisode`, `ExperienceStep`, and `ExperienceEvent` in `src/ai_game_player/experience.py` define the storage-independent logical record. `config/experience.schema.json` is the versioned JSON Schema; `from_dict` methods enforce the runtime contract without a schema-library dependency.

Episodes contain uniquely identified steps ordered by unique sequence numbers; steps contain uniquely identified events. The records retain timezone-qualified timestamps, event source, optional model identity/version, typed content-addressed artifact references, and optional provenance including source, actor/model, confidence, and evidence-event linkage. Artifact references carry a SHA-256 digest, MIME type, and an explicit sensitive-data flag. Payloads remain JSON objects so domain-specific event types can evolve without changing the envelope.

`ExperienceReader` is the logical read API (`get_episode` and `iter_episodes`). `InMemoryExperienceReader` provides a small adapter for tests and backend conformance; physical append logs, artifact storage, migration, and recovery belong to Issues #167 and #168. This contract does not define retention or compression.
