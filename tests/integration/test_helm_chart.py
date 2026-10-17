"""Integration: helm-чарт (stage B, ADR-0013) — настоящий ``helm template``.

* За ingress каждый клиент приходит в campaign-manager с адреса пода
  контроллера: без ``campaignManager.forwardedAllowIps`` троттлинг логина
  считал всех одним IP (5 неверных паролей блокировали email для всех,
  30 попыток — вход вообще). С ingress значение обязательно, «*» — отказ.
* Секрет из одних пробелов не проходит проверку длины (``trim`` до ``len``).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

CHART = Path(__file__).resolve().parents[2] / "helm" / "cashback"
CM_TEMPLATE = "templates/deployment-campaign-manager.yaml"
INGRESS_CIDR = "10.244.0.0/16"
SECRETS = {
    "secrets.jwtSecret": "a" * 64,
    "secrets.serviceJwtSecret": "b" * 64,
    "secrets.mobileJwtSecret": "c" * 64,
    "secrets.adminBootstrapPassword": "Bootstrap-chosen-42",
}


@pytest.fixture(scope="module")
def helm_template(require_tool):
    helm = require_tool("helm")

    def _run(*, show_only: str | None = None,
             **values: str) -> subprocess.CompletedProcess:
        cmd = [helm, "template", "cashback", str(CHART)]
        for key, value in {**SECRETS, **values}.items():
            # --set-string: секреты всегда строки; булевы (ingress.enabled) — --set.
            flag = "--set-string" if key.startswith("secrets.") else "--set"
            cmd += [flag, f"{key}={value}"]
        if show_only:
            cmd += ["--show-only", show_only]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    return _run


def _env_value(rendered: str, name: str) -> str | None:
    match = re.search(rf'^\s*- name: {name}\n\s*value: "([^"]*)"$', rendered, re.MULTILINE)
    return match.group(1) if match else None


def test_ingress_requires_the_trusted_proxy_cidr(helm_template):
    proc = helm_template()          # values.yaml: ingress.enabled=true
    assert proc.returncode != 0
    assert "campaignManager.forwardedAllowIps is required" in proc.stderr


def test_trusted_proxy_cidr_reaches_campaign_manager(helm_template):
    proc = helm_template(show_only=CM_TEMPLATE,
                         **{"campaignManager.forwardedAllowIps": f" {INGRESS_CIDR} "})
    assert proc.returncode == 0, proc.stderr
    assert _env_value(proc.stdout, "FORWARDED_ALLOW_IPS") == INGRESS_CIDR


def test_trusting_every_peer_is_refused(helm_template):
    proc = helm_template(**{"campaignManager.forwardedAllowIps": "*"})
    assert proc.returncode != 0
    assert "must not be \"*\"" in proc.stderr


def test_without_ingress_the_value_is_optional(helm_template):
    proc = helm_template(show_only=CM_TEMPLATE, **{"ingress.enabled": "false"})
    assert proc.returncode == 0, proc.stderr
    assert _env_value(proc.stdout, "FORWARDED_ALLOW_IPS") is None   # uvicorn: 127.0.0.1


@pytest.mark.parametrize("key", ["secrets.jwtSecret", "secrets.serviceJwtSecret",
                                 "secrets.mobileJwtSecret"])
def test_whitespace_only_secret_is_refused(helm_template, key):
    proc = helm_template(**{key: " " * 40,
                            "campaignManager.forwardedAllowIps": INGRESS_CIDR})
    assert proc.returncode != 0
    assert f"{key} must be at least 32 bytes" in proc.stderr
