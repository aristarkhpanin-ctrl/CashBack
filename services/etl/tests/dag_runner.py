"""Run ``cashback_daily_etl``'s task code without Airflow.

``airflow.decorators`` is replaced by a recorder: every ``@task`` call
returns a :class:`Node` that remembers its arguments — the same upstream
edges TaskFlow derives from them. :func:`run` executes the recorded tasks
in dependency order with Airflow's default ``all_success`` trigger rule.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

DAG_FILE = Path(__file__).resolve().parents[1] / "dags" / "cashback_daily_etl.py"


class Node:
    def __init__(self, name: str, fn: Any, args: tuple) -> None:
        self.name, self.fn, self.args = name, fn, args

    def upstream(self) -> set[str]:
        return {a.name for a in self.args if isinstance(a, Node)}


def load_dag(monkeypatch) -> dict[str, Node]:
    """Import the DAG file under a recording ``airflow.decorators`` stub."""
    nodes: dict[str, Node] = {}

    def task(*_a, **_kw):
        def deco(fn):
            def call(*args):
                nodes[fn.__name__] = Node(fn.__name__, fn, args)
                return nodes[fn.__name__]
            return call
        return deco

    def dag(**_kw):
        return lambda fn: fn

    decorators = types.ModuleType("airflow.decorators")
    decorators.dag, decorators.task = dag, task
    airflow = types.ModuleType("airflow")
    airflow.decorators = decorators
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.decorators", decorators)
    spec = importlib.util.spec_from_file_location("cashback_daily_etl_under_test", DAG_FILE)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    return nodes


def run(nodes: dict[str, Node], stubs: dict[str, Any]) -> tuple[dict, dict[str, BaseException]]:
    """Run tasks in recorded (= topological) order; a failed upstream skips the task.

    Returns (results by task, exceptions by failed task — ``None`` value for
    tasks that did not run because an upstream failed).
    """
    results: dict[str, Any] = dict(stubs)
    failed: dict[str, BaseException | None] = {}
    for node in nodes.values():
        if node.name in stubs:
            continue
        if node.upstream() & failed.keys():
            failed[node.name] = None  # upstream_failed
            continue
        args = [results[a.name] if isinstance(a, Node) else a for a in node.args]
        try:
            results[node.name] = node.fn(*args)
        except Exception as exc:  # noqa: BLE001 — a task failure, as Airflow sees it
            failed[node.name] = exc
    return results, failed
