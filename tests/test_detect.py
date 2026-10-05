import json
import sys

import pytest
import yaml

import detect

FACTS = {"cores": 32, "ram_gb": 128, "disk_free_gb": 2000, "gpu_vram_gb": 0, "virtualization": True}


def run(tmp_path, monkeypatch, override):
    (tmp_path / "profiles").mkdir(exist_ok=True)
    (tmp_path / "profiles" / "std.yaml").write_text(yaml.safe_dump({
        "clusters": 3, "servers_per_cluster": 1, "workers_per_cluster": 2, "server_vcpu": 2, "server_ram_gb": 4,
        "mgmt_vm": {"ram_gb": 8, "disk_gb": 40},
    }))
    if override is not None:
        (tmp_path / "profile.override.yaml").write_text(yaml.safe_dump(override))
    out = tmp_path / "out.json"
    monkeypatch.setattr(detect, "ROOT", str(tmp_path))
    monkeypatch.setattr(detect, "collect", lambda _p: FACTS)
    monkeypatch.setattr(detect, "select_profile", lambda _f: "std")
    monkeypatch.setattr(sys, "argv", ["detect.py", "--out", str(out)])
    detect.main()
    return json.loads(out.read_text())


def test_override_changes_sizing(tmp_path, monkeypatch):
    base = run(tmp_path, monkeypatch, None)
    assert len(base["nodes"]) == 9
    small = run(tmp_path, monkeypatch, {"clusters": 1, "workers_per_cluster": 1})
    assert len(small["nodes"]) == 2
    assert {n["cluster"] for n in small["nodes"].values()} == {"hub"}


def test_override_that_does_not_fit_exits(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        run(tmp_path, monkeypatch, {"workers_per_cluster": 50})


def test_top_level_pins_still_win(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, {"clusters": 1, "host_reserve_ram_gb": 20})
    assert out["host_reserve_ram_gb"] == 20 and out["clusters"] == 1
