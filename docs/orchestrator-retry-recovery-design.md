# Airflow / Prefect / Dagster のリトライ・リカバリ設計思想比較

対象範囲: 「フローが一度最後まで走り切った後、失敗ノードを含めてどう復旧させるか」という部分リカバリの観点。実行中の自動retry（`retries`/`retry_delay`のようなタスクレベルの再試行設定）は3ツールとも同等の機能を持つため、ここでは比較対象から除く。

## 比較表

|         | 部分リカバリの提供形態                   | Run自体の扱い                                                | 結果データの永続化                                                                       |
| ------- | ---------------------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| Airflow | ネイティブ（Clear failed tasks）         | 同一DAG Run内で再実行                                        | XComとしてメタデータDBに直接保存（デフォルト）、カスタムXComバックエンドでS3等にも変更可 |
| Dagster | ネイティブ（Re-execute from failure）    | 元Runを参照した新Run（lineageあり）                          | IOマネージャ経由で外部ストレージ（S3/Snowflake等）に保存、DBには参照情報                 |
| Prefect | ネイティブ機能なし、キャッシュで代替実装 | 完全に新しいFlow Run（内部はキャッシュヒットで実質スキップ） | `persist_result=True`等を設定した場合のみResult Storageに保存、DBには参照ポインタのみ    |

## Airflow

- リトライの基本単位はタスク。DAG Runは`dag_run_id`単位でタスクごとの状態がメタデータDBに永続化されている。
- UI（Grid view）やCLI（`airflow tasks clear`）で**失敗したタスク（＋downstream）だけをClear**すれば、同じDAG Run内で該当タスクだけ再実行される。成功済みタスクのXComなどはそのまま残る。
- 部分リカバリが最もこなれた、成熟した機能として提供されている。

## Dagster

- op/asset単位で`RetryPolicy(max_retries=, delay=, backoff=, jitter=)`を宣言的に設定可能。
- **「Re-execute from failure」**がネイティブ機能として存在し、失敗したステップ（op/asset）だけを選んで再実行できる。その際、成功していた上流assetの出力はIOマネージャ経由で再利用される。
- 厳密には「同じRunを再開」ではなく、**元のRunを参照した新しいRun**が作られる（re-executed from run X、という形でlineageは追える）。挙動としてはAirflowのClearに近い体験。

## Prefect

- **完了済みのFlow Runを後から「部分再実行」するネイティブ機能はない**。Flow Runは実行し終わったら基本的にイミュータブル。
- Prefect Server/Cloudのバックエンドは、Postgres/SQLiteで動くリレーショナルDBを持ち、`flow_run` / `task_run` / `flow_run_state` / `task_run_state`のようなテーブルに各Task Runの状態・時刻・ログ・所属するFlow Run IDを記録している。これはAirflowのメタデータDBと役割としては同じ。
- 違いは以下の2点:
  1. **タスクの戻り値（実データ）はDBに入らない**。デフォルトでは結果は永続化されず、`persist_result=True`やキャッシュ設定を有効にしたときだけResult Storage（ローカルファイル/S3等）に保存され、DB側にはその参照ポインタだけが入る。
  2. **「このFlow Runの続きから」を扱うエンジン機能がない**。DBには「Flow Run Xの中でTask Aが失敗した」という情報自体はあるが、「Flow Run Xを指定して、失敗した部分だけ再実行する」というAPI/操作が存在しない。
- 代替として使われるのが記事（`article-prefect-dbt-orchestrator-slim-ci.md`）にある`PrefectDbtOrchestrator` + `CacheConfig`の仕組み。これは「Run IDに基づく再開」ではなく「キャッシュキー（入力が変わっていないか）に基づくスキップ」であり、仕組みとしては別物。新しいFlow Runを丸ごと起動し、たまたまキャッシュキーが一致したタスクの実行がスキップされる、という間接的な実現方法になる。

## まとめ

AirflowとDagsterは「失敗後のリカバリ」をオーケストレーター自体が一級市民の機能として持っているのに対し、Prefectはその機能がない代わりに、キャッシュ設計で自前に近い体験を作り込む必要がある。3ツールともタスク実行のメタデータ（状態・ログ・時刻）自体は保存しているが、そのメタデータを使って「特定のRunを選択的に再実行する」エンジン機能を持つかどうかが設計思想の分岐点になっている。

## 参考: GitHub Star数（スナップショット）

取得日: 2026-07-12 / 取得方法: `gh api repos/<owner>/<repo>` の `stargazers_count` / `created_at`

| ツール  | リポジトリ         | Stars  | リポジトリ作成日 |
| ------- | ------------------ | ------ | ---------------- |
| Airflow | apache/airflow     | 46,094 | 2015-04-13       |
| Prefect | PrefectHQ/prefect  | 22,973 | 2018-06-29       |
| Dagster | dagster-io/dagster | 15,816 | 2018-04-30       |

Airflowが最も多く、Prefect・Dagsterはほぼ同時期にスタートしたプロジェクトだが、現時点ではPrefectがDagsterよりやや多いスター数となっている。