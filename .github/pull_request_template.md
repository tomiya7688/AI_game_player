## Summary

<!-- What changed and why? -->

## Validation

<!-- List the relevant commands and results. -->

## Self-review

<!-- Read the change (`git add -N .` then `git diff origin/main`, see the workflow doc "差分の取得") and record each of the 7 points in doc/コーディングルール.md ("自己レビューの観点"): "OK" or "finding and fix". Write "unverified" for anything not checked. -->

1. Correctness:
2. Safety:
3. Responsibility (class/file/function rules):
4. Naming:
5. Structured comments:
6. Tests:
7. Docs and doc/versions.md:

## Local light review

<!-- After your own self-review and before the PR. Run BOTH Claude Haiku (lowest effort) and ChatGPT 6 Luna (lowest effort), read-only. Per model: findings, fixed, rejected with reason, or "not run: reason" (model id unconfirmed / CLI missing / quota / error). Never a substitute for Self-review. -->

- Claude Haiku (lowest effort):
- ChatGPT 6 Luna (lowest effort):

## AI review (CodeRabbit / GitHub Copilot)

<!-- Per bot: findings count, fixed, rejected (with reason), or "not run: quota/no response". Free tiers may be exhausted; that is not a blocker. -->

- CodeRabbit:
- GitHub Copilot:

## Regression coverage (bug fixes)

- Related issue:
- Reproduction test (file::test):
- Confirmed failing before the fix: yes / no / not reproducible
- If not reproducible, explain the limitation and alternative verification: