"""一回の判断へ固定する、観測とは別のKnowledge soft evidence。"""

from dataclasses import dataclass
import json
from typing import Any, Mapping, cast


PROJECTION_VERSION = 1
MAX_MODEL_CONTEXT_BYTES = 4096


# {
#   責務: [KnowledgeProjection: 参照Knowledgeの版・出所・soft hintを一回の判断へ固定する]
#   フィールド: [_payload: canonical JSONの不変bytes。実観測や許可候補を置き換えない,
#     expose_to_models: 明示的に有効にした場合だけモデル要求の圧縮contextへ含める]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeProjection:
    _payload: bytes
    expose_to_models: bool = False

    # {
    #   責務: [__post_init__: 投影の契約版と判断参照、公開設定を検証する]
    #   処理: [JSON objectと参照IDを確認し、可変入力やモデルによる版の書き換えを残さない]
    #   引数: [self: 構築中の投影]
    #   戻り値: [なし]
    #   エラー: [ValueError: JSON・契約版・判断参照・公開設定が不正]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self._payload, bytes)
            or type(self.expose_to_models) is not bool
        ):
            raise ValueError(
                "knowledge projection requires immutable bytes and boolean exposure"
            )
        try:
            payload = json.loads(self._payload)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("invalid knowledge projection JSON") from error
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != PROJECTION_VERSION
            or not isinstance(payload.get("context_snapshot_id"), str)
            or not payload["context_snapshot_id"]
        ):
            raise ValueError(
                "invalid knowledge projection contract or context reference"
            )

    # {
    #   責務: [from_dict: 投影を可変辞書から独立した値へ変換する]
    #   処理: [有限数だけをJSONへ変換し、canonical bytesとして保持する]
    #   引数: [payload: Adapterが検証済みKnowledgeから作った投影,
    #     expose_to_models: モデルcontextへの明示的公開許可]
    #   戻り値: [KnowledgeProjection: 入力辞書の後続変更に影響されない投影]
    #   エラー: [TypeErrorまたはValueError: JSONへ変換できない、非有限値または契約が不正]
    # }
    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, expose_to_models: bool = False
    ) -> "KnowledgeProjection":
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return cls(encoded, expose_to_models)

    # {
    #   責務: [to_dict: Traceや次の処理へ投影の独立した写しを渡す]
    #   処理: [保持bytesから新しいJSON objectを作る]
    #   引数: [self: step中に固定された投影]
    #   戻り値: [dict: 元の投影を変更せず利用できるsoft evidence]
    # }
    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._payload))

    # {
    #   責務: [to_model_dict: 小さいモデルへ渡すKnowledge補助文脈を4KiB以内へ圧縮する]
    #   処理: [版・scope・出所digestを残し、prior、ROI、遷移を容量内で順に追加する。
    #     省略件数を示し、完全な出所はローカルTraceとSnapshotへ残す]
    #   引数: [self: stepに固定した投影]
    #   戻り値: [dict: 実観測と別種の補助文脈。省略を未知のまま表す]
    # }
    def to_model_dict(self) -> dict[str, Any]:
        full = self.to_dict()
        compact = {
            key: full[key]
            for key in (
                "schema_version",
                "context_snapshot_id",
                "status",
                "reason",
                "snapshot_id",
                "snapshot_version",
                "snapshot_sha256",
                "scope",
                "sample_count",
                "confidence",
                "weight_hints",
                "threshold_hints",
            )
        }
        compact["evidence_type"] = "knowledge_soft_evidence"
        compact["provenance"] = {
            key: full["provenance"][key]
            for key in ("update_rule", "update_rule_version")
            if key in full["provenance"]
        }
        compact.update({"priors": {}, "roi_hints": {}, "transition_priors": []})
        omitted = {
            name: full["truncated"][name]
            for name in ("candidate_priors", "roi_hints", "transition_priors")
        }
        for name, limit in (("priors", 64), ("roi_hints", 8), ("transition_priors", 8)):
            source = full[name]
            entries = list(source.items()) if isinstance(source, dict) else list(source)
            for index, entry in enumerate(entries):
                if index >= limit:
                    break
                if isinstance(source, dict):
                    key, value = entry
                    compact[name][key] = value
                else:
                    compact[name].append(entry)
                if (
                    len(json.dumps(compact, separators=(",", ":")).encode("utf-8"))
                    > MAX_MODEL_CONTEXT_BYTES - 256
                ):
                    if isinstance(source, dict):
                        compact[name].pop(key)
                    else:
                        compact[name].pop()
                    break
            retained = len(compact[name])
            omitted["candidate_priors" if name == "priors" else name] += (
                len(entries) - retained
            )
        compact["omitted"] = omitted
        return compact
