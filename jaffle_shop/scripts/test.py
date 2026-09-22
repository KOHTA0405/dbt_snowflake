import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import duckdb

TERRAFORM_DIR = Path(__file__).resolve().parents[3] / "snowflake" / "terraform" / "snowflake"
SNOWFLAKE_ACCOUNT_IDENTIFIER = "HEWLHVQ-QR52630"
SNOWFLAKE_ROLE = "DEVELOPER_DEV"


def terraform_stdout(*args: str) -> str:
    result = subprocess.run(
        ["terraform", f"-chdir={TERRAFORM_DIR}", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Terraform state を読み取れませんでした。AWS 認証を確認してください。")
    return result.stdout


def load_pat() -> str:
    if terraform_stdout("workspace", "show").strip() != "dev":
        raise RuntimeError("Snowflake Terraform の dev workspace を選択してください。")

    state = json.loads(terraform_stdout("state", "pull"))
    for resource in state.get("resources", []):
        if (
            resource.get("type") == "snowflake_user_programmatic_access_token"
            and resource.get("name") == "duckdb_iceberg_dev"
        ):
            for instance in resource.get("instances", []):
                token = instance.get("attributes", {}).get("token")
                if token:
                    return token
    raise RuntimeError("dev の DuckDB 用 PAT が Terraform state に見つかりません。")


def exchange_pat_for_access_token(pat: str) -> str:
    token_url = (
        f"https://{SNOWFLAKE_ACCOUNT_IDENTIFIER}.snowflakecomputing.com"
        "/polaris/api/catalog/v1/oauth/tokens"
    )
    request = Request(
        token_url,
        data=urlencode(
            {
                "grant_type": "client_credentials",
                "scope": f"session:role:{SNOWFLAKE_ROLE}",
                "client_secret": pat,
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request) as response:
            access_token = json.load(response).get("access_token")
    except (HTTPError, URLError):
        raise RuntimeError(
            "Snowflake access token を取得できませんでした。"
            "PAT の有効期限、ネットワークポリシー、認証ポリシーを確認してください。"
        ) from None

    if not access_token:
        raise RuntimeError("Snowflake access token の応答が不正です。")
    return access_token


def main() -> None:
    access_token = exchange_pat_for_access_token(load_pat())
    escaped_token = access_token.replace("'", "''")

    with duckdb.connect() as con:
        con.execute("INSTALL iceberg")
        con.execute("LOAD iceberg")
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        try:
            con.execute(f"CREATE SECRET horizon_token (TYPE iceberg, TOKEN '{escaped_token}')")
        except duckdb.Error:
            raise RuntimeError("DuckDB secret を作成できませんでした。PAT の値は表示しません。") from None

        con.execute("""
            ATTACH 'DEV' AS horizon (
                TYPE iceberg,
                SECRET horizon_token,
                ENDPOINT 'https://hewlhvq-qr52630.snowflakecomputing.com/polaris/api/catalog',
                ACCESS_DELEGATION_MODE 'vended_credentials'
            )
        """)

        print(con.execute("SHOW ALL TABLES").fetchall())
        print(con.execute("SELECT * FROM horizon.GOLD.ICEBERG_SMOKE_TEST").fetchall())


if __name__ == "__main__":
    main()
