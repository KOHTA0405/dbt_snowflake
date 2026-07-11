import base64
import os
from pathlib import Path

from prefect import flow
from prefect_dbt import PrefectDbtSettings
from prefect_dbt.core._orchestrator import ExecutionMode, PrefectDbtOrchestrator

DBT_PROJECT_DIR = Path(__file__).resolve().parent.parent / "jaffle_shop"


@flow(name="dbt-build")
def dbt_build_flow(target: str = "dev"):
    # Managed execution's env var injection mangles multiline PEM values, so
    # the private key is passed base64-encoded and decoded here instead. Both
    # the dev and prd keys are always injected (job_variables are static and
    # don't vary per parameter), so pick the one matching this run's target.
    key_env_var = "SNOWFLAKE_PRIVATE_KEY_PRD_B64" if target == "cloud_prd" else "SNOWFLAKE_PRIVATE_KEY_DEV_B64"
    if key_b64 := os.environ.get(key_env_var):
        os.environ["SNOWFLAKE_PRIVATE_KEY"] = base64.b64decode(key_b64).decode("utf-8")

    # PrefectDbtOrchestrator is a beta API (prefect_dbt.core._orchestrator, not
    # exported from the package's public __init__). PER_NODE mode runs each dbt
    # node as its own Prefect task/process, enabling per-node retries in the future.
    orchestrator = PrefectDbtOrchestrator(
        settings=PrefectDbtSettings(
            project_dir=DBT_PROJECT_DIR,
            profiles_dir=DBT_PROJECT_DIR,
        ),
        execution_mode=ExecutionMode.PER_NODE,
    )
    orchestrator.run_build(target=target)


if __name__ == "__main__":
    dbt_build_flow()
