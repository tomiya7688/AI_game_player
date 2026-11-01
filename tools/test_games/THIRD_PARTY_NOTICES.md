<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# 独立テストゲームの依存と権利表記

この一覧は開発用fixtureの固定依存を記録します。本体の通常runtimeとは独立した環境へ導入し、通常アプリの配布対象には含めません。

| 配布名・固定版 | 上流 | 観測した配布metadataの宣言 |
|---|---|---|
| pygame-ce 2.5.7 | https://github.com/pygame-community/pygame-ce | LGPL v2.1 |
| python-chess 1.999 | https://github.com/niklasf/python-chess | GPL-3.0+（chessへのメタパッケージ） |
| chess 1.11.2 | https://github.com/niklasf/python-chess | GPL-3.0+ |
| python-shogi 1.1.1 | https://github.com/gunyarakun/python-shogi | GPL-3.0-only |

2026-11-01の独立環境への実インストールで確認しました。`python_shogi-1.1.1.dist-info/LICENSE`はGNU GPL version 3の本文です。元branchのREADMEにあったLGPLという記載を、固定版の実際の宣言へ訂正しました。

このゲーム集の新規コード・起動スクリプト・専用テストは、元commitのGPL-3.0-or-later宣言と[LICENSE.txt](LICENSE.txt)を保持し、各sourceへSPDXを記録します。第三者パッケージの条件は各上流・配布物の本文で確認してください。宣言や本文の記録だけで権利・再配布条件の確認を完了したとは扱いません。配布する場合のnotice・source等は#124/#228へ追跡します。

外部画像・音声は同梱していません。文字表示にはOSのフォントを使い、そのフォントをこのfixtureから再配布しません。開発用のpytest・Ruffは専用環境だけで使用します。X11入力受入れにはpython-xlib 0.33（配布metadataはLGPLv2+）を専用テスト環境で使用します。通常ゲーム起動や本体runtimeからはimportしません。
