"""Skills (§9): formato, instalación, divulgación progresiva y auditoría."""

from __future__ import annotations

import pytest

from argos.core.session import SessionOptions, run_session
from argos.model.fake import FakeProvider
from argos.skills import SkillError, SkillRegistry, parse_skill


def write_skill(path, name="demo", body="Haz X con cuidado.", version="1.0.0"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Skill de prueba\nversion: {version}\n---\n{body}\n")
    return path


def test_parse_and_version_hash(tmp_path):
    sk = parse_skill(write_skill(tmp_path / "demo") / "SKILL.md")
    assert sk.name == "demo" and sk.full_version.startswith("1.0.0@") and "Haz X" in sk.body


@pytest.mark.parametrize("text", [
    "sin front-matter",
    "---\nname: Mal Nombre\ndescription: x\n---\nb",
    "---\nname: ok\n---\ncuerpo",
    "---\nname: ok\ndescription: d\n---\n",
])
def test_invalid_skills_rejected(tmp_path, text):
    (tmp_path / "SKILL.md").write_text(text)
    with pytest.raises(SkillError):
        parse_skill(tmp_path / "SKILL.md")


def test_install_and_update(tmp_path):
    reg = SkillRegistry(tmp_path / "skills")
    src = write_skill(tmp_path / "src")
    assert reg.install(str(src)).name == "demo"
    with pytest.raises(SkillError, match="ya existe"):
        reg.install(str(src))
    write_skill(src, version="1.1.0")
    assert reg.install(str(src), force=True).version == "1.1.0"
    assert SkillRegistry(tmp_path / "skills").skills["demo"].version == "1.1.0"


async def test_progressive_disclosure_and_audit(cfg, store, fake_sandbox):
    """RF-SK-05/06/07: índice barato, cuerpo al activarse, activación y versión auditadas."""
    provider = FakeProvider([
        {"type": "tool_call", "tool": "skills.load", "args": {"name": "data-analysis"}},
        {"type": "final", "message": "ok"},
    ])
    res = await run_session(SessionOptions(task="analiza"), cfg, provider, store=store,
                            sandbox_factory=lambda: fake_sandbox)
    before, after = provider.requests[0].render(), provider.requests[1].render()
    assert "- data-analysis:" in before and "df.dtypes" not in before
    assert "## Skill activa: data-analysis" in after and "df.dtypes" in after
    act = store.events(res.session_id, ["skill_activation"])
    assert act and act[0].skill == "data-analysis" and "@" in act[0].version
    started = store.events(res.session_id, ["session"])[0]
    assert started.skills_versions["data-analysis"] == act[0].version


async def test_profile_skill_filter(root, store, fake_sandbox):
    from argos.config import load_config

    cfg = load_config(root, {"data_dir": str(root / "var")})
    cfg.profiles["personal"].skills = ["sandbox-*"]
    provider = FakeProvider([{"type": "final", "message": "ok"}])
    await run_session(SessionOptions(task="t"), cfg, provider, store=store,
                      sandbox_factory=lambda: fake_sandbox)
    prompt = provider.requests[0].render()
    assert "sandbox-troubleshooting" in prompt and "data-analysis" not in prompt
