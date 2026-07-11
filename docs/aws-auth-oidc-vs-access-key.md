# AWS認証: アクセスキー方式 vs OIDC方式

このプロジェクトでは今、2つの異なるAWS認証方式が混在している。

- **GitHub Actions(Slim CI)**: OIDCで一時クレデンシャルを取得([.github/workflows/slim-ci.yml](../.github/workflows/slim-ci.yml))
- **Prefect Cloud(本番flow)**: `AwsCredentials` Blockに保存した長期のIAMユーザーアクセスキー

この違いと、そもそもOIDCが何をしているのかを整理する。

## 1. アクセスキー方式: 何が起きているか

IAMユーザーを作り、そのユーザーの`Access Key ID` + `Secret Access Key`を発行する。この2つの文字列があれば、それを知っている人・システムは誰でも(有効期限が来るまでずっと)そのIAMユーザーとして振る舞える。

```
IAMユーザー "prefect-prd" を作成
  → Access Key ID: AKIAxxxxxxxx
  → Secret Access Key: xxxxxxxxxxxxxxxxxxxx（これを知っていれば誰でもなりすませる）
```

このプロジェクトでは、この2つの値をPrefect Cloudの`AwsCredentials` Blockに**保存**している。Prefect Cloud側のデータベースのどこかに、半永久的に有効な文字列として存在し続けることになる。

**弱点**:
- 値そのものが漏れたら最後、有効期限が切れるかローテーションするまで誰でも使える(「知っていること」だけが認証の根拠なので、漏洩と正規利用の区別がつかない)
- ローテーションは手動(または別途の仕組み)が必要
- 「このアクセスキーは今どこに何個コピーされているか」を追跡するのが難しい

## 2. OIDCとは何か(そもそも論)

OIDC(OpenID Connect)は、大元は「Aというサービスにログインしている事実を、Bというサービスに証明する」ための仕組み。「GoogleアカウントでXにログイン」のような、いわゆるソーシャルログインの裏側で使われている技術がこれ。

仕組みの核はJWT(JSON Web Token)という、**発行者が電子署名した「身分証明書」**のようなデータ構造。

```
JWT = ヘッダー + 中身(claims) + 署名
```

中身(claims)には「誰が」「いつ」「何の目的で」発行したトークンかという情報が入っている。例えばGitHub ActionsのOIDCトークンなら:

```json
{
  "iss": "https://token.actions.githubusercontent.com",
  "sub": "repo:KOHTA0405/dbt_snowflake:ref:refs/heads/main",
  "aud": "sts.amazonaws.com",
  "exp": 1752... // 数分〜数十分後に失効
}
```

- `iss`(issuer): 「誰がこの身分証を発行したか」= GitHubのOIDC発行サーバー
- `sub`(subject): 「誰の身分証か」= このリポジトリのmainブランチで動いているワークフロー
- `exp`(expiration): 「いつまで有効か」= 短時間(分単位)

このJWTの署名は、発行者(GitHub)が持つ**秘密鍵**で作られており、発行者が公開している**公開鍵**(JWKS: JSON Web Key Set、`https://token.actions.githubusercontent.com/.well-known/jwks`のようなURLで誰でも取得できる)を使えば、誰でも「このJWTは本当にGitHubが発行したものか」「改ざんされていないか」を検証できる。

**重要なポイント**: JWT自体には秘密の値が一切含まれていない。「GitHubの秘密鍵で署名された」という事実だけが信頼の根拠であり、JWTの中身(claims)は誰が見てもいい。漏れても、有効期限が数分〜数十分で切れるので実害が小さい。

## 3. AWSがOIDCをどう信頼に変換しているか

AWS IAMには「OIDCプロバイダを信頼先として登録する」機能がある。手順はこうなる。

### 3-1. AWS側にOIDCプロバイダを登録する

