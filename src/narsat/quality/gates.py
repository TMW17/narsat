"""Quality gates between the layers of the lakehouse.

A gate is a list of named checks. Each check ends as ``pass``, ``warn`` or ``fail``
and has a severity: a failed check with severity ``error`` blocks publication (the
CLI exits non-zero, so the orchestrator stops the downstream tasks); a ``warn``
result is recorded and shown on the data-quality page but lets the run continue.

Results are written to the ``dq_result`` table so that quality has a history.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
import pandera.pandas as pa

from narsat import metrics
from narsat.db import Connection
from narsat.log import get_logger

log = get_logger(__name__)

Status = Literal["pass", "warn", "fail"]
Severity = Literal["warn", "error"]


@dataclass(frozen=True)
class CheckResult:
    suite: str
    check_name: str
    status: Status
    severity: Severity = "error"
    observed: dict[str, Any] = field(default_factory=dict)
    detail: str | None = None

    @property
    def blocking(self) -> bool:
        return self.status == "fail" and self.severity == "error"


class GateFailed(RuntimeError):
    def __init__(self, failures: Sequence[CheckResult]) -> None:
        self.failures = list(failures)
        names = ", ".join(f"{f.suite}.{f.check_name}" for f in self.failures)
        super().__init__(f"data-quality gate failed: {names}")


def schema_checks(suite: str, frame: pd.DataFrame, schema: pa.DataFrameSchema) -> list[CheckResult]:
    """Validate a frame against a pandera schema; one result per violated rule."""
    try:
        schema.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        cases = exc.failure_cases
        results: list[CheckResult] = []
        column_level = cases["schema_context"] == "Column"
        for (column, check), group in cases.loc[column_level].groupby(["column", "check"]):
            results.append(
                CheckResult(
                    suite,
                    f"schema[{column}:{check}]",
                    "fail",
                    observed={
                        "violations": len(group),
                        "examples": [str(v) for v in group["failure_case"].head(5)],
                    },
                )
            )
        # A rule over several columns reports every column of each offending row;
        # what matters is the rule and how many rows break it.
        for check, group in cases.loc[~column_level].groupby("check", dropna=False):
            rows = group["index"].dropna().unique()
            results.append(
                CheckResult(
                    suite,
                    f"schema[{check}]",
                    "fail",
                    observed={
                        "violations": len(rows) if len(rows) else len(group),
                        "example_rows": [int(r) for r in rows[:5]],
                    },
                )
            )
        return results
    return [CheckResult(suite, "schema", "pass", observed={"rows": len(frame)})]


def ratio_check(
    suite: str,
    name: str,
    numerator: int,
    denominator: int,
    minimum: float,
    severity: Severity = "error",
) -> CheckResult:
    """Pass when ``numerator / denominator >= minimum`` (vacuously true on empty input)."""
    ratio = 1.0 if denominator == 0 else numerator / denominator
    observed = {
        "numerator": numerator,
        "denominator": denominator,
        "ratio": round(ratio, 6),
        "minimum": minimum,
    }
    if ratio >= minimum:
        return CheckResult(suite, name, "pass", severity, observed)
    status: Status = "fail" if severity == "error" else "warn"
    return CheckResult(suite, name, status, severity, observed)


def silver_hotspot_checks(
    silver: pd.DataFrame, bronze_ids: set[str], schema: pa.DataFrameSchema
) -> list[CheckResult]:
    suite = "silver.hotspots"
    results = schema_checks(suite, silver, schema)
    silver_ids = set(silver["hotspot_id"]) if not silver.empty else set()
    missing = bronze_ids - silver_ids
    results.append(
        CheckResult(
            suite,
            "complete_versus_bronze",
            "pass" if not missing else "fail",
            observed={"bronze_ids": len(bronze_ids), "missing_in_silver": len(missing)},
            detail=None if not missing else f"e.g. {sorted(missing)[:3]}",
        )
    )
    if not silver.empty:
        dynamic = silver.loc[~silver["is_static_source"]]
        results.append(
            ratio_check(
                suite,
                "hotspots_assigned_to_event",
                int(dynamic["event_id"].notna().sum()),
                len(dynamic),
                0.99,
                "warn",
            )
        )
        results.append(
            ratio_check(
                suite,
                "hotspots_with_country",
                int(silver["country_iso3"].notna().sum()),
                len(silver),
                0.90,
                "warn",
            )
        )
    return results


def silver_event_checks(
    events: pd.DataFrame, silver_hotspots: pd.DataFrame, schema: pa.DataFrameSchema
) -> list[CheckResult]:
    suite = "silver.fire_events"
    results = schema_checks(suite, events, schema)
    if events.empty:
        return results
    live = events.loc[events["status"].isin(["active", "cooling", "closed"])]
    members = (
        silver_hotspots.loc[silver_hotspots["event_id"].notna()].groupby("event_id").size()
        if not silver_hotspots.empty
        else pd.Series(dtype="int64")
    )
    counted = live["event_id"].map(members).fillna(0).astype("int64")
    # More members in silver than the event row claims cannot happen in a consistent
    # snapshot: it means the enrichment of an older month is stale (rebuild it).
    exceeded = live.loc[counted > live["n_hotspots"]]
    results.append(
        CheckResult(
            suite,
            "member_count_not_exceeded",
            "pass" if exceeded.empty else "fail",
            observed={"live_events": len(live), "exceeded": len(exceeded)},
            detail=None if exceeded.empty else f"e.g. {exceeded['event_id'].head(3).tolist()}",
        )
    )
    # Fewer members can be transient (detections arriving while the snapshot is taken)
    # or mean that some months were not rebuilt, so it only warns.
    results.append(
        ratio_check(
            suite,
            "member_count_complete",
            int((counted == live["n_hotspots"]).sum()),
            len(live),
            0.98,
            "warn",
        )
    )
    return results


def gold_burned_area_checks(
    burned: pd.DataFrame, events: pd.DataFrame, schema: pa.DataFrameSchema
) -> list[CheckResult]:
    suite = "gold.burned_areas"
    results = schema_checks(suite, burned, schema)
    if burned.empty:
        return results
    known = set(events["event_id"]) if not events.empty else set()
    orphans = burned.loc[~burned["event_id"].isin(known)]
    results.append(
        CheckResult(
            suite,
            "event_exists",
            "pass" if orphans.empty else "fail",
            observed={"rows": len(burned), "orphans": len(orphans)},
        )
    )
    results.append(
        ratio_check(
            suite,
            "non_empty_products",
            int((burned["area_ha"] > 0).sum()),
            len(burned),
            0.5,
            "warn",
        )
    )
    return results


def record(conn: Connection, results: Sequence[CheckResult], run_id: str | None = None) -> str:
    """Persist results; returns the run identifier they were stored under."""
    run = run_id or uuid.uuid4().hex
    for result in results:
        conn.execute(
            """
            INSERT INTO dq_result (run_id, suite, check_name, status, severity, observed, detail)
            VALUES (%(run)s, %(suite)s, %(name)s, %(status)s, %(severity)s, %(observed)s::jsonb,
                    %(detail)s)
            """,
            {
                "run": run,
                "suite": result.suite,
                "name": result.check_name,
                "status": result.status,
                "severity": result.severity,
                "observed": json.dumps(result.observed, default=str),
                "detail": result.detail,
            },
        )
        metrics.DQ_CHECKS.labels(result.suite, result.status).inc()
    return run


def enforce(results: Sequence[CheckResult]) -> None:
    """Raise ``GateFailed`` if any blocking check failed; log everything that is not a pass."""
    for result in results:
        if result.status != "pass":
            log.warning(
                "dq_check",
                suite=result.suite,
                check=result.check_name,
                status=result.status,
                observed=result.observed,
                detail=result.detail,
            )
    blocking = [r for r in results if r.blocking]
    if blocking:
        raise GateFailed(blocking)
