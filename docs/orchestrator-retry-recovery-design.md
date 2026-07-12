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

### 補足: Prefect 1.0時代との違い

Prefectは元々ネイティブなresume機能を持たなかったわけではない。Prefect 1.0（Prefect Core）には**checkpointing**という仕組みがあり、Prefect Server/Cloudと連携していればタスク成功時にその戻り値が`Result`オブジェクト経由で永続化されていた。Flow Runをrestartすると、成功済みタスクはその永続化結果を使ってスキップされ、失敗したタスク（とその下流）だけが再実行される、という**Run履歴に基づく位置ベースの再開**が標準機能として提供されていた（[Results | Prefect Docs (v1/core)](https://docs.prefect.io/core/concepts/results.html)）。

実際に[Issue #10749](https://github.com/PrefectHQ/prefect/issues/10749)では、「Prefect 1では、UIで失敗タスクの状態を手動でCompletedに書き換えてFlowをrestartすると次のタスクから実行が続いたが、Prefect 2では同じ操作をしても書き換えたタスクごと再実行されてしまう」という挙動差が報告されている。つまり今の「キャッシュで代替する」設計は、Prefect 1.0からの後退（少なくとも仕組みの変更）であり、最初からそうだったわけではない。

**なぜ変わったのか（推論、Prefect側の明言は未確認）**: Prefect 2.0（開発コード名Orion）最大の変更点は、「Flowは事前に静的なDAGとして定義しなければならない」という制約を撤廃し、素のPythonの制御構文（if/for/while、動的なタスク生成）をそのまま使えるようにしたこと（[Our Second-Generation Workflow Engine](https://www.prefect.io/blog/second-generation-workflow-engine)）。これは「negative engineering（オーケストレーターの都合に合わせて書かされる作業）を減らす」という製品哲学に基づく。

Prefect 1.0やAirflowでは、Flow/DAGは実行前に構造が確定した静的なグラフだったため、「このRunのタスクAはグラフ上のこの位置にある」という**位置ベースの同一性**が保証でき、restart時に「前回Runで、この位置のタスクは成功していたか」を素直に参照できた。Prefect 2.0以降はFlow自体が実行時に評価されるPythonコードそのものになり、タスクグラフの形が実行ごとに変わりうる（条件分岐やループでタスク数・構造が変わる）。この前提では位置ベースの再開は安全性を失うため、同一性の判定基準を「位置」から「内容（入力・コードが同じか＝キャッシュキー）」に切り替える必要があった、というのが最も筋の通る説明になる（[The Importance of Idempotent Data Pipelines for Resilience](https://www.prefect.io/blog/the-importance-of-idempotent-data-pipelines-for-resilience)）。

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