"""The monitoring configuration only refers to metrics the code actually exports.

A renamed metric would otherwise silently empty a dashboard panel or, worse, make an
alert impossible to fire.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from prometheus_client import REGISTRY

import narsat.api.app
import narsat.api.platform_metrics
import narsat.metrics  # noqa: F401 - imported for the metrics they register

INFRA = Path(__file__).resolve().parents[2] / "infra"
NAME = re.compile(r"\bnarsat_[a-z0-9_]+")


def exported_samples() -> set[str]:
    """Every sample name the registry can produce (counters with _total, histograms with
    _bucket/_sum/_count, ...)."""
    names: set[str] = set()
    for metric in REGISTRY.collect():
        if not metric.name.startswith("narsat_"):
            continue
        names.add(metric.name)
        suffixes = {
            "counter": ("_total", "_created"),
            "histogram": ("_bucket", "_sum", "_count", "_created"),
            "gauge": ("",),
        }.get(metric.type, ("",))
        names.update(metric.name + suffix for suffix in suffixes)
    return names


def alert_expressions() -> list[str]:
    rules = yaml.safe_load((INFRA / "prometheus" / "alerts.yml").read_text(encoding="utf-8"))
    return [rule["expr"] for group in rules["groups"] for rule in group["rules"]]


def dashboard_expressions() -> list[str]:
    dashboard = json.loads(
        (INFRA / "grafana" / "dashboards" / "narsat-overview.json").read_text(encoding="utf-8")
    )
    return [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]


@pytest.mark.parametrize("source", ["alerts", "dashboard"])
def test_every_referenced_metric_exists(source: str) -> None:
    expressions = alert_expressions() if source == "alerts" else dashboard_expressions()
    referenced = {name for expr in expressions for name in NAME.findall(expr)}
    assert referenced, "no NarSat metric referenced at all"
    missing = referenced - exported_samples()
    assert not missing, f"{source} refer to metrics the code does not export: {sorted(missing)}"


def test_alerts_have_a_severity_and_a_summary() -> None:
    rules = yaml.safe_load((INFRA / "prometheus" / "alerts.yml").read_text(encoding="utf-8"))
    names = []
    for group in rules["groups"]:
        for rule in group["rules"]:
            names.append(rule["alert"])
            assert rule["labels"]["severity"] in {"info", "warning", "critical"}, rule["alert"]
            assert rule["annotations"]["summary"], rule["alert"]
    assert len(names) == len(set(names))


def test_scrape_targets_match_the_compose_services() -> None:
    config = yaml.safe_load((INFRA / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
    targets = {
        job["job_name"]: job["static_configs"][0]["targets"][0] for job in config["scrape_configs"]
    }
    compose = yaml.safe_load((INFRA.parent / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    for job, target in targets.items():
        host, port = target.split(":")
        if host == "localhost":
            continue
        assert host in services, f"{job} scrapes {host}, which is not a Compose service"
        environment = services[host].get("environment", {})
        if host != "api":
            assert str(environment.get("NARSAT_METRICS_PORT")) == port, job
