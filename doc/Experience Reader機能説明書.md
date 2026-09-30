# Experience Reader

`ai_game_player.experience_reader.JsonlExperienceReader` は、Canonical Episode を1行1件のJSON Lines形式で読み込みます。Experience の長時間イベント追記はこのReaderの責務ではなく、Event Journal (#203) の契約に従います。

## 読み取りと検索

- `get_episode(episode_id)` でEpisodeを取得します。
- `get_step(episode_id, step_id)` で任意のStepを取得します。
- `iter_episodes()` で全件を読み取ります。
- `find(source=..., model_id=..., created_after=..., created_before=...)` で絞り込みます。時刻の境界はタイムゾーン付きISO-8601で指定します。
- `reconstruct_state(episode_id, step_id)` は対象Stepまでの `state.snapshot` と `state.delta` イベントを順に適用します。Snapshot payloadは `{"state": {...}}`、Delta payloadは `{"set": {...}, "remove": ["key"]}` です。Snapshotより前のDeltaや不正payloadはエラーとして扱います。

## 互換性と回復

schema_versionのない旧v0 Episodeは、Episode/Step/Eventの追加フィールドを既定値で補ってschema v1へ読み替えます。未知のフィールドや未対応versionは拒否し、情報を黙って破棄しません。

不正な最終行はクラッシュで書き切れなかったtailとして無視し、`recovery_notices` に記録します。途中行の破損、重複Episode ID、Artifact参照検証エラーは失敗として扱います。任意のcheckpointファイル `<jsonl-path>.checkpoint.json` は `{"line_count": N, "sha256": "..."}` を持ち、先頭N行のSHA-256がデータと一致する場合のみ有効です。checkpointがない／無効でも全レコード走査でindexを再構築します。

`artifact_store` を渡した場合、Episode/Step/Eventに含まれるArtifact参照すべてを読み込み時に検証します。Storeを渡さない場合、Readerは参照の存在を確認しません。
