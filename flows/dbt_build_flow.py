import base64
import os
from datetime import timedelta
from pathlib import Path

from prefect import flow, task
from prefect_aws import S3Bucket
from prefect_dbt import PrefectDbtSettings
from prefect_dbt.core._orchestrator import CacheConfig, ExecutionMode, PrefectDbtOrchestrator

DBT_PROJECT_DIR = Path(__file__).resolve().parent.parent / "jaffle_shop"

# Short enough to speed up same-day retries after a partial failure, but
# shorter than the daily schedule so every scheduled run rebuilds fresh
# instead of skipping unchanged models with stale source data.
CACHE_EXPIRATION = timedelta(hours=12)


@task
def upload_manifest_to_s3():
    manifest_path = DBT_PROJECT_DIR / "target" / "manifest.json"
    s3_bucket = S3Bucket.load("s3-bucket-prd")
    s3_bucket.upload_from_path(manifest_path, "manifest/manifest.json")


# Run as a subflow so the per-node dbt tasks are grouped under a single,
# collapsible node in the parent flow run's graph in the Prefect UI, keeping
# them visually separate as sibling tasks (e.g. upload_manifest_to_s3) are added.
@flow(name="dbt-build-run")
def run_dbt_build(target: str, cache: CacheConfig | None):
    # PrefectDbtOrchestrator is a beta API (prefect_dbt.core._orchestrator, not
    # exported from the package's public __init__). PER_NODE mode runs each dbt
    # node as its own Prefect task/process, enabling per-node retries in the future.
    orchestrator = PrefectDbtOrchestrator(
        settings=PrefectDbtSettings(
            project_dir=DBT_PROJECT_DIR,
            profiles_dir=DBT_PROJECT_DIR,
        ),
        execution_mode=ExecutionMode.PER_NODE,
        cache=cache,
    )
    orchestrator.run_build(target=target)


@flow(name="jaffle-shop-pipeline")
def dbt_build_flow(target: str = "dev"):
    # Managed execution's env var injection mangles multiline PEM values, so
    # the private key is passed base64-encoded and decoded here instead.
    # Both dev and prd keys are decoded regardless of the requested target:
    # PrefectDbtOrchestrator's internal manifest-parsing step (triggered when
    # target/manifest.json doesn't exist yet) doesn't forward --target and
    # always falls back to profiles.yml's default target (dev), so dev's key
    # must be available even when running against prd.
    for env_name in ("DEV", "PRD"):
        if key_b64 := os.environ.get(f"SNOWFLAKE_PRIVATE_KEY_{env_name}_B64"):
            os.environ[f"SNOWFLAKE_PRIVATE_KEY_{env_name}"] = base64.b64decode(key_b64).decode("utf-8")

    # Only cache prod builds: caching skips re-running nodes whose code hasn't
    # changed, which would defeat the point of a daily refresh in dev/CI-like
    # contexts, and adds an AWS dependency to the local dev loop for no benefit.
    cache = None
    if target == "prd":
        cache = CacheConfig(
            result_storage=S3Bucket.load("s3-bucket-prd-cache"),
            expiration=CACHE_EXPIRATION,
        )

    run_dbt_build(target=target, cache=cache)

    # run_build() raises DbtBuildFailed on any node error, so this only runs
    # after a fully successful build.
    if target == "prd":
        upload_manifest_to_s3()


if __name__ == "__main__":
    dbt_build_flow()