「このissuer(発行者)が発行したJWTなら検証してよい」とAWSに教える。

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
```

これで、AWSは「GitHubのJWKSから公開鍵を取ってきて、GitHub発行のJWTの署名を検証できる」状態になる。

### 3-2. IAMロールの信頼ポリシーで「誰の身分証まで許可するか」を絞る

OIDCプロバイダを信頼するだけだと「GitHubで発行された全世界のJWTを信頼する」ことになってしまう。それでは他人のリポジトリのワークフローにもロールを乗っ取られてしまうので、`sub`(誰の身分証か)の値で絞り込む。

```json
{
  "Effect": "Allow",
  "Principal": { "Federated": "arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com" },
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {
    "StringEquals": {
      "token.actions.githubusercontent.com:sub": "repo:KOHTA0405/dbt_snowflake:ref:refs/heads/main"
    }
  }
}
```

これで「このJWTの`sub`が`repo:KOHTA0405/dbt_snowflake:ref:refs/heads/main`と一致する場合に限り、このロールのAssumeを許可する」という条件になる。

### 3-3. 実行時の流れ

```
GitHub Actions実行開始
  → GitHubのOIDCサーバーが「このワークフロー実行専用」のJWTを発行(数分で失効)
  → aws-actions/configure-aws-credentials が sts:AssumeRoleWithWebIdentity を呼ぶ
     (JWTを提示するだけで、事前に何かのキーを渡す必要はない)
  → AWSがJWTの署名をGitHubの公開鍵で検証 + subの一致を確認
  → 一致すれば、数十分だけ有効な一時クレデンシャル
     (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN)を発行
  → ワークフローはその一時クレデンシャルでS3にアクセス
  → ワークフロー終了と同時に、GitHubのJWTもAWSの一時クレデンシャルも失効
```

**「誰も何も保存していない」のがポイント**。GitHub側にもAWS側にも、長期間有効な秘密情報は存在しない。信頼の根拠は「GitHubの秘密鍵で署名されたJWTを、正しいsubの条件で検証できた」という**その場限りの証明**だけ。

## 4. アクセスキー方式とOIDC方式の対比

| | アクセスキー方式(現在のPrefect Cloud) | OIDC方式(現在のGitHub Actions) |
| --- | --- | --- |
| 認証の根拠 | 「秘密の文字列を知っていること」 | 「信頼された発行者が署名したJWTを提示できること」 |
| 保存されるもの | Access Key ID + Secret Access Keyがどこかに保存され続ける | 何も保存されない(実行のたびにその場でJWTが発行される) |
| 有効期間 | 手動でローテーションするまで無期限 | 数分〜数十分 |
| 漏洩時の影響 | 有効期限まで誰でも悪用可能。検知しない限り気づけない | JWT自体が漏れても実行終了時には失効済み。悪用の窓が極小 |
| なりすまし対象の絞り込み | IAMユーザー単位(そのユーザーの権限全て) | `sub`条件で「特定リポジトリの特定ブランチ」のように細かく絞れる |
| ローテーション運用 | 必要(定期的なキー再発行・失効) | 不要(そもそも長期の値が存在しない) |

## 5. Prefect CloudでもOIDCが使えるのか

使える。Prefect Cloud自体が独自のOIDC発行者として振る舞う機能(AWS workload identity federation)がManaged work poolに用意されている。仕組みはGitHub Actionsの場合と同型。

```
GitHubのOIDC発行者: https://token.actions.githubusercontent.com
Prefect CloudのOIDC発行者: https://api.prefect.cloud/oidc-provider
```

AWS側でこの発行者を信頼するOIDCプロバイダを登録し、IAMロールの信頼ポリシーで`sub: prefect:account:<Prefect CloudのアカウントID>`のように絞り込めば、GitHub Actionsと同じ理屈でPrefect Cloudの各flow実行に一時クレデンシャルを注入できる。現在`aws-credentials-dev`/`aws-credentials-prd`として保存している長期アクセスキーは不要になる。

具体的な設定手順は別途整理する(このドキュメントでは「なぜ・どう安全になるか」の理解を優先した)。
