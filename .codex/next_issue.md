# Next Issue

Issue: #92 — [CI] Required Checks / Main Protection：CI未通過変更のmergeを防ぐbranch rulesetを整備する
Priority: P1
Labels: P1
Source of Truth: https://github.com/tomiya7688/AI_game_player/issues/92

## Requirements excerpt (not a replacement for the Issue)
## 種別
開発運用 / CI / Repository Protection

## 概要
CIを強化してもmerge時に必須でなければQuality Gateとして機能しないため、main branchへの通常mergeで必要status check・review条件を要求するruleset / branch protection方針を整備する。

## 背景 / 問題
現時点でrepository rulesetは設定されていない。Contributor増加後に、人手ミスや急ぎ対応でCI未完了・失敗状態の変更がmainへ入る経路を残したままだと、CI強化の効果が限定される。

## 目的
- mainへ入る変更が最低限のCI Gateを通るようにする
- stale approval / outdated branchの扱いを決める
- high-risk変更ではreview policyと連動する
- emergency overrideを必要時だけ明示的に扱う
- required check名を安定させる

## スコープ
- GitHub Repository Ruleset / branch protection
- required status checks
- required reviewとの連携
- branch up-to-date要求の採否
- force push / delete制御
- admin/maintainer bypass方針
- emergency hotfix override + follow-up regression
- check名変更時の運用
- merge queue導入可否

## 非対象
- maintainerが一切overrideできない運用
- CI failure原因を確認せずcheckを解除すること
- docs-only変更へ不要に重い全platform Gateを強制すること

## 実装 / 設計方針
#91で定義したFast Gateの安定jobをrequired checksとし、変更領域別の追加GateはPR policyと連携する。emergency bypass時は理由とfollow-up Issue/testを必須にする方針を検討する。

## 完了条件
- [ ] main protection方針が定義されている
- [ ] 必須status check一覧が定義されている
- [ ] CI failure/未完了状態の通常mergeを防げる
- [ ] review policy #79と整合している
- [ ] force push / delete方針がある
- [ ] emergency bypassの記録・follow-upルールがある
- [ ] check名称変更時にprotectionが無効化されない運用がある
- [ ] merge queue導入可否が評価されている

## 関連Issue / 依存関係
- #79 PR Review / Merge Policy
- #82 Role / Trust Ladder
- #85 Contributor-scale Quality Gate
- #91 Fast PR / Deep Scheduled CI

## 補足
Quality Gateは「CIが存在する」ではなく、**通常経路では通過しなければmainへ入れない**ところまでを一体で設計する。

## Working-tree paths
- No relevant changed or untracked repository paths were reported by Git.

## Related module and symbol candidates
Confidence is a heuristic match strength, not a probability or a verified dependency.
- `src/ai_game_player/__main__.py` — confidence 0.84; Issue text names symbol(s): main; shared identifier(s): main
Symbol candidates:
- `ai_game_player.__main__.main` — confidence 0.84; Issue text names this qualified or terminal symbol

## Candidate tests and checks
- Test candidate `tests/test_candidate_merger.py` — confidence 0.88; Repository Map dependency connects this test to `ai_game_player.candidate_merger`; test filename matches `src/ai_game_player/candidate_merger.py`
- Required project check: `finish_task.bat`.
- Treat candidate paths as navigation hints; confirm behavior in source and tests.

## Read only if needed
- Use the AGENTS.md task router for source/tests.

## Evidence / stop conditions
- Discussion is not loaded. Read relevant comments at the Issue URL when requirements are unclear.
- Determine Goal / Required / Acceptance, then stop broad exploration.
- Missing or truncated requirements must be checked at the original Issue before implementation.
- Search first; read target source/tests, then direct dependencies only as needed.
- Use targeted tests while implementing; finish_task.bat remains mandatory before commit.
- Report unexecuted runtime/GUI checks as Unverified.
