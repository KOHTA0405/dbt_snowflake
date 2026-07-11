---
title: "Snowflake + Prefect + dbtでモデル単位の可視化・キャッシュ・Slim CIを組んだ話"
emoji: "🧊"
type: "tech"
topics: ["dbt", "prefect", "snowflake", "aws", "githubactions"]
published: false
---

## はじめに

個人のSnowflake検証プロジェクト([jaffle_shop](https://github.com/dbt-labs/jaffle-shop)ベース)で、Prefect Cloud経由でdbtを実行するパイプラインを構築しました。本記事では、その中でも特に工夫した3点について書きます。

1. `PrefectDbtOrchestrator`によるモデル単位の実行状況の可視化
2. S3をバックエンドにしたノード単位キャッシュによる、変更の無いモデルの実行スキップ
3. `manifest.json`をS3に永続化し、CIでは変更モデルだけを実行するSlim CI

## 全体構成

```mermaid
flowchart TB
    subgraph prefect["本番Prefect flow"]
        A1["Prefect Cloud managed実行"]
        A2["PrefectDbtOrchestrator (PER_NODE)<br/>各モデルをPrefect Taskとして実行"]
        A3["Snowflake (PRD) へbuild"]
        A4["build成功後:<br/>manifest.json PUT / ノードキャッシュ R/W"]
        A1 --> A2 --> A3 --> A4
    end

    subgraph ci["CI (GitHub Actions)"]
        B1["PRごとに起動 (pull_request)"]
        B2["manifest.jsonをS3からダウンロード"]
        B3["dbt-core単体(Prefectを経由しない)<br/>dbt build --select state:modified+<br/>--defer --state ./state"]
        B4["Snowflake (PRD) を参照<br/>(--defer, read-only)"]
        B1 --> B2 --> B3 --> B4
    end

    S3[("S3: xxx-dbt-snowflake-artifacts<br/>prod/manifest/manifest.json<br/>prod/cache/...")]
    SF[("Snowflake PRD database")]

    A4 -->|PUT| S3
    S3 -->|GET| B2
    A3 -.->|CREATE/INSERT| SF
    B4 -.->|参照(read-only)| SF
```

dbt自体はSnowflake上のjaffle_shopデータセットをDEV/PRDの2データベースに分けてビルドしており、本番用のPrefect flowはPRDに対して実行、ローカル開発はDEVに対して実行します。

## 1. PrefectDbtOrchestratorでモデル単位の可視化

最初は`prefect-dbt`パッケージの`PrefectDbtRunner`を使っていました。これはdbt-coreの`dbtRunner`をそのまま1プロセスで呼び出す薄いラッパーで、dbt内部のイベントストリームにフックしてPrefectのTaskを作るため、UIから今どのモデルが動いているかは見えます。しかし実行のスケジューリング自体はdbtに委ねたままで、Prefect側からの制御(モデル単位のリトライなど)はできませんでした。

そこで、同パッケージの`PrefectDbtOrchestrator`(2026年7月現在ベータ版の`prefect_dbt.core._orchestrator`)に切り替えました。こちらは`manifest.json`を自前でパースしてノードの実行順序(wave)を計算し、`PER_NODE`モードでは各ノードを本当に独立したPrefect Task/プロセスとして実行します。

```python
from prefect_dbt.core._orchestrator import ExecutionMode, PrefectDbtOrchestrator

orchestrator = PrefectDbtOrchestrator(
    settings=PrefectDbtSettings(
        project_dir=DBT_PROJECT_DIR,
        profiles_dir=DBT_PROJECT_DIR,
    ),
    execution_mode=ExecutionMode.PER_NODE,
)
orchestrator.run_build(target=target)
```

`PER_NODE`にすることで、Prefect UI上でモデルごとに独立したTask run(成功/失敗/実行時間)が確認できるようになり、後述のノード単位キャッシュも有効化できるようになりました。ベータ版なので、今後のアップデートでAPIが変わる可能性がある点は注意が必要です。

## 2. S3バックエンドのノード単位キャッシュ

`PrefectDbtOrchestrator`は`cache=CacheConfig(...)`を渡すことで、内容が変わっていないノードの実行をスキップできます。これを使うと、一部のモデルだけ失敗したときに**同じflowをそのまま再実行するだけで、成功済みノードはスキップされ失敗したノード以降だけが再実行される**という、UIからの「特定モデルだけretry」に近い体験が実現できます。

```python
from datetime import timedelta
from prefect_aws import S3Bucket
from prefect_dbt.core._orchestrator import CacheConfig

cache = CacheConfig(
    result_storage=S3Bucket.load("s3-bucket-prd-cache"),
    expiration=timedelta(hours=12),
)
```

### なぜS3か

キャッシュの保存先としてSnowflakeの内部ステージも検討しましたが、ノードキャッシュは「flow実行のたびにノードごとに1回ずつ有無をチェックする」高頻度・多数の小さいオブジェクトへのアクセスになります。Snowflakeセッション確立のオーバーヘッドが、S3の軽量なHTTP GETに比べて大きく、ノード数が多いほど遅延が積み重なりやすいため、Prefect標準の`S3Bucket`Blockがそのまま使えるS3に一本化しました。

### キャッシュキーは「コードの中身」だけで決まる

キャッシュキーは以下だけのハッシュから計算されます(`prefect_dbt.core._cache.DbtNodeCachePolicy.compute_key`)。

- モデルSQL/seed CSVファイルの中身
- モデルのconfig設定
- `--full-refresh`フラグの有無
- 上流ノードのキャッシュキー(上流が変われば連鎖して変わる)
- 依存マクロファイルの中身
- 出力先のテーブル/ビュー名

つまり**`source()`で参照する外部テーブルの行データが変わったかどうかは一切見ていません**。本番flowを日次スケジュール実行する予定だったため、これは重要な落とし穴でした。モデルのコードが変わっていなければ、元データが更新されていてもキャッシュがヒットしてしまい、テーブルが更新されなくなってしまうのです。

対策として、`expiration`をS3のライフサイクルルール(オブジェクトの自動削除、30日)より短く、日次実行の間隔(24時間)より確実に短い**12時間**に設定しました。これにより「同日中の再実行はキャッシュで高速化されるが、翌日の定期実行では必ずキャッシュが期限切れになりフルで再構築される」という動きを両立させています。

またデフォルトでは`test`・`snapshot`・`incremental`モデルはキャッシュ対象外です。特にtestはモデルのSQLが同じでも元データが変われば結果(pass/fail)が変わりうるデータ品質チェックなので、キャッシュでスキップすると壊れたデータに対して古い「pass」を信じてしまうリスクがあり、デフォルトのまま運用しています。

## 3. manifest.jsonをS3に置いてSlim CIを実現する

dbtの`state:modified`選択子と`--defer --state`フラグを使って変更モデル(とその下流)だけをビルド・テストする、いわゆる「Slim CI」自体はdbtユーザーにはお馴染みの手法だと思うので、詳細は公式ドキュメントに譲ります([Continuous integration in dbt](https://docs.getdbt.com/docs/deploy/continuous-integration) / [state node selector](https://docs.getdbt.com/reference/node-selection/methods#state))。

これを機能させるには、比較基準となる`manifest.json`をCIから参照できる場所に永続化しておく必要があります。Prefect Cloudのmanaged実行は使い捨てコンテナなので、明示的に永続化しないと実行終了と同時に消えてしまいます。ここが今回工夫した部分です。

### 本番flow側: build成功時にS3へPUT

```python
@task
def upload_manifest_to_s3():
    manifest_path = DBT_PROJECT_DIR / "target" / "manifest.json"
    s3_bucket = S3Bucket.load("s3-bucket-prd")
    s3_bucket.upload_from_path(manifest_path, "manifest/manifest.json")
```

`run_build()`は失敗時に例外を送出するため、このタスクは自然と「build全体が成功した時だけ」実行され、常に固定パスへ上書きすることで「最新の正しい状態」だけを保持します。

### CI側: GitHub Actions単体で完結させる

CI用のdeploymentをPrefect側に作ることも考えましたが、CIは「PRごとに1回、短命に、PRチェックとして結果を返す」用途であり、Prefectのスケジューリングや観測性(per-nodeタスク可視化)は不要です。そのため**Prefectを経由せず、dbt-core単体だけで**動かしています。

```yaml
name: Slim CI

on:
  pull_request:
    paths:
      - "jaffle_shop/**"

permissions:
  id-token: write
  contents: read

jobs:
  slim-ci:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: jaffle_shop
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<account-id>:role/dbt-snowflake-artifacts-ci
          aws-region: ap-northeast-1

      - name: Download production manifest.json
        run: |
          mkdir -p state
          aws s3 cp s3://<bucket>/prod/manifest/manifest.json state/manifest.json

      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh

      - name: Install dependencies
        run: |
          uv sync --project ..
          uv run --project .. dbt deps

      - name: Run Slim CI build (changed models only)
        env:
          SNOWFLAKE_PRIVATE_KEY_B64: ${{ secrets.SNOWFLAKE_PRIVATE_KEY_B64 }}
        run: |
          export SNOWFLAKE_PRIVATE_KEY_DEV=$(echo "$SNOWFLAKE_PRIVATE_KEY_B64" | base64 --decode)
          uv run --project .. dbt build --target dev --select state:modified+ --defer --state ./state
```

CIのAWS認証は長期アクセスキーを使わず、GitHub ActionsのOIDCフェデレーションでIAM Roleを一時的にAssumeする方式にしました。IAM Role側は`prod/manifest/*`のGet専用に権限を絞り込み、書き込み権限は一切持たせていません。

ちなみにdbt Labsからは、このmanifest.jsonの手動管理を丸ごと不要にする`dbt-state`という新機能も出てきています。試してみたところ現状はdbt-core 2.0のアルファ版でしか動かず、まだ本番投入できる段階ではなさそうでしたが、早く安定版で使えるようになってほしいところです🙏

## ハマったポイント: dbtバージョンのズレでmanifest.jsonが読めなくなった

実装後、実際にPRを作ってSlim CIを検証したところ、`dbt build`が以下のエラーで失敗しました。

```
mashumaro.exceptions.InvalidFieldValue: Field "supported_languages" of type
Optional[List[ModelLanguage]] in Macro has invalid value ['sql', 'python', 'javascript']
```

原因は、S3に置かれていた`manifest.json`が`dbt_version: 1.11.12`で生成されていたのに対し、CI側は`uv.lock`で`dbt-core==1.10.15`に固定されていたことでした。dbt-core 1.11で組み込みマクロの`supported_languages`に`'javascript'`が追加されましたが、1.10系の`ModelLanguage`Enumにはまだ存在せず、古いバージョンで新しいmanifestを読もうとして型検証エラーになっていたのです。

なぜバージョンがズレたのかというと、Prefect Cloudのmanaged実行環境(`pip_packages`)でdbt-snowflakeのバージョンを固定していなかったためでした。`pip_packages`はバージョン指定なしだと毎回pipが最新版を解決するため、`uv.lock`で固定しているローカル/CIの環境と静かにズレてしまいます。

```yaml
# prefect.yaml
job_variables:
  pip_packages:
    - dbt-snowflake==1.11.6
    - dbt-core==1.11.12
    - prefect-dbt==0.7.25
    - prefect-aws==0.7.9
```

`uv.lock`側もdbt-core 1.11系にアップグレードして揃え、`pip_packages`にも明示的にバージョンを固定することで解決しました。「manifest.jsonを生成する環境」と「それを読む環境」でdbtのバージョンを厳密に揃えておく必要がある、という教訓です。

:::message
より根本的には`uv.lock`固定の依存関係をカスタムイメージに焼き込む方法もありますが、今回使っているWork Pool type(実行基盤の種類)である`prefect:managed`はカスタムイメージ非対応で、対応するself-hosted型のWork Poolは無料プランでは使えませんでした。そのため今回は`pip_packages`のバージョン固定で対処しています。
:::

## まとめ

- `PrefectDbtOrchestrator`(PER_NODEモード)により、dbtのモデル単位の実行状況がPrefect UIから見えるようになった
- ノード単位キャッシュ(S3バックエンド)により、変更の無いモデルの再実行をスキップできるが、キャッシュキーはコードの中身のみを見ており実データの変化は検知しないため、有効期限の設計が重要
- `manifest.json`をS3に永続化し、CIはPrefectを経由せずdbt-core単体で`state:modified+`によるSlim CIを実現
- 生成環境と参照環境でdbtのバージョンを揃えておかないと、`manifest.json`の互換性が壊れる
