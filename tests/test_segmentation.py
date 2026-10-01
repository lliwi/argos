"""Aislamiento entre segmentos en el despliegue (RF-SEC-02, RF-SEC-03, P2, ADR-0008).

Comprueba el compose.yaml real: quién ve `secrets/` y `var/`, y que ningún segmento comparte red
de salida con otro. Complementa la verificación en vivo documentada en el ADR.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
SEGMENT = {
    "core": "main",
    "daemon": "main",
    "matrix": "main",
    "core-osint": "osint",
    "core-pentest": "pentest",
}
NEEDS_SECRETS = {"core", "daemon"}  # inventario y SOPS: solo el núcleo de main


def _mounts(name: str) -> tuple[set[str], set[str]]:
    binds, tmpfs = set(), set()
    for v in SERVICES[name].get("volumes", []):
        if isinstance(v, dict) and v.get("type") == "tmpfs":
            tmpfs.add(v["target"])
        elif isinstance(v, str):
            binds.add(v.split(":")[1] if ":" in v else v)
    return binds, tmpfs


def test_secrets_only_visible_to_main_core():
    for name in SEGMENT:
        binds, tmpfs = _mounts(name)
        assert "${ARGOS_DIR}" in binds, name  # repo en solo lectura
        hidden = "${ARGOS_DIR}/secrets" in tmpfs
        assert hidden == (name not in NEEDS_SECRETS), f"{name}: secrets/ mal expuesto"


def test_each_core_only_sees_its_segment_data():
    for name, seg in SEGMENT.items():
        binds, tmpfs = _mounts(name)
        assert "${ARGOS_DIR}/var" in tmpfs, f"{name}: var/ de otros segmentos visible"
        data = {b for b in binds if b.startswith("${ARGOS_DIR}/var/")}
        assert data and all(b.endswith(f"/{seg}") for b in data), (name, data)


def test_no_network_shared_between_segments():
    nets_by_seg: dict[str, set[str]] = {}
    for name, seg in SEGMENT.items():
        nets_by_seg.setdefault(seg, set()).update(SERVICES[name].get("networks") or [])
    for seg in ("main", "osint", "pentest"):
        nets_by_seg[seg].update(SERVICES[f"egress-{seg}"].get("networks") or [])
    segs = list(nets_by_seg)
    for i, a in enumerate(segs):
        for b in segs[i + 1 :]:
            assert not nets_by_seg[a] & nets_by_seg[b], f"{a} y {b} comparten red"


def test_tokens_live_under_secrets():
    assert SERVICES["matrix"]["env_file"][0]["path"] == "secrets/matrix.env"
    assert SERVICES["daemon"]["env_file"][0]["path"] == "secrets/hooks.env"
    assert "docker.sock" not in str([SERVICES[n].get("volumes") for n in SEGMENT])
