"""DAG integrity tests. Run them in an environment that has Apache Airflow installed:

    python -m pytest orchestration/tests -q

They are skipped elsewhere (the platform's own environment does not include Airflow).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("airflow.sdk", reason="Apache Airflow is not installed in this environment")

DAG_FILE = Path(__file__).resolve().parents[1] / "dags" / "narsat_pipelines.py"
EXPECTED = {
    "narsat_map_events": ["update_events", "map_pending", "score_agreement"],
    "narsat_lakehouse_refresh": ["refresh_lake", "build_marts", "publish_marts"],
    "narsat_lake_maintenance": ["maintain_lake"],
    "narsat_firms_backfill": ["backfill"],
}


@pytest.fixture(scope="module")
def dags() -> dict[str, object]:
    from airflow.sdk import DAG

    spec = importlib.util.spec_from_file_location("narsat_pipelines", DAG_FILE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["narsat_pipelines"] = module
    spec.loader.exec_module(module)
    return {value.dag_id: value for value in vars(module).values() if isinstance(value, DAG)}


def test_every_dag_is_defined_with_its_tasks_in_order(dags: dict) -> None:
    assert set(dags) == set(EXPECTED)
    for dag_id, task_ids in EXPECTED.items():
        dag = dags[dag_id]
        assert [task.task_id for task in dag.topological_sort()] == task_ids
        for upstream, downstream in zip(task_ids, task_ids[1:], strict=False):
            assert downstream in dag.get_task(upstream).downstream_task_ids


def test_dags_do_not_backfill_or_overlap(dags: dict) -> None:
    for dag in dags.values():
        assert dag.catchup is False
        assert dag.max_active_runs == 1
        assert "narsat" in dag.tags
        assert dag.doc_md


def test_tasks_only_call_the_cli_or_dbt(dags: dict) -> None:
    for dag in dags.values():
        for task in dag.tasks:
            command = task.bash_command
            assert command.startswith(("/opt/narsat/bin/narsat ", "mkdir -p ")), command
            assert task.execution_timeout is not None
            assert not command.rstrip().endswith((".sh", ".bash"))  # would be read as a template


def test_schedules(dags: dict) -> None:
    schedules = {dag_id: dag.schedule for dag_id, dag in dags.items()}
    assert schedules == {
        "narsat_map_events": "20 * * * *",
        "narsat_lakehouse_refresh": "30 2 * * *",
        "narsat_lake_maintenance": "0 5 * * 0",
        "narsat_firms_backfill": None,
    }


def test_backfill_parameters_are_validated(dags: dict) -> None:
    from airflow.sdk.exceptions import ParamValidationError

    params = dags["narsat_firms_backfill"].params
    params.validate()  # the defaults are valid
    assert params["source"] == "VIIRS_SNPP_SP"
    for name, bad_value in (
        ("source", "VIIRS; rm -rf /"),
        ("start", "2023-07-01; echo pwned"),
        ("end", "yesterday"),
    ):
        param = params.get_param(name)
        with pytest.raises(ParamValidationError):
            param.resolve(bad_value)
