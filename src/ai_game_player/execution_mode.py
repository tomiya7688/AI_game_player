def execution_labels(live_execution: bool) -> tuple[str, str, str]:
    if live_execution:
        return "判断＋実行（実入力）", "連続実行開始（実入力）", "実入力: 許可中"
    return "判断＋実行（dry-run）", "連続dry-run開始", "実入力: 無効"