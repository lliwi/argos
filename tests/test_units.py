"""Tests unitarios de piezas sin E/S externa."""

from __future__ import annotations

from pathlib import Path

import pytest

from argos.audit.events import ErrorKind, RiskClass, SessionStarted, Turn
from argos.audit.redact import Redactor
from argos.config import Profile
from argos.governance.budget import BudgetTracker
from argos.governance.retry import with_retry
from argos.model.base import DecisionParseError, ModelAuthError, ModelError, parse_decision
from argos.model.codex_cli import parse_codex_events
from argos.sandbox.egress_proxy import host_allowed
from argos.tools.base import ToolError
from argos.tools.shell import ShellExecTool, offensive_targets, parse_installs
from argos.tools.workspace import resolve

FIXTURES = Path(__file__).parent / "fixtures"


# --- redacción (RF-OB-11) ----------------------------------------------------------------------

def test_redacts_known_secrets_and_patterns():
    r = Redactor()
    r.register_secret("super-secreto-123")
    text = ("token super-secreto-123 y api_key=abcdef123456 y ghp_" + "a" * 36
            + " mail pepe@example.com tel 612 345 678")
    out = r.redact_text(text)
    assert "super-secreto-123" not in out and "abcdef123456" not in out
    assert "ghp_" not in out and "pepe@example.com" not in out and "612 345 678" not in out
    assert "[REDACTED:secret]" in out and "api_key=[REDACTED:assignment]" in out


def test_redaction_preserves_structural_ids():
    r = Redactor()
    data = {"id": "1234567890123456789", "session_id": "abc", "command": "echo 4111111111111111"}
    out = r.redact(data)
    assert out["id"] == data["id"] and "4111111111111111" not in out["command"]


def test_pii_redaction_can_be_disabled():
    assert "a@b.com" in Redactor(redact_pii=False).redact_text("a@b.com")


# --- parser de decisiones y Codex --------------------------------------------------------------

def test_parse_decision_variants():
    d = parse_decision('```json\n{"type":"tool_call","tool":"shell.exec",'
                       '"args_json":"{\\"command\\": \\"ls\\"}","message":""}\n```')
    assert d.tool == "shell.exec" and d.args == {"command": "ls"}
    d = parse_decision('{"type":"final","tool":"","args_json":"","message":"hecho"}')
    assert d.type == "final" and d.tool is None and d.message == "hecho"
    d = parse_decision('{"type":"tool_call","tool":"x","args":{"a":1}}')
    assert d.args == {"a": 1}


@pytest.mark.parametrize("bad", ["sin json", '{"type":"otro"}', '{"type":"tool_call"}',
                                 '{"type":"tool_call","tool":"x","args_json":"{no"}'])
def test_parse_decision_rejects(bad):
    with pytest.raises(DecisionParseError):
        parse_decision(bad)


def test_codex_failed_turn_raises_model_error():
    lines = (FIXTURES / "codex_turn_failed_auth.jsonl").read_text().splitlines()
    with pytest.raises(ModelAuthError, match="codex login"):
        parse_codex_events(lines)
    assert issubclass(ModelAuthError, ModelError)


def test_codex_success_stream_extracts_text_usage_and_violations():
    lines = (FIXTURES / "codex_turn_ok.jsonl").read_text().splitlines()
    text, usage, violations = parse_codex_events(lines)
    assert parse_decision(text).tool == "shell.exec"
    assert (usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens) == (14229, 0, 83)
    assert violations == []


def test_codex_detects_engine_acting_on_its_own():
    item = ('{"type":"item.completed","item":{"id":"i","type":"command_execution",'
            '"command":"ls","exit_code":0}}')
    _, _, violations = parse_codex_events([item])
    assert violations and violations[0].startswith("command_execution")


# --- shell: riesgo, instalaciones, alcance -----------------------------------------------------

def test_install_detection():
    got = parse_installs("pip install --quiet pandas==2.2.2 numpy && npm i -g @scope/pkg@1.0")
    assert ("pip", "pandas", "2.2.2") in got and ("pip", "numpy", None) in got
    assert ("npm", "@scope/pkg", "1.0") in got


@pytest.mark.parametrize("cmd,risk", [
    ("ls -la", RiskClass.WRITE),
    ("rm -rf /home/agent", RiskClass.DESTRUCTIVE),
    ("rm -fr build", RiskClass.DESTRUCTIVE),
    ("nmap -sV 10.0.0.5", RiskClass.OFFENSIVE),
    ("echo nmap", RiskClass.WRITE),
    ('bash -c "nmap 10.0.0.5"', RiskClass.OFFENSIVE),
    ("cd x && /usr/bin/sqlmap -u http://a.b", RiskClass.OFFENSIVE),
])
def test_shell_risk(cmd, risk):
    assert ShellExecTool().risk_for({"command": cmd}) == risk


