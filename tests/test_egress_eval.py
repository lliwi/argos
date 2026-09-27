"""Proxy de egress en vivo (localhost), runner de evaluación y sandbox Docker real."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid

import pytest

from argos.eval.runner import compare_to_baseline, run_suite
from argos.sandbox.docker_sandbox import DockerSandbox, EgressPolicy
from argos.sandbox.egress_proxy import EgressProxy


async def test_proxy_blocks_and_logs(tmp_path):
    (tmp_path / "policy.json").write_text(json.dumps(
        {"default": ["allowed.test"], "clients": {"127.0.0.1": ["extra.test"]}}))
    proxy = EgressProxy(tmp_path)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT evil.test:443 HTTP/1.1\r\nHost: evil.test\r\n\r\n")
        await writer.drain()
        response = (await reader.read(4096)).decode()
        writer.close()
    finally:
        server.close()
    assert response.startswith("HTTP/1.1 403") and "allowlist" in response
    ev = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert ev["decision"] == "blocked" and ev["host"] == "evil.test"
    assert proxy.policy.allowlist_for("127.0.0.1") == ["allowed.test", "extra.test"]
    assert proxy.policy.allowlist_for("10.0.0.9") == ["allowed.test"]


async def test_golden_suite_with_fake_provider(cfg, store):
    """CA-8: la suite corre, puntúa y registra eval_run (tareas sin Docker)."""
    summary = await run_suite(cfg, store, "golden", "fake", None,
                              only=["g002-reminder", "g004-budget"])
    assert [t["status"] for t in summary["tasks"]] == ["passed", "passed"]
    sid = summary["tasks"][0]["session_id"]
    runs = store.events(sid, ["eval_run"])
    assert runs and runs[0].passed and runs[0].score == 1.0
    assert compare_to_baseline(summary, summary) == []


def test_regression_gate_detects_drop():
    base = {"aggregate": {"success_rate": 1.0}, "tasks": [
        {"task_id": "t", "status": "passed", "score": 1.0, "metrics": {"tokens": 100}}]}
    cur = {"aggregate": {"success_rate": 0.0}, "tasks": [
        {"task_id": "t", "status": "failed", "score": 0.5, "metrics": {"tokens": 200}}]}
    regs = compare_to_baseline(cur, base)
    assert any("pasaba" in r for r in regs) and any("tokens" in r for r in regs)
    assert any("success_rate" in r for r in regs)


def _docker_ready(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    ok = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode
    net = subprocess.run(["docker", "network", "inspect", "argos_sandbox_main"],
                         capture_output=True).returncode
    return ok == 0 and net == 0


@pytest.mark.docker
async def test_real_sandbox_isolation(cfg):
    if not _docker_ready(cfg.sandbox.image):
        pytest.skip("imagen de sandbox o red argos_sandbox no disponibles")
    policy = EgressPolicy(cfg.egress_path())
    sid = uuid.uuid4().hex
    sb = DockerSandbox(sid, cfg.data_path / "workspaces" / sid, cfg.sandbox, policy,
                       network=cfg.segment_network(), proxy_url=cfg.segment_proxy())
    try:
        res = await sb.exec("id -u; touch /etc/x 2>/dev/null; echo ro=$?; "
                            "echo x > /workspace/in/x 2>/dev/null; echo in=$?; "
                            "echo ok > /workspace/out/f; cat /workspace/out/f")
        assert res.stdout.split() == ["1000", "ro=1", "in=1", "ok"]
        timed = await sb.exec("sleep 5", timeout_s=1)
        assert timed.timed_out
    finally:
        await sb.destroy()
