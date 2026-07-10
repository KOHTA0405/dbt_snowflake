from pathlib import Path

from prefect import flow
from prefect_dbt import PrefectDbtRunner, PrefectDbtSettings

DBT_PROJECT_DIR = Path(__file__).resolve().parent.parent / "jaffle_shop"


@flow(name="dbt-build")
def dbt_build_flow(target: str = "dev"):
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
