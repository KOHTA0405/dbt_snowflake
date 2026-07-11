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

一般的にはS3などのクラウドストレージが定石だが、このプロジェクトはAWS/GCPのアカウントを持たずSnowflakeに閉じているため、**Snowflake内部ステージ**が最小の追加インフラで実現できる現実的な選択肢として挙がった。

その後、[5. ノード単位キャッシュの永続化先](#5-ノード単位キャッシュ選択的リトライの永続化先)の検討を経て、**S3の方が理にかなっている**という結論に傾いている(詳細は当該セクション参照)。まだ最終決定はしていない。

#### Snowflake内部ステージ案の実装イメージ

採用する場合、変更点は大きく3つ。

1. **Snowflake側のセットアップ(一回限り)**: ステージを1つ作成する

   ```sql
   CREATE STAGE IF NOT EXISTS dbt_artifacts_stage;
   ```

2. **本番flowにアップロードタスクを追加**: `dbt build`成功後、`snowflake-connector-python`で直接接続して`manifest.json`を`PUT`する。`prod`ターゲットでビルドが成功した時だけ実行し、常に固定パス(例: `latest/manifest.json`)に上書きしていく(バージョン管理はせず「最新の正しい状態」だけを持つ)

   ```python
   import snowflake.connector

   @task
   def upload_manifest_to_stage():
       conn = snowflake.connector.connect(...)  # 既存の秘密鍵で接続
       cur = conn.cursor()
       cur.execute(
           f"PUT file://{DBT_PROJECT_DIR}/target/manifest.json "
           "@dbt_artifacts_stage/latest OVERWRITE=TRUE AUTO_COMPRESS=FALSE"
       )
       conn.close()
   ```

3. **CI(GitHub Actions)にダウンロードステップを追加**: `dbt build`実行前に、同じステージから`GET`で取得する(読み取り専用のCI用Snowflake認証情報が別途必要)

   ```bash
   snowsql -q "GET @dbt_artifacts_stage/latest/manifest.json file://./state/"
   ```

まとめると「本番flowに1タスク追加」+「Snowflakeに1ステージ作成」+「CIワークフローに1ステップ追加」の3点セット。

### 3. 本番flowのスコープ拡大(取り込み → dbt)

今後、本番で動かすflowにはdbtのモデル実行だけでなく、データ取り込み処理も含める想定。「取り込みタスク → `dbt build`」という順序のflowにし、今の`flows/dbt_build_flow.py`に取り込みタスクを追加する形で自然に拡張できる。

CI(Slim CI)側は取り込みは行わず、既存データに対してdbtの変更モデルだけを検証するため、本番flowとは別物として扱う。

### 4. CIはPrefectを経由しない

本番flow(取り込み→dbt)と、CI用の「変更モデルだけdbt実行」は、トリガー(スケジュール/手動 vs PR)・対象環境(PROD vs CI用スクラッチDB)・スコープ(取り込み込み vs dbtのみ)が異なるため、無理に1つのflowにまとめず別物として扱う方針とした。

さらに、CI自体もPrefectのデプロイメントとして用意するのではなく、**Prefectを経由せずGitHub Actions単体で完結させる**方針とした。

- CIは「PRごとに1回、短命に、GitHubのPRチェックとして結果を返す」用途であり、Prefectのスケジューリング・観測性(per-nodeタスク可視化など)は不要
- `prefect_dbt`(`PrefectDbtRunner`)も使わず、素の`dbt-core`+`dbt-snowflake`をGitHub Actionsのランナーに直接インストールしてCLIで実行する
- イメージ: GitHub Actionsのワークフロー内で「①stateとなる`manifest.json`を取得 → ② `uv run dbt build --select state:modified+ --defer --state ./state` を実行」という2ステップだけの薄いワークフローになる

### 5. ノード単位キャッシュ(選択的リトライ)の永続化先

`flows/dbt_build_flow.py`は`PrefectDbtOrchestrator`(PER_NODEモード)に切り替え済み。このモードは`cache=CacheConfig(...)`を渡すことで、内容が変わっていないノードの結果をキャッシュしてスキップできる。これを使うと、一部のモデルだけが失敗したときに**同じflowをそのまま再実行するだけで、成功済みノードはスキップされ失敗したノード以降だけが再実行される**(UIからの「特定モデルだけretry」に近い体験になる)。

キャッシュの保存先は`key_storage`(Prefectの`WritableFileSystem`Block)で指定する。ここでもmanifest.jsonと同様「Snowflakeステージを使えないか」を検討したが、以下の理由で不向きと判断した。

- Prefect/prefect-dbtにSnowflakeステージ用の`WritableFileSystem`実装は存在せず、`snowflake-connector-python`の`PUT`/`GET`をラップした独自Blockを自作する必要がある
- manifest.jsonは「本番buildが成功するたびに1ファイルをPUTするだけ」の低頻度アクセスだったのに対し、ノードキャッシュは「flow実行のたびに、ノードごとに1回ずつ有無をチェックする」高頻度・多数の小さいオブジェクトへのアクセスになる。SnowflakeセッションのオーバーヘッドがS3のような軽量なオブジェクトストレージより大きく、ノード数が増えるほど遅延が積み重なりやすい

**結論**: manifest.jsonの永続化に加えてノードキャッシュの永続化も必要になったことを踏まえると、2つの用途のために別々の仕組み(Snowflakeステージ用の独自Block + 何らかの案)を保守するより、**両方をS3(または同等のオブジェクトストレージ)に一本化する**方が理にかなっている。Prefect標準の`S3Bucket`/`RemoteFileSystem`Blockがそのまま使えるため、独自実装も不要になる。トレードオフはAWSアカウント・認証情報という新しい運用対象が増えることだが、用途が1つから2つに増えたことでそのコストを払う価値が出てきた、というのが現時点の判断。

### 6. ノードキャッシュの仕様(実装済み)

`flows/dbt_build_flow.py`で`cache=CacheConfig(result_storage=S3Bucket.load("s3-bucket-prd-cache"), expiration=timedelta(hours=12))`として実装済み(`prd`/`cloud_prd`ターゲットのみ有効、devでは無効)。動作を確認して分かった仕様を残す。

**キャッシュキーは「コードの中身」だけで決まり、実データの変化は見ていない**

キャッシュキーは以下のみのハッシュから計算される(`prefect_dbt.core._cache.DbtNodeCachePolicy.compute_key`)。

- モデルSQL/seed CSVファイルの中身
- モデルのconfig設定
- `--full-refresh`フラグの有無
- 上流ノードのキャッシュキー(上流が変われば連鎖して変わる)
- 依存マクロファイルの中身
- 出力先のテーブル/ビュー名

`source()`で参照する外部テーブルの行データが変わったかどうかは一切見ていない。そのため「モデルのコードは変わっていないが元データは変わった」場合でもキャッシュはヒットしてしまう。これが本番flowを日次スケジュール実行する予定にもかかわらず`expiration`を(S3のライフサイクルルールの30日ではなく)12時間という短い値にした理由で、日次実行の間隔(24時間)より確実に短くすることで、スケジュール実行のたびに必ずキャッシュが期限切れになり、フルで再構築されることを保証している。

**キャッシュはRun・Retryを問わず同じロジックで効く**

Prefect UIで「Run」(新規実行)しても「Retry」(失敗したflow runの再実行)しても、キャッシュの判定ロジックは同じ。「特定のflow run由来かどうか」は見ておらず、単に「直近`expiration`以内に、同じ内容のノードが成功済みか」だけを見ている。なので同日中に新しく手動実行し直しても、前回成功した範囲はスキップされる。

**デフォルトでキャッシュ対象外のもの**

`CacheConfig`のデフォルトでは以下が除外される(`exclude_materializations`/`exclude_resource_types`)。

- `incremental`マテリアライゼーションのモデル: 実行のたびに新しい行を積み増す性質上、コードが同じでも毎回結果が変わるため
- `test`・`snapshot`・unit test: 特にtestはモデルのSQLが同じでも、元データが変われば結果(pass/fail)が変わりうるデータ品質チェックのため、キャッシュでスキップさせるとデータが壊れていても古い「pass」を信じてしまうリスクがある

`exclude_resource_types`を明示的に上書きすればtestもキャッシュ対象に含めることは技術的に可能だが、上記の理由から非推奨と判断し、デフォルトのまま運用している。

## 未解決事項(実装時に詰める)

- `prod`データベース作成に伴うSnowflake側の設定(ロール・warehouse・スキーマ設計)
- 本番flowに追加する取り込みタスクの実装(取り込み元・処理内容は別途検討)
- `manifest.json`・ノードキャッシュの永続化先の最終決定(現時点ではS3が有力候補)
- ノードキャッシュ(`CacheConfig`)を実際に有効化するかどうか、有効化する場合の`retries`等のパラメータ設計
- CI用Snowflake認証情報の管理方法(このプロジェクトでは秘密鍵をPrefect SecretやGitHub Secretsとして安全に扱う運用が既に確立済み。[docs/prefect-cloud-deployment.md](./prefect-cloud-deployment.md)を参照)
