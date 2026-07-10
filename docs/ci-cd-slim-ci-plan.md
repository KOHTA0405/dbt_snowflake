# CI/CDでのSlim CI(state:modified)導入案

PRで変更されたモデルとその下流だけをビルド・テストする「Slim CI」を導入するための設計メモ。まだ実装はしておらず、将来着手する際の参考として残す。

## 目的

PRのたびにプロジェクト全体をビルドするのではなく、本番の状態と比較して**変更されたモデル(とその下流)だけ**を対象に実行し、CIの時間とコストを抑える。

## 基本の仕組み

1. 本番の`dbt build`が成功するたびに、その時点の`manifest.json`をどこかに永続化しておく(「最新の正しい状態」)
2. CI(PR時など)で、保存済みの`manifest.json`を取得し、以下のように実行する

```bash
dbt build --select state:modified+ --state ./state --defer
```

- `state:modified+`: 定義が変更されたモデルと、その下流ノードだけを選択する
- `--defer --state`: 選択されなかった上流モデルは再ビルドせず、本番のテーブル/ビューをそのまま参照して依存関係を解決する

## このプロジェクト特有の論点

### 1. 比較対象となる「本番環境」がまだ無い

現状の`jaffle_shop/profiles.yml`には`dev`(ローカル)と`cloud`(Prefect Cloud)の2ターゲットしかなく、どちらも同じ`database: DEV`を向いている。Slim CIは「本番の状態」と「PRでの変更」を比較する仕組みなので、独立した本番環境が必要。

**決定**: 新しく`prod`ターゲット/`PROD`データベースを作成する方針とした(Snowflake側の作業が別途必要)。

- `dev`: ローカル開発用
- `cloud`: CI用のスクラッチDBとして実行(PRごとの検証用)
- `prod`: 新規。Prefectの本番定期実行はこちらに変更し、build成功時の`manifest.json`をstateとして保存する

### 2. `manifest.json`の永続化先

本番run成功時に生成される`manifest.json`を、CIから参照できる場所に保存する必要がある。Prefect Cloudのマネージド実行は使い捨てのコンテナのため、明示的に永続化しないと実行終了と同時に消えてしまう。

**選択肢**:

| 方法 | メリット | デメリット |
| --- | --- | --- |
| クラウドオブジェクトストレージ(S3等) | dbtコミュニティで最も一般的な定石。ファイルの保存・バージョン管理に向く。Git履歴を汚さない | 新しいクラウドプロバイダのアカウント・認証情報が必要 |
| Snowflake内部ステージ | 既存のSnowflake接続情報でそのまま`PUT`/`GET`可能。新規クラウドアカウント不要 | このプロジェクト特有の代替案(一般的な定石ではない) |
| GitHubの専用ブランチにコミット | 追加インフラ不要、既存のGitHub連携だけで完結 | バイナリ的なファイルをgit管理する形になり、履歴が汚れる |

一般的にはS3などのクラウドストレージが定石だが、このプロジェクトはAWS/GCPのアカウントを持たずSnowflakeに閉じているため、**Snowflake内部ステージ**が最小の追加インフラで実現できる現実的な選択肢として挙がった。まだ最終決定はしていない。

### 3. 本番flowのスコープ拡大(取り込み → dbt)

今後、本番で動かすflowにはdbtのモデル実行だけでなく、データ取り込み処理も含める想定。「取り込みタスク → `dbt build`」という順序のflowにし、今の`flows/dbt_build_flow.py`に取り込みタスクを追加する形で自然に拡張できる。

CI(Slim CI)側は取り込みは行わず、既存データに対してdbtの変更モデルだけを検証するため、本番flowとは別物として扱う。

### 4. CIはPrefectを経由しない

本番flow(取り込み→dbt)と、CI用の「変更モデルだけdbt実行」は、トリガー(スケジュール/手動 vs PR)・対象環境(PROD vs CI用スクラッチDB)・スコープ(取り込み込み vs dbtのみ)が異なるため、無理に1つのflowにまとめず別物として扱う方針とした。

さらに、CI自体もPrefectのデプロイメントとして用意するのではなく、**Prefectを経由せずGitHub Actions単体で完結させる**方針とした。

- CIは「PRごとに1回、短命に、GitHubのPRチェックとして結果を返す」用途であり、Prefectのスケジューリング・観測性(per-nodeタスク可視化など)は不要
- `prefect_dbt`(`PrefectDbtRunner`)も使わず、素の`dbt-core`+`dbt-snowflake`をGitHub Actionsのランナーに直接インストールしてCLIで実行する
- イメージ: GitHub Actionsのワークフロー内で「①stateとなる`manifest.json`を取得 → ② `uv run dbt build --select state:modified+ --defer --state ./state` を実行」という2ステップだけの薄いワークフローになる

## 未解決事項(実装時に詰める)

- `prod`データベース作成に伴うSnowflake側の設定(ロール・warehouse・スキーマ設計)
- 本番flowに追加する取り込みタスクの実装(取り込み元・処理内容は別途検討)
- `manifest.json`永続化先の最終決定(S3 / Snowflakeステージ / その他)
- CI用Snowflake認証情報の管理方法(このプロジェクトでは秘密鍵をPrefect SecretやGitHub Secretsとして安全に扱う運用が既に確立済み。[docs/prefect-cloud-deployment.md](./prefect-cloud-deployment.md)を参照)
