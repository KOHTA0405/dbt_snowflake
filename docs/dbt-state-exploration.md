# dbt-state の調査メモ

[dbt-labs/dbt-state](https://github.com/dbt-labs/dbt-state)を実際に隔離環境で試した記録。**このリポジトリの本番構成には反映していない**(dbt-core 2.0はアルファ版のため)。

## これは何か

dbt Labsが提供する、既存の`state:modified` + `manifest.json`手動管理によるSlim CIを置き換える新機能。このプロジェクトが[docs/ci-cd-slim-ci-plan.md](./ci-cd-slim-ci-plan.md)・[docs/article-prefect-dbt-orchestrator-slim-ci.md](./article-prefect-dbt-orchestrator-slim-ci.md)で自前実装したS3ベースのmanifest.json永続化と同じ課題(変更モデルだけを実行したい)を解くが、アプローチが異なる。

## 対応dbt-coreバージョンの調査結果(重要)

`pip install dbt-state`するだけでは機能しない。実際にPyPI/GitHubリリースを確認したところ:

| dbt-coreバージョン | 状況 |
| --- | --- |
| 1.11.12(このプロジェクトの現行バージョン) | `dbt-state`パッケージを入れても`dbt login`コマンド・`--manage-state`フラグともに存在せず、フックする仕組みが無い。実質何も起きない |
| 1.12.0b2 | 一時的に`dbt-state`(`>=2.18,<3.0`)をバンドルし`dbt login`を追加。opt-in(`--manage-state`/`DBT_ENGINE_MANAGE_STATE`/`flags.manage_state`のいずれか) | 
| 1.12.0rc1 | **上記を全てロールバック**("Remove the experimental `dbt login` command and the bundled `dbt-state` plugin") |
| 1.12.0rc2(2026-07-09時点の最新rc) | ロールバックされたまま。関連機能は無し |
| 2.0.0a4(アルファ) | `dbt login`・`--manage-state`/`--no-manage-state`が実際に動作することを確認 |

**結論**: 現時点(2026-07-12)で試すには`dbt-core==2.0.0a4`一択。1.12系は正式リリースされても入っていない可能性が高い(rc1でのロールバックが伏線)。

### この結論に至る前段: メインプロジェクトの`.venv`(dbt-core 1.11.12)での確認コマンド

一旦`uv add dbt-state`でこのリポジトリ本体に追加して試したが、以下の通り何もフックされず、結局メインプロジェクトからは削除して隔離venvでの検証に切り替えた。

```bash
uv run dbt --version
uv run dbt -h
uv run dbt login
# Usage: dbt [OPTIONS] COMMAND [ARGS]...
# Error: No such command 'login'.

uv run dbt-state --help
# Commands:
#   explain  Show cache decision explanations from the most recent dbt run
# (login等は無く、explainのみ)

uv run dbt build -h | grep -i -A2 "manage-state"
# (出力なし。--manage-stateフラグ自体が存在しない)
```

## 実際に試した手順(隔離venv、dbt-core 2.0.0a4)

本体の`.venv`は汚さず、`/private/tmp`配下に使い捨てvenvを作って検証した。

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python "dbt-core==2.0.0a4" "dbt-snowflake" "dbt-state"
```

インストールされたバージョン: `dbt-core 2.0.0a4` / `dbt-adapters 1.24.4` / `dbt-snowflake 1.11.2` / `dbt-state 2.28.3`(後述の通りこの外部パッケージはほぼ使われない)。

### 1. ログイン

```bash
.venv/bin/dbt login
```

ブラウザが開き、standaloneアカウント(https://app.state.dbt.com、事前に作成済み)でOAuth認証。成功後は`dbt_cloud.yml`にトークンが保存される。

```
.venv/bin/dbt login status
# Status: authenticated (via dbt_cloud.yml)
#   type:          personal access token
#   account host:  ng571.us1.dbt.com
```

### 2. ビルド(1回目・キャッシュなし)

`.envrc`未読み込みの状態でまず実行し、Snowflakeの秘密鍵の環境変数が無くて一度失敗した。

```bash
.venv/bin/dbt build --project-dir jaffle_shop --profiles-dir jaffle_shop --target dev --manage-state
# [error] [InvalidConfig (dbt1005)]: Jinja render error: invalid operation: 'env_var':
#   environment variable 'SNOWFLAKE_PRIVATE_KEY_DEV' not found
```

`.envrc`を読み込んでから同じコマンドを再実行し、成功。

```bash
set -a && source .envrc && set +a
.venv/bin/dbt build --project-dir jaffle_shop --profiles-dir jaffle_shop --target dev --manage-state
```

全ノードが通常通りビルドされる。`[warning] StateServiceWarn (dbt1410): ... not in prefetch cache`という警告が全ノードに出るが、これは「まだ判定材料が無い(初回)」という意味で、エラーではない。

### 3. ビルド(2回目・同一コマンドをそのまま再実行)

```bash
.venv/bin/dbt build --project-dir jaffle_shop --profiles-dir jaffle_shop --target dev --manage-state
```

結果:

```
Summary: 62 total | 6 success | 50 reused | 6 warn
```

- seed 6個(`raw_items`, `raw_orders`など)は毎回`Succeeded`(再ロードされる。キャッシュ対象外の模様)
- モデル・テスト50個は`Reused`(スキップ)

ログの例:

```
Reused [-------] model DBT_SNOWFLAKE_MONITORING.query_history_enriched (incremental - No new changes on any upstreams)
Reused [-------] model SILVER.fct_orders (table - New changes detected. Did not meet lag_tolerance of 45m. Last updated 0s ago)
```

2行目が重要で、「上流(seed)のデータは実際に0秒前に更新されたが、デフォルトの`lag_tolerance`(45分)以内だったので再ビルドしなかった」という判定。既存実装のノードキャッシュ(`CacheConfig`、[docs/ci-cd-slim-ci-plan.md](./ci-cd-slim-ci-plan.md#6-ノードキャッシュの仕様実装済み))は「コードの中身が同じか」しか見ないが、dbt-stateは「データがいつ更新されたか」まで見て閾値で判断している。

### 4. `--debug`で判定の中身を確認

```bash
.venv/bin/dbt --debug build --project-dir jaffle_shop --profiles-dir jaffle_shop --target dev --manage-state --select dim_customers
```

Snowflakeに対して実際に発行されているクエリが2種類確認できた。

1. **ロジック変更の検知**: `EXECUTE IMMEDIATE`ブロック内で`GET_DDL('VIEW', obj_name)`を呼び出し、上流オブジェクト(例: `STG_CUSTOMERS`)の定義DDLを取得
2. **鮮度の検知**: `INFORMATION_SCHEMA.TABLES`から`last_altered`タイムスタンプを取得

```sql
SELECT table_schema, table_name, last_altered,
       (table_type = 'VIEW' OR table_type = 'MATERIALIZED VIEW') AS is_view
FROM "DEV".INFORMATION_SCHEMA.TABLES
WHERE table_schema = 'SILVER' and table_name = 'DIM_CUSTOMERS'
```

これがドキュメント上の「SQL statement hashes」「Last-modified timestamps」の実体で、Snowflake側への追加クエリ発行というコストを払って判定していることがわかる。

### 5. 診断コマンド`dbt-state explain`は動かなかった

pip版`dbt-state`パッケージが提供する`dbt-state explain <model>`を試したところ、`dbt-core 2.0`側の内部API変更で壊れていた。

```bash
.venv/bin/dbt-state explain --help
```

```
Traceback (most recent call last):
  ...
  File ".../dbt_state/plugin.py", line 13, in <module>
    from dbt.flags import get_flags
ModuleNotFoundError: No module named 'dbt.flags'
```

判定理由がdbt本体側にもあるか確認するため、以下も実行して確認した(該当なし)。

```bash
.venv/bin/dbt -h | grep -i -E "explain|state|login"
# login          Authenticate with dbt platform
# (explain/stateサブコマンドはdbt本体には無い)
```

```
ModuleNotFoundError: No module named 'dbt.flags'
```

`pip show dbt-core`で確認すると、`dbt-state`(外部パッケージ)は`dbt-core`に依存する側であり、**`dbt-core 2.0`自体が状態判定ロジックを内蔵しており、外部の`dbt-state`パッケージはほぼ使われていない**。1.12ベータ時代の「dbt-coreがdbt-stateプラグインを取り込む」設計から、2.0では「dbt-core本体に統合」という設計に変わったと見られる。ただし判定理由自体はビルドログや`--debug`出力に既にインラインで出ているため、実用上explainコマンドが無くても大きな支障はなかった。

## 既存実装との違い(まとめ)

| 項目 | 今回自前実装したSlim CI | dbt-state |
| --- | --- | --- |
| 変更検出 | manifest.jsonのファイル差分(`state:modified`) | `GET_DDL`によるSQL定義の意味的比較 |
| 上流データの変化 | 検知しない(コードの中身だけを見る) | `INFORMATION_SCHEMA.TABLES.last_altered`のタイムスタンプ + `lag_tolerance`で判定(実際に動作確認済み) |
| stateの永続化 | 自前でS3にmanifest.jsonをPUT/GET | dbt Labs側のサーバーが管理。メタデータのみ送信、実データは送らない |
| セットアップ | manifest.jsonの配置、`--defer --state`の指定が必要 | `dbt login`後、`--manage-state`を付けるだけ |
| 認証 | 不要(自前のAWS認証のみ) | dbt Platformアカウント、またはスタンドアロンアカウント(app.state.dbt.com)へのログインが必要 |
| 前提dbt-coreバージョン | 1.10〜1.11で動作中 | 実質2.0系アルファのみ(1.12は機能ロールバック済み) |

## 注意点・未決定事項

- Snowflakeへの追加クエリ(`GET_DDL`・`INFORMATION_SCHEMA`)が判定のたびにノード数分発行される。ノード数が多いプロジェクトではウェアハウスの負荷・レイテンシが無視できない可能性がある
- SQL定義・更新タイムスタンプ等のメタデータがdbt LabsのUSリージョンサーバーに送信される。実データは送らないとされているが、新たな外部サービス依存が発生する
- 無料枠は「dbt Platform 30日トライアル」または「スタンドアロンアカウント(料金体系は非公開)」のみで、恒久無料の枠は今のところ確認できていない
- 本体のdbt-coreをアルファ版2.0系に上げる必要があり、正式リリース(1.12系またはそれ以降)でこの機能が復活するかどうかは不透明(1.12のrc1でロールバックされた事実がある)
- 既存のS3ベースSlim CI実装(GitHub Actions側)を置き換えるのか、併存させるのかは未検討
- `select *`を含むビューは常に再ビルド対象になる(上流スキーマをクエリなしで確認できないため)。今回の検証では未確認
