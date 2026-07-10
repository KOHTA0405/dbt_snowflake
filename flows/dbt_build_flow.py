import base64
import os
from pathlib import Path

from prefect import flow
from prefect_dbt import PrefectDbtRunner, PrefectDbtSettings

DBT_PROJECT_DIR = Path(__file__).resolve().parent.parent / "jaffle_shop"


@flow(name="dbt-build")
def dbt_build_flow(target: str = "dev"):
    # Managed execution's env var injection mangles multiline PEM values,
    # so the private key is passed base64-encoded and decoded here instead.
    if key_b64 := os.environ.get("SNOWFLAKE_PRIVATE_KEY_B64"):
        os.environ["SNOWFLAKE_PRIVATE_KEY"] = base64.b64decode(key_b64).decode("utf-8")

    runner = PrefectDbtRunner(
        settings=PrefectDbtSettings(
            project_dir=DBT_PROJECT_DIR,
            profiles_dir=DBT_PROJECT_DIR,
        )
    )
    runner.invoke(["deps"])
    runner.invoke(["build", "--target", target])


if __name__ == "__main__":
    dbt_build_flow()
