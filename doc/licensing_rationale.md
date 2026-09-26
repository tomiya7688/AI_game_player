# ライセンス方針と、その意図

この文書は法的なライセンス本文ではなく、**AI Game Player Projectがなぜ現在のライセンス構成を選んでいるか**を説明するためのものです。

実際の利用条件は、それぞれの `LICENSE`、Asset PackのLicense、Model/Runtimeのupstream Licenseを確認してください。

## まず結論

Projectでは、すべてを1つのLicenseへ無理に統一することより、次の3点を優先します。

1. **1.0.0を気軽に入手して試せること**
2. **1.1以降の機能をLicense都合で削らないこと**
3. **どのAsset / Model / RuntimeにどのLicenseが適用されるか、利用者が分かること**

そのため、Release世代とArtifact種類ごとにLicenseの扱いが異なります。

## なぜ1.0.0だけMIT中心なのか

1.0.0は、このProjectを初めて一般利用できる形で公開するVersionです。

最初のVersionでCharacter Assetや事前学習済みModel等まで複数Licenseでまとめると、利用者が「とりあえず試したい」だけでもLicense構成を理解する必要が生じます。

そこで1.0.0では、Project側が直接同梱する主要PayloadをできるだけMITで単純化します。

1.0.0の考え方:

```text
Project code / docs       -> MIT
Bundled Player asset      -> すーぱーあいこん / MIT
Wise Misk artwork         -> 非同梱（License未確定）
Kadoka / Maru             -> 非同梱（Obake License）
Third-party Model/Runtime -> upstream Licenseのまま自動取得
```

これは「MITが他のLicenseより優れている」という意味ではありません。

**最初のReleaseだけ、入手・再配布・検証の入口を単純にしたい**というDistribution上の判断です。

## なぜ開発中は賢者ミスクなのに、1.0.0ではすーぱーあいこんなのか

賢者ミスクはDevelopment Default Playerです。

Player Seat、状態画像、Player Profile、Learning UI等を実際のCharacterを使って開発するために利用します。

ただし賢者ミスクの最終Asset Licenseはまだ決めていません。将来、Character向けの公開Licenseを別途設計する可能性があります。

一方、すーぱーあいこんは既にMITで公開されており、1.0.0の「MIT中心の配布物」にそのまま使えます。

したがって、

```text
Development profile -> 賢者ミスク
1.0.0 release profile -> すーぱーあいこん
```

としています。

これはCharacterの優劣や公式性の順位ではなく、**Release時点のLicense境界を明確にするための差し替え**です。

## 「1.0.0はMIT」とThird-party Modelの関係

1.0.0がMIT中心でも、実行時に利用するすべてのArtifactがMITという意味ではありません。

例えば現在のModel候補にはApache-2.0のModelがあります。

それらをProjectのMIT Assetとして再配布するのではなく、1.0.0ではApplicationが必要に応じて自動取得します。

```text
root executable
 -> Model/Runtime名とLicenseを表示
 -> pinned artifactを取得
 -> SHA-256 / provenanceを検証
 -> application-managed storageへ配置
 -> 利用開始
```

ユーザーへ手動downloadやModel Server構築を要求しない一方で、**元のLicenseを隠したりMITへ見せかけたりもしない**ことを目的としています。

## なぜ1.1以降はMIT-onlyを維持しないのか

1.1以降では、次のようなものを直接配布したくなる可能性があります。

- 事前学習済みModel
- fine-tune / LoRA / Adapter
- 賢者ミスク等のCharacter Asset
- Kadoka / Maru
- 追加Runtime
- Game-specific Profile
- Community Asset Pack

これらを「MITだけで配れるもの」に限定すると、Licenseの都合だけで機能やModelを削ることになります。

そのため1.1以降は、**適切なLicenseを明示したmulti-license Distributionを通常形として許容**します。

Projectは1.0.0のために用意したMIT-only構成を、将来Versionでも並行保守することを約束しません。

## では1.0.0を使い続けたい人はどうするのか

MIT中心の1.0.0を気に入って、そのVersionをベースに使い続けたい人も想定しています。

そのため1.0ではCoreをできるだけ小さくし、後続機能を直接Coreへ埋め込みすぎない方針を取ります。

安定した拡張境界として、例えば次を用意します。

- Provider API
- Player / Model Profile
- StageEnvelope
- Processing Hook
- Capability declaration
- MOD / Feature Pack

さらに1.1以降のReleaseでは **Update Content Sheet** を用意します。

そこには、追加された機能について次の情報を記録します。

- 何が追加されたか
- どのContract / Schemaを使うか
- 必要なDependency
- 必要なLicense
- Safety / Privacyへの影響
- 1.0へMODとして持ち込めるか
- 一部だけbackportできるか
- 新しいCoreが必要か

つまり、

**「MIT-only版を公式に作り続ける」のではなく、「1.0を自分で延命・拡張しやすい情報と境界を残す」**

という方針です。

## Asset LicenseをUIに表示する理由

Player Asset PackはCharacterによってLicenseが異なる可能性があります。

だからといって、画像を選ぶたびにLicense全文をModalで表示すると通常操作を邪魔します。

そのためAsset選択UIでは、

```text
賢者ミスク
License: <license name>
[選択] [詳細を見る]
```

のように、**License名は常に見えるが、全文は必要な時だけ開く**形にします。

Asset Packには最低限次のmetadataを持たせます。

- license name
- source
- version/hash
- author
- detail / notice

ModelやRuntimeも同じ考え方で、License情報を隠さず、通常操作を過剰に妨げない表示を目指します。

## Character Licenseについて

現時点では:

- すーぱーあいこん: MIT
- 賢者ミスク: 未決定
- Kadoka / Maru: Obake License

です。

賢者ミスクについては、MITへ固定する必要がなくなったため、将来Character向けのOSS/Free Licenseを検討します。

そのLicenseは、単に利用を制限するためではなく、

- 自由利用
- 改変
- 再配布
- Commercial use
- Attribution
- Character名
- 派生Character
- 公式/非公式の区別
- Model/fine-tuneとの関係

をCharacter用途に合う形で分かりやすくすることが目的です。

## User / Contributorが確認すべきもの

用途ごとに正本を分けます。

- Project code: root `LICENSE`
- Player/Character Asset: `ASSET_LICENSES.md` + Asset Pack metadata
- Third-party Model/Runtime/Game: `THIRD_PARTY_NOTICES.md` + manifest
- 1.0.0 Release境界: `doc/release_1_0.md`
- Project判断の理由: **この文書**
- Character向け将来License検討: Issue #193
- 後続機能の1.0 backport情報: Issue #192 / Update Content Sheet

## 原則

ProjectのLicense運用で最も重視するのは、

**「利用者が意図せず違うLicenseのArtifactを使っていた」という状態を作らないこと**

です。

Licenseを増やさないこと自体を目的にはしません。

機能やAssetに適したLicenseを使い、その代わり、

- 何に
- どのLicenseが
- なぜ適用されているか

を、通常ユーザーにも理解できる形で示します。