def test_offensive_targets():
    assert offensive_targets("nmap -p 80 web.example.org") == ["web.example.org"]
    assert offensive_targets("curl example.org") is None


# --- workspace ---------------------------------------------------------------------------------

def test_workspace_resolution(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    assert resolve(tmp_path, "informe.txt", writable=True) == (tmp_path / "out/informe.txt")
    assert resolve(tmp_path, "in/datos.csv", writable=False) == (tmp_path / "in/datos.csv")
    for bad, writable in (("in/x", True), ("../../etc/passwd", False), ("out/../../x", True)):
        with pytest.raises(ToolError) as exc:
            resolve(tmp_path, bad, writable=writable)
        assert exc.value.kind == ErrorKind.VALIDATION_ERROR


# --- egress ------------------------------------------------------------------------------------

def test_host_allowlist():
    allow = ["pypi.org", "*.githubusercontent.com"]
    assert host_allowed("pypi.org", allow) and host_allowed("PYPI.org.", allow)
    assert host_allowed("raw.githubusercontent.com", allow)
    assert not host_allowed("evilpypi.org", allow) and not host_allowed("example.com", allow)


def test_host_allowlist_ip_and_cidr():
    allow = ["pypi.org", "192.168.0.0/16", "10.1.2.3"]
    assert host_allowed("192.168.0.20", allow) and host_allowed("192.168.5.9", allow)
    assert host_allowed("10.1.2.3", allow) and not host_allowed("10.1.2.4", allow)
    assert not host_allowed("8.8.8.8", allow)          # externo: no está permitido
    assert not host_allowed("192.168.0.20", ["pypi.org"])   # sin rango: bloqueado
    assert host_allowed("pypi.org", allow)             # dominios siguen funcionando


# --- perfiles (P2) -----------------------------------------------------------------------------

def test_profile_rejects_untrusted_plus_powerful_secrets():
    with pytest.raises(ValueError, match="P2"):
        Profile(name="x", reads_untrusted=True, secrets=[{"name": "T", "powerful": True}])


def test_profile_tool_globs():
    p = Profile(name="x", tools=["workspace.*", "shell.exec"])
    assert p.allows_tool("workspace.read_file") and not p.allows_tool("reminders.add")


# --- presupuesto y reintentos ------------------------------------------------------------------

def test_budget_warn_pause_abort():
    events = []
    b = BudgetTracker("s", 100, 10_000, 0.8, 0, events.append)
    assert b.add(50) == "ok"
    assert b.add(35) == "warn"
    assert b.add(20) == "pause"
    b.extend()
    assert b.add(100) == "abort"
    assert [e.action for e in events] == ["warn", "pause", "abort"]


def test_budget_day_limit_blocks_start():
    events = []
    assert BudgetTracker("s", 100, 1000, 0.8, 1000, events.append).check_start() == "abort"
    assert events[0].scope == "day"


async def test_retry_only_idempotent():
    calls = []

    async def flaky(n):
        calls.append(n)
        raise OSError("boom")

    with pytest.raises(OSError):
        await with_retry(flaky, idempotent=False, base_delay=0)
    assert calls == [1]
    calls.clear()
    with pytest.raises(OSError):
        await with_retry(flaky, idempotent=True, attempts=3, base_delay=0)
    assert calls == [1, 2, 3]


# --- store de auditoría ------------------------------------------------------------------------

def test_store_roundtrip_and_blobs(store):
    store.emit(SessionStarted(session_id="s1", agent_profile="p", channel="cli", task="t",
                              model="m", prompt_version="v", config_hash="c",
                              harness_commit="h"))
    store.emit(Turn(session_id="s1", seq=1, model="m", prompt_tokens=10))
    evs = store.events("s1")
    assert [e.type for e in evs] == ["session", "turn"]
    assert (store.jsonl_dir / "s1.jsonl").read_text().count("\n") == 2
    ref = store.put_blob("hola password=12345678")
    assert "12345678" not in store.get_blob(ref)
    assert store.sessions()[0]["id"] == "s1" and store.resolve_session("s") == "s1"


@pytest.mark.parametrize("text,leaks", [
    ("mi usuario es argo y la contraseña djhqk98_jkolpK9, recuérdalo", "djhqk98_jkolpK9"),
    ("La clave del wifi es Casa2026!", "Casa2026!"),
    ("password: hunter2x", "hunter2x"),
    ("el pin es 483920", "483920"),
])
def test_redacts_spanish_and_free_form_credentials(text, leaks):
    out = Redactor().redact_text(text)
    assert leaks not in out and "[REDACTED:" in out


@pytest.mark.parametrize("text", ["la contraseña es segura", "cambia la clave cuando puedas",
                                  "el token expira mañana"])
def test_does_not_redact_ordinary_sentences(text):
    assert Redactor(redact_pii=False).redact_text(text) == text
