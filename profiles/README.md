# Profiles

Profiles are user-facing configuration bundles, not alternate implementations.

Planned profile classes include `Default`, `Lightweight`, `Performance`, `Safe`, `Experimental`, and per-game custom profiles.

Rules:

- Basic users should not need to edit profile files manually.
- Advanced changes should be saveable, reversible, and exportable/importable.
- Optional providers/features must remain optional unless marked safety-required.
- Profiles may tune safety within an allowed envelope but may not disable minimum emergency-stop/fail-closed guarantees.
- Profile schemas must be versioned before external sharing becomes supported.
