# Prefect Cloudへのdbt buildデプロイでハマった点

`flows/dbt_build_flow.py`(`PrefectDbtRunner`経由で`dbt build`を実行するflow)をPrefect Cloudにデプロイする際に発生した問題と対処のメモ。

## 1. Snowflakeの秘密鍵はローカルファイルパスを参照できない

`jaffle_shop/profiles.yml`の`dev`ターゲットは`private_key_path`でローカルの鍵ファイルを参照しているが、Prefect Cloudのマネージド実行環境(サーバーレス)には当然そのファイルは存在しない。

**対処**: `profiles.yml`に`cloud`ターゲットを追加し、鍵の中身を環境変数`SNOWFLAKE_PRIVATE_KEY`から読む(`private_key: "{{ env_var('SNOWFLAKE_PRIVATE_KEY') }}"`)。値自体はPrefectのSecret Blockとして登録し、`prefect.yaml`の`job_variables.env`から`{{ prefect.blocks.secret.<block-name> }}`で参照する。

## 2. `prefect-cloud deploy`は毎回「全体を上書き」する

`--secret`だけを追加したくて、以前指定した`--with dbt-snowflake --with prefect-dbt`を省略して再デプロイしたところ、依存パッケージのインストール指定が消えてしまい、実行時に`ModuleNotFoundError: No module named 'prefect_dbt'`となった。

`prefect-cloud deploy`はdiffを当てるのではなく、そのコマンドで指定した内容で設定を丸ごと置き換える。依存関係・パラメータ・secretなど、変更したくない設定も含めて毎回フルで指定する必要がある。

## 3. `dbt_packages/`はリポジトリに含まれない

`dbt_packages/`は(正しく)`.gitignore`されているため、Prefect Cloud側でリポジトリをcloneした直後にはパッケージが1つもインストールされていない状態になる。`dbt build`だけを実行すると以下のエラーになる。

```
Compilation Error
  dbt found 3 package(s) specified in packages.yml, but only 0 package(s) installed in dbt_packages. Run "dbt deps" to install package dependencies.
```

**対処**: flow内で`dbt build`の前に`dbt deps`を実行する。

```python
runner.invoke(["deps"])
runner.invoke(["build", "--target", target])
```

## 4. `prefect-cloud`CLIはFlow名をコードから読み取らない

`prefect-cloud deploy flows/dbt_build_flow.py:dbt_build_flow ...`のように関数名を指定すると、その**文字列そのもの**(`dbt_build_flow`)をPrefect Cloud上のFlow名として登録する。コードを実際にimportして中身を確認するわけではないため、`@flow(name="dbt-build")`のように明示的な名前を付けていても無視される。

一方、実際にflowが実行される際はPrefectのエンジンがコードをimportして`@flow(name=...)`を見るため、そちらの名前(`dbt-build`)で別のFlowが自動生成される。結果として:

- デプロイメントは`dbt_build_flow`という(コード上には存在しない)Flowに紐づく
- 実際の実行履歴は`dbt-build`という別のFlowに溜まる

という、デプロイメントと実行履歴が別のFlowに分裂する状態になった。

**対処**: `prefect-cloud` CLIをやめ、標準の`prefect deploy` + `prefect.yaml`に切り替えた。こちらはコードを実際にimportしてFlowオブジェクトを見るため、`@flow(name=...)`の値と完全に一致する。

## 5. `prefect-cloud` CLI vs 標準の`prefect deploy`、どちらを使うべきか

- **`prefect-cloud` CLI**(`uvx prefect-cloud deploy ...`): 公式のGitHub quickstart([docs.prefect.io/v3/get-started/github-quickstart](https://docs.prefect.io/v3/get-started/github-quickstart))で案内されている。単一スクリプトをコマンド1つでサクッとクラウドで動かす、オンボーディング向けの入り口。ワークプールも自動作成される。ただし上記2, 4のような簡略化による粗さがある。
- **標準の`prefect deploy`**(`prefect.yaml`ベース): 複数flowの管理、secret参照、CI連携など、ある程度育ったプロジェクト向けの標準的なやり方。コードの実態と設定が一致し、設定がYAMLとしてリポジトリにバージョン管理される。

このプロジェクトでは最終的に標準の`prefect deploy`に移行した。既存のマネージドワークプール(`default-work-pool`、`prefect:managed`タイプ)はどちらの方法からも利用できるので、ワークプールを作り直す必要はなかった。

## 現在の構成

- デプロイ定義: `prefect.yaml`
- 実行: `uv run prefect deployment run 'dbt-build/dbt-build'`(スケジュールなし、手動実行のみ)
- secret: `snowflake-private-key-dev` / `snowflake-private-key-prd`(Secret Block)→ それぞれ`SNOWFLAKE_PRIVATE_KEY_DEV_B64` / `SNOWFLAKE_PRIVATE_KEY_PRD_B64`環境変数として注入
- AWS(S3)認証: prd targetのAWS認証はIAM Userの長期アクセスキーを廃止し、AWS workload identity federation(OIDC)に移行済み。詳細は[docs/aws-auth-oidc-vs-access-key.md](./aws-auth-oidc-vs-access-key.md)(仕組みの解説)と、snowflakeリポジトリの`docs/prefect-aws-workload-identity.md`(Terraform側の実装)を参照

## AWS認証のOIDC移行(実施済み)

prd targetのPrefect flowが使うAWS認証を、IAM Userのアクセスキー(`aws-credentials-prd` Blockに保存)からOIDC経由の一時クレデンシャルに切り替えた。dev targetはローカル実行のみでPrefect managed work poolを使わないため対象外(IAM Userのアクセスキーのまま)。

**変更内容**:

1. `prefect.yaml`の`job_variables`に`federated_identity`を追加。Work Poolのbase job templateに元々`federated_identity`という設定項目(`aws_role_arn` / `aws_region_name` / `duration_seconds`)が用意されており、Prefect Managed work poolの標準機能として存在する。

   ```yaml
   job_variables:
     federated_identity:
       aws_role_arn: arn:aws:iam::730335183162:role/dbt-snowflake-artifacts-prefect-prd
       aws_region_name: ap-northeast-1
   ```

2. `aws-credentials-prd` Blockの`aws_access_key_id`/`aws_secret_access_key`を空にして再保存。この2つはpydanticモデル上Optionalで、未設定ならboto3の標準認証チェーン(環境変数優先)にフォールバックする。Prefect Managedが`federated_identity`設定に基づいて`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`を実行時に注入するため、コード変更は不要だった。
3. `s3-bucket-prd`/`s3-bucket-prd-cache` Blockは`credentials`フィールドが`aws-credentials-prd`をBlock参照(値のコピーではない)で持っているため、2の変更だけで両方に反映される。
4. `snowflake`リポジトリのTerraformで、GitHub Actions CI用と同型のOIDCプロバイダ(`https://api.prefect.cloud/oidc-provider`)+ IAM Roleを作成し、動作確認後に旧IAM User(`dbt-snowflake-artifacts-prod`)・アクセスキーを削除。

**動作確認**: `prefect deployment run 'dbt-build/dbt-build' --param target=prd`を旧IAM User削除の前後で1回ずつ実行し、いずれも`Completed`(manifest.jsonのS3アップロード・ノードキャッシュ読み書きを含め正常動作)。
