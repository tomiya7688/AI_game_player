# Runtime Resource Lifecycle（Session Resource Registry / Cleanup Coordinator）

## 背景と目的

Session内で確保・借用される資源（共有Frame、GPU cache、capture session等）の所有者と解放経路を1か所に集め、release・task終了・世代切替・Stop・error・pressure・期限切れを同じ解放経路で処理します（Issue #786、親 #785）。各資源の実解放（#571 Frame、#534 GPU cache、#334 GDI）は再実装せず、owner APIをAdapterとして登録します。読者はruntime資源を追加する実装者です。通常利用者の操作は変わりません。設計の根拠は[設計書](../design/issue-786-session-resource-registry.md)です。

## 構成

| 型 | 責務 |
|---|---|
| `SessionResourceRegistry` | 登録（`acquire`）・借用（`borrow`）・解放（`release`/`close_task`/`advance_generation`/`close_generation`/`close_session`）・`snapshot()`。thread-safe（lock内は表の操作だけ、releaserとTrace sinkはlock外） |
| `CleanupCoordinator` | `on_pressure(ResourcePressureSnapshot)`と`cleanup_expired()`。上限付きで解放 |
| `ResourceReleaser` / `ClosableOwnerAdapter` | owner側の解放を1行で呼ぶAdapter。`close()`優先、無ければ`shutdown()` |
| `ManagedSessionRuntime.resources` | Sessionが所有するRegistry。`close()`の先頭で`close_session()` |

## 状態遷移

`ACTIVE`→（release・scope終了・evict・期限切れ）→`RELEASING`→`RELEASED`または`FAILED`。借用中の解放要求は`RELEASING`（deferred）にし、最後の借用が終わったthreadでreleaserを呼びます。releaserは1つのleaseにつき最大1回で、`FAILED`は自動再試行しません。

## 解放順序

`close_session`はTASK→GENERATION（新しい世代から）→SESSIONの順で、同じ群の中は取得の逆順です。`pinned`はpressure・期限切れのevictionだけを禁止し、scope終了では解放されます。pressure時の順序は期限切れ→`priority`小→作成が古い→`resource_id`で、`elevated`は8件/10ms、`critical`は32件/50msが上限です。`none`は何もしません。

## Adapterの書き方

```python
class FrameReleaser:  # #571 の例
    def __init__(self, shared_buffer): self._buffer = shared_buffer
    def release(self, resource_id): self._buffer.release(resource_id)  # owner APIを1行で呼ぶ

registry.acquire("frame-1", "frame.shared", FrameReleaser(buf), owner_scope=ResourceScope.TASK,
                 estimated_bytes=1_000_000, evictable=True, provenance="pipeline.capture")
```

`acquire`はownerが資源を作った後、consumerへ渡す前に呼びます。登録前に例外が起きた場合の解放はowner自身の責任です。1回の呼出し内で`finally`解放される資源（GDI、planner作業directory）は登録しません。`PROCESS` scopeは拒否します（暗黙にglobal化しない）。

## Trace・#787との接続

`ResourceLifecycleSnapshot`（schema_version 1）と`resource.cleanup/v1`、`resource.release_failed/v1`は、ID・種別・量・状態・時間・例外型名だけを持ちます（例外messageや任意objectは含みません）。Traceの失敗は`trace_failed`に数えるだけで解放を止めません。

## 制約と残件

- releaserが戻らない場合はRegistryから中断できません（owner側の責務）。`close_session`の時間上限（既定2000ms）で打ち切り、再closeで続きから処理します。
- 未接続: pressure発生源（#425/#546）、実ownerのAdapter（#571/#534/#334）、長時間の増加判定（#787）。

最終確認日: 2026-11-07
