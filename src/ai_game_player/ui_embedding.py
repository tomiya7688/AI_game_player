import json
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite, sqrt
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ai_game_player.models import DetectedElement
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.ui_recognition import UiPrototype


@dataclass(frozen=True)
class EmbeddingVector:
    lane: str
    values: tuple[float, ...]
    provider: str
    model: str
    version: str
    preprocessing: str
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.lane.strip():
            raise ValueError("embedding lane must not be empty")
        if not self.values:
            raise ValueError("embedding vector must not be empty")
        if any(not isfinite(value) for value in self.values):
            raise ValueError("embedding values must be finite")
        if not self.provider.strip() or not self.model.strip() or not self.version.strip() or not self.preprocessing.strip():
            raise ValueError("embedding metadata must not be empty")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("embedding confidence must be between 0 and 1")

    @property
    def dimension(self) -> int:
        return len(self.values)

    def compatible_with(self, other: "EmbeddingVector") -> bool:
        return (
            self.lane == other.lane
            and self.model == other.model
            and self.version == other.version
            and self.preprocessing == other.preprocessing
            and self.dimension == other.dimension
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EmbeddingVector":
        return cls(
            str(value["lane"]),
            tuple(float(part) for part in value["values"]),
            str(value["provider"]),
            str(value["model"]),
            str(value["version"]),
            str(value["preprocessing"]),
            float(value.get("confidence", 1.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "values": list(self.values),
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "preprocessing": self.preprocessing,
            "dimension": self.dimension,
            "confidence": self.confidence,
        }


class EmbeddingProvider(Protocol):
    name: str
    lane: str
    model: str
    version: str
    preprocessing: str

    def embed(self, frame: ScreenFrame, element: DetectedElement) -> EmbeddingVector | None:
        ...


class GridVisualEmbeddingProvider:
    """Small dependency-free visual baseline for local known-UI retrieval."""

    name = "grid_visual"
    lane = "visual"
    model = "brightness-grid"
    version = "1"

    def __init__(self, grid_size: int = 4) -> None:
        if grid_size < 2:
            raise ValueError("grid size must be at least 2")
        self.grid_size = grid_size
        self.preprocessing = f"brightness-grid-{grid_size}x{grid_size}"

    def embed(self, frame: ScreenFrame, element: DetectedElement) -> EmbeddingVector:
        left, top, width, height = _clamp_bbox(frame, element.bbox)
        values: list[float] = []
        for grid_y in range(self.grid_size):
            y = min(frame.height - 1, top + ((2 * grid_y + 1) * height) // (2 * self.grid_size))
            for grid_x in range(self.grid_size):
                x = min(frame.width - 1, left + ((2 * grid_x + 1) * width) // (2 * self.grid_size))
                offset = (y * frame.width + x) * 4
                blue, green, red = frame.bgra[offset : offset + 3]
                brightness = (red * 299 + green * 587 + blue * 114) / (255 * 1000)
                values.append(brightness)
        return EmbeddingVector(
            self.lane,
            _l2_normalize(values),
            self.name,
            self.model,
            self.version,
            self.preprocessing,
            element.confidence,
        )


class HashedTextEmbeddingProvider:
    """Deterministic semantic baseline; external text/image models can replace it."""

    name = "hashed_text"
    lane = "semantic"
    model = "signed-token-hash"
    version = "1"
    preprocessing = "casefold-whitespace"

    def __init__(self, dimension: int = 32) -> None:
        if dimension < 4:
            raise ValueError("semantic embedding dimension must be at least 4")
        self.dimension = dimension

    def embed(self, frame: ScreenFrame, element: DetectedElement) -> EmbeddingVector | None:
        text = " ".join((element.text or "").casefold().split())
        if not text:
            return None
        values = [0.0] * self.dimension
        for token in text.split():
            digest = sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            values[index] += sign
        return EmbeddingVector(
            self.lane,
            _l2_normalize(values),
            self.name,
            self.model,
            self.version,
            self.preprocessing,
            element.confidence,
        )


@dataclass(frozen=True)
class EmbeddingProviderConfig:
    provider: EmbeddingProvider
    enabled: bool = True
    fallback: bool = False


@dataclass(frozen=True)
class EmbeddingProviderStatus:
    provider: str
    lane: str
    state: str
    cache_hit: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[EmbeddingVector, ...]
    statuses: tuple[EmbeddingProviderStatus, ...]

    def lanes(self) -> dict[str, list[EmbeddingVector]]:
        result: dict[str, list[EmbeddingVector]] = {}
        for vector in self.vectors:
            result.setdefault(vector.lane, []).append(vector)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "vectors": [vector.to_dict() for vector in self.vectors],
            "statuses": [status.to_dict() for status in self.statuses],
        }


class EmbeddingCache:
    """Exact crop/provider cache. Optional path makes the cache persistent."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._values: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("embedding cache must be an object")
            self._values = {str(key): value for key, value in raw.items() if isinstance(value, dict)}

    def get(self, key: str) -> EmbeddingVector | None:
        value = self._values.get(key)
        return EmbeddingVector.from_dict(value) if value is not None else None

    def put(self, key: str, vector: EmbeddingVector) -> None:
        self._values[key] = vector.to_dict()
        self._flush()

    def __len__(self) -> int:
        return len(self._values)

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(self._values, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class EmbeddingPipeline:
    """Runs independent embedding lanes while preserving model-specific spaces."""

    def __init__(self, providers: list[EmbeddingProviderConfig], cache: EmbeddingCache | None = None) -> None:
        self.providers = list(providers)
        self.cache = cache or EmbeddingCache()

    def embed(self, frame: ScreenFrame, element: DetectedElement) -> EmbeddingBatch:
        primary = [config for config in self.providers if not config.fallback]
        fallback = [config for config in self.providers if config.fallback]
        vectors, statuses = self._run(primary, frame, element)
        present_lanes = {vector.lane for vector in vectors}
        for config in fallback:
            provider = config.provider
            if not config.enabled:
                statuses.append(EmbeddingProviderStatus(provider.name, provider.lane, "disabled"))
            elif provider.lane in present_lanes:
                statuses.append(EmbeddingProviderStatus(provider.name, provider.lane, "skipped"))
            else:
                added, added_status = self._run([config], frame, element)
                vectors.extend(added)
                statuses.extend(added_status)
                present_lanes.update(vector.lane for vector in added)
        return EmbeddingBatch(tuple(vectors), tuple(statuses))

    def _run(
        self,
        configs: list[EmbeddingProviderConfig],
        frame: ScreenFrame,
        element: DetectedElement,
    ) -> tuple[list[EmbeddingVector], list[EmbeddingProviderStatus]]:
        vectors: list[EmbeddingVector] = []
        statuses: list[EmbeddingProviderStatus] = []
        for config in configs:
            provider = config.provider
            if not config.enabled:
                statuses.append(EmbeddingProviderStatus(provider.name, provider.lane, "disabled"))
                continue
            key = self._cache_key(provider, frame, element)
            cached = self.cache.get(key)
            if cached is not None:
                vectors.append(cached)
                statuses.append(EmbeddingProviderStatus(provider.name, provider.lane, "success", True))
                continue
            try:
                vector = provider.embed(frame, element)
                if vector is None:
                    statuses.append(EmbeddingProviderStatus(provider.name, provider.lane, "empty"))
                    continue
                self._validate_provider_vector(provider, vector)
                self.cache.put(key, vector)
                vectors.append(vector)
                statuses.append(EmbeddingProviderStatus(provider.name, provider.lane, "success"))
            except Exception as exc:  # Providers remain independent failure domains.
                statuses.append(EmbeddingProviderStatus(provider.name, provider.lane, "error", error=str(exc)))
        return vectors, statuses

    def _validate_provider_vector(self, provider: EmbeddingProvider, vector: EmbeddingVector) -> None:
        expected = (provider.name, provider.lane, provider.model, provider.version, provider.preprocessing)
        actual = (vector.provider, vector.lane, vector.model, vector.version, vector.preprocessing)
        if actual != expected:
            raise ValueError("embedding provider returned mismatched metadata")

    def _cache_key(self, provider: EmbeddingProvider, frame: ScreenFrame, element: DetectedElement) -> str:
        digest = sha256()
        digest.update("|".join((provider.name, provider.lane, provider.model, provider.version, provider.preprocessing)).encode("utf-8"))
        digest.update((element.text or "").encode("utf-8"))
        left, top, width, height = _clamp_bbox(frame, element.bbox)
        digest.update(f"|{left}|{top}|{width}|{height}|".encode("ascii"))
        row_bytes = frame.width * 4
        for y in range(top, top + height):
            start = y * row_bytes + left * 4
            digest.update(frame.bgra[start : start + width * 4])
        return digest.hexdigest()


@dataclass(frozen=True)
class EmbeddingRecord:
    record_id: str
    game_id: str
    identity_id: str
    element_type: str
    vector: EmbeddingVector
    text: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EmbeddingRecord":
        return cls(
            str(value["record_id"]),
            str(value["game_id"]),
            str(value["identity_id"]),
            str(value["element_type"]),
            EmbeddingVector.from_dict(value["vector"]),
            str(value["text"]) if value.get("text") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "game_id": self.game_id,
            "identity_id": self.identity_id,
            "element_type": self.element_type,
            "vector": self.vector.to_dict(),
            "text": self.text,
        }


@dataclass(frozen=True)
class EmbeddingPrototype:
    prototype_id: str
    ui_prototype_id: str
    identity_id: str
    vector: EmbeddingVector
    source_games: tuple[str, ...]
    representative_record_id: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EmbeddingPrototype":
        return cls(
            str(value["prototype_id"]),
            str(value["ui_prototype_id"]),
            str(value["identity_id"]),
            EmbeddingVector.from_dict(value["vector"]),
            tuple(str(game) for game in value["source_games"]),
            str(value["representative_record_id"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prototype_id": self.prototype_id,
            "ui_prototype_id": self.ui_prototype_id,
            "identity_id": self.identity_id,
            "vector": self.vector.to_dict(),
            "source_games": list(self.source_games),
            "representative_record_id": self.representative_record_id,
        }


@dataclass(frozen=True)
class EmbeddingNeighbor:
    identity_id: str
    similarity: float
    record_id: str
    game_id: str


@dataclass(frozen=True)
class PrototypeNeighbor:
    identity_id: str
    similarity: float
    prototype_id: str
    ui_prototype_id: str


class EmbeddingMemory:
    """Persistent exact-search baseline with explicit UI prototype linkage."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, game_id: str, identity_id: str, element: DetectedElement, batch: EmbeddingBatch) -> list[EmbeddingRecord]:
        if not game_id.strip() or not identity_id.strip():
            raise ValueError("game_id and identity_id must not be empty")
        data = self._read()
        records: list[EmbeddingRecord] = []
        for vector in batch.vectors:
            record = EmbeddingRecord(uuid4().hex, game_id, identity_id, element.element_type, vector, element.text)
            data["records"].append(record.to_dict())
            records.append(record)
        self._write(data)
        return records

    def records(self) -> list[EmbeddingRecord]:
        return [EmbeddingRecord.from_dict(value) for value in self._read()["records"]]

    def prototypes(self) -> list[EmbeddingPrototype]:
        return [EmbeddingPrototype.from_dict(value) for value in self._read()["prototypes"]]

    def nearest(
        self,
        query: EmbeddingVector,
        *,
        top_k: int = 5,
        game_id: str | None = None,
        exclude_game_id: str | None = None,
    ) -> list[EmbeddingNeighbor]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        result: list[EmbeddingNeighbor] = []
        for record in self.records():
            if game_id is not None and record.game_id != game_id:
                continue
            if exclude_game_id is not None and record.game_id == exclude_game_id:
                continue
            if not query.compatible_with(record.vector):
                continue
            result.append(EmbeddingNeighbor(record.identity_id, _cosine(query.values, record.vector.values), record.record_id, record.game_id))
        result.sort(key=lambda value: value.similarity, reverse=True)
        return result[:top_k]

    def nearest_prototypes(self, query: EmbeddingVector, top_k: int = 5) -> list[PrototypeNeighbor]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        result = [
            PrototypeNeighbor(prototype.identity_id, _cosine(query.values, prototype.vector.values), prototype.prototype_id, prototype.ui_prototype_id)
            for prototype in self.prototypes()
            if query.compatible_with(prototype.vector)
        ]
        result.sort(key=lambda value: value.similarity, reverse=True)
        return result[:top_k]

    def build_from_ui_prototypes(self, ui_prototypes: list[UiPrototype]) -> list[EmbeddingPrototype]:
        records = self.records()
        built: list[EmbeddingPrototype] = []
        for ui_prototype in ui_prototypes:
            matching = [
                record
                for record in records
                if record.identity_id == ui_prototype.visual_id and record.game_id in ui_prototype.source_games
            ]
            spaces: dict[tuple[str, str, str, str, int], list[EmbeddingRecord]] = {}
            for record in matching:
                vector = record.vector
                key = (vector.lane, vector.model, vector.version, vector.preprocessing, vector.dimension)
                spaces.setdefault(key, []).append(record)
            for space_records in spaces.values():
                first = space_records[0]
                centroid = _l2_normalize(
                    [sum(record.vector.values[index] for record in space_records) / len(space_records) for index in range(first.vector.dimension)]
                )
                vector = EmbeddingVector(
                    first.vector.lane,
                    centroid,
                    "prototype_centroid",
                    first.vector.model,
                    first.vector.version,
                    first.vector.preprocessing,
                    sum(record.vector.confidence for record in space_records) / len(space_records),
                )
                representative = max(space_records, key=lambda record: _cosine(record.vector.values, centroid))
                identity = f"{ui_prototype.prototype_id}|{vector.lane}|{vector.model}|{vector.version}|{vector.preprocessing}"
                built.append(
                    EmbeddingPrototype(
                        "embedding-prototype-" + sha256(identity.encode("utf-8")).hexdigest()[:12],
                        ui_prototype.prototype_id,
                        ui_prototype.visual_id,
                        vector,
                        tuple(sorted({record.game_id for record in space_records})),
                        representative.record_id,
                    )
                )
        data = self._read()
        data["prototypes"] = [prototype.to_dict() for prototype in built]
        self._write(data)
        return built

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {"records": [], "prototypes": []}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("records"), list) or not isinstance(value.get("prototypes"), list):
            raise ValueError("embedding memory must contain records and prototypes arrays")
        return value

    def _write(self, value: dict[str, list[dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


@dataclass(frozen=True)
class EmbeddingRetrievalCase:
    query: EmbeddingVector
    expected_identity_id: str
    game_id: str


@dataclass(frozen=True)
class EmbeddingRetrievalMetrics:
    query_count: int
    hit_at_1: int
    hit_at_k: int
    accuracy_at_1: float
    recall_at_k: float


class EmbeddingRetrievalEvaluator:
    def evaluate(
        self,
        memory: EmbeddingMemory,
        cases: list[EmbeddingRetrievalCase],
        *,
        top_k: int = 5,
        cross_game: bool = False,
    ) -> EmbeddingRetrievalMetrics:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        hit_at_1 = 0
        hit_at_k = 0
        for case in cases:
            neighbors = memory.nearest(
                case.query,
                top_k=top_k,
                exclude_game_id=case.game_id if cross_game else None,
                game_id=None if cross_game else case.game_id,
            )
            identities = [neighbor.identity_id for neighbor in neighbors]
            hit_at_1 += int(bool(identities) and identities[0] == case.expected_identity_id)
            hit_at_k += int(case.expected_identity_id in identities)
        count = len(cases)
        return EmbeddingRetrievalMetrics(
            count,
            hit_at_1,
            hit_at_k,
            hit_at_1 / count if count else 1.0,
            hit_at_k / count if count else 1.0,
        )


def _clamp_bbox(frame: ScreenFrame, bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, width, height = bbox
    left = min(max(0, left), frame.width - 1)
    top = min(max(0, top), frame.height - 1)
    width = max(1, min(width, frame.width - left))
    height = max(1, min(height, frame.height - top))
    return left, top, width, height


def _l2_normalize(values: list[float] | tuple[float, ...]) -> tuple[float, ...]:
    norm = sqrt(sum(value * value for value in values))
    if norm == 0:
        return tuple(0.0 for _ in values)
    return tuple(value / norm for value in values)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
