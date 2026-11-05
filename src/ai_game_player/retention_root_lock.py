"""Artifact Store rootごとの書込み協調lock（admission/register/collect/recoverが共有する）。"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ai_game_player.cross_process_lock import cross_process_file_lock

RETENTION_LOCK_FILENAME = ".retention.lock"
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
# Windowsのreparse point属性（junction/symlink）。stat定数はWindows版Pythonにしかないため値を固定する。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MILLISECONDS_PER_SECOND = 1000.0


# {
#   責務: [RetentionRootUnsafeError: root・lock fileがsymlink/junction等で、lockも変更も始めずに止めたことを通知する]
#   フィールド: [なし: RuntimeErrorの理由文だけを持つ]
# }
class RetentionRootUnsafeError(RuntimeError):
    """The managed root or its lock file is not a plain directory/file; nothing was changed."""


# {
#   責務: [RetentionLockTimeoutError: 期限内にroot lockを取得できず、transactionを開始しなかったことを通知する]
#   フィールド: [なし: TimeoutErrorの理由文だけを持つ]
# }
class RetentionLockTimeoutError(TimeoutError):
    """The root lock stayed contended past its deadline; the caller changed nothing."""


# {
#   責務: [_RootLockState: 1つのroot keyについて、process内mutexと再入深さ・OS lock保持を所有する]
#   フィールド: [mutex: threadの排他と同一threadの再入を担うRLock, depth: 現在の再入深さ,
#     os_lock: depthが1以上の間OS advisory lockを保持するExitStack]
# }
@dataclass
class _RootLockState:
    mutex: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    os_lock: ExitStack | None = None


_STATES: dict[str, _RootLockState] = {}
_STATES_GUARD = threading.Lock()


# {
#   責務: [is_junction: Windowsのdirectory junction（symlink判定に出ないreparse point）を検出する]
#   処理: [Python 3.12以降はos.path.isjunctionを使う。3.10/3.11ではlstatのreparse point属性で判定し、
#     属性を持たない非Windowsではjunctionなしとする]
#   引数: [path: 検査するpath]
#   戻り値: [bool: Trueならjunctionなどのreparse pointで、書込み・削除の対象にしない]
# }
def is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        return bool(checker(path))
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


# {
#   責務: [require_plain_directory: 既存のpathがsymlink/junctionでない実directoryであることを保証する]
#   処理: [存在しなければ何もしない。存在するのにsymlink・junction・directory以外なら拒否する]
#   引数: [path: 検査するpath, label: エラー文に出す対象名]
#   戻り値: [なし]
#   エラー: [RetentionRootUnsafeError: symlink・junction・directory以外]
# }
def require_plain_directory(path: Path, label: str) -> None:
    if not os.path.lexists(path):
        return
    if path.is_symlink() or is_junction(path) or not path.is_dir():
        raise RetentionRootUnsafeError(
            f"{label} must be a plain directory, not a link: {path}"
        )


# {
#   責務: [_state_for: root keyに対応するprocess内のlock状態を、無ければ作って返す]
#   処理: [実pathを大文字小文字正規化したkeyでprocess共通のtableを引き、registry guard内で作成する]
#   引数: [root: 解決対象のroot directory]
#   戻り値: [_RootLockState: 同じrootなら同じobject]
# }
def _state_for(root: Path) -> _RootLockState:
    key = os.path.normcase(os.path.realpath(root))
    with _STATES_GUARD:
        return _STATES.setdefault(key, _RootLockState())


# {
#   責務: [_acquire_os_lock: 残り時間内にOS advisory lockを取得し、保持用のExitStackを返す]
#   処理: [期限切れなら即座に失敗し、cross_process_file_lockの期限超過を専用例外へ変換する]
#   引数: [lock_path: root直下のlock file, deadline: 単調時計の期限]
#   戻り値: [ExitStack: closeでlockを解放する]
#   エラー: [RetentionLockTimeoutError: 期限超過, OSError: lock fileを開けない]
# }
def _acquire_os_lock(lock_path: Path, deadline: float) -> ExitStack:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RetentionLockTimeoutError(f"retention root lock timed out: {lock_path}")
    stack = ExitStack()
    try:
        stack.enter_context(
            cross_process_file_lock(lock_path, timeout_seconds=remaining)
        )
    except TimeoutError as error:
        stack.close()
        raise RetentionLockTimeoutError(
            f"retention root lock timed out: {lock_path}"
        ) from error
    except BaseException:
        stack.close()
        raise
    return stack


# {
#   責務: [retention_root_lock: 同じStore rootへの書込みtransactionをprocess間・thread間で直列化する]
#   処理: [1: rootとlock fileがlink/junctionでないことを確認し、rootが無ければ作る
#     2: process内RLockを期限付きで取り、最外のacquireだけOS advisory lockを有界待ちで取る
#     3: 同一threadの再入は深さを増やすだけで、最外のreleaseでOS lockとmutexを解放する
#     lockはOSが保持の正本で、mtimeや時刻を根拠に奪わない]
#   補足: [リンク検査とlock fileを開く操作の間の差替えは排除できない（collectorの削除検査と同じ前提で、
#     管理rootは同じユーザーの信頼できるprocessだけが書く）]
#   引数: [root: Artifact Storeのroot directory, timeout_seconds: 取得の総待ち上限秒（有限の正の数）]
#   戻り値: [Iterator[float]: with本体の間lockを保持。値は取得にかかったms（再入はmutexを待たないためほぼ0）]
#   エラー: [ValueError: 待ち上限が正の数でない, RetentionRootUnsafeError: root/lockがlink等,
#     RetentionLockTimeoutError: 期限内に取得できない, OSError: lock fileを開けない]
# }
@contextmanager
def retention_root_lock(
    root: Path, *, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> Iterator[float]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not timeout_seconds > 0
    ):
        raise ValueError("lock timeout must be a positive number of seconds")
    root_path = Path(root)
    require_plain_directory(root_path, "retention root")
    root_path.mkdir(parents=True, exist_ok=True)
    require_plain_directory(root_path, "retention root")
    lock_path = root_path / RETENTION_LOCK_FILENAME
    if os.path.lexists(lock_path) and (
        lock_path.is_symlink() or is_junction(lock_path) or not lock_path.is_file()
    ):
        raise RetentionRootUnsafeError(
            f"retention lock file must be a regular file: {lock_path}"
        )
    state = _state_for(root_path)
    started = time.monotonic()
    deadline = started + timeout_seconds
    if not state.mutex.acquire(timeout=timeout_seconds):
        raise RetentionLockTimeoutError(
            f"retention root lock timed out in-process: {root_path}"
        )
    try:
        if state.depth == 0:
            state.os_lock = _acquire_os_lock(lock_path, deadline)
        state.depth += 1
    except BaseException:
        state.mutex.release()
        raise
    waited_ms = (time.monotonic() - started) * _MILLISECONDS_PER_SECOND
    try:
        yield waited_ms
    finally:
        state.depth -= 1
        try:
            if state.depth == 0 and state.os_lock is not None:
                held, state.os_lock = state.os_lock, None
                held.close()
        finally:
            state.mutex.release()
