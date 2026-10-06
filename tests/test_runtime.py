import json

import pytest

from pcb_weaver.models import ToolchainConfig
from pcb_weaver.runtime import load_runtime


def test_router_execution_options_survive_runtime_validation(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"timeout_seconds": 300, "route_timeout_seconds": 900,
                               "controlled_neckdown": True}), encoding="utf-8")
    _, config = load_runtime(str(tmp_path / "data"), str(path))
    assert config["route_timeout_seconds"] == 900
    assert config["controlled_neckdown"] is True


@pytest.mark.parametrize("value", [0, -1, 7201, True, "900"])
def test_router_timeout_rejects_unsafe_config(value):
    with pytest.raises(ValueError):
        ToolchainConfig(route_timeout_seconds=value)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_neckdown_requires_explicit_boolean(value):
    with pytest.raises(ValueError):
        ToolchainConfig(controlled_neckdown=value)


@pytest.mark.parametrize("key", ["java", "kicad_cli", "kicad_python"])
def test_explicit_relative_executable_is_config_relative(tmp_path, monkeypatch, key):
    config_file = tmp_path / "tools.json"
    config_file.write_text(json.dumps({key: "tools/runtime/bin/engine"}), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _, config = load_runtime(str(tmp_path / "data"), str(config_file))
    assert config[key] == str(tmp_path / "tools/runtime/bin/engine")


def test_path_commands_and_wsl_absolute_paths_keep_their_meaning(tmp_path):
    config_file = tmp_path / "tools.json"
    config_file.write_text(json.dumps({"wsl_distro": "Ubuntu", "java": "/opt/jre/bin/java",
        "kicad_cli": "kicad-cli", "kicad_python": "python3", "freerouting_jar": "/opt/router.jar"}), encoding="utf-8")
    _, config = load_runtime(str(tmp_path / "data"), str(config_file))
    assert config["java"] == "/opt/jre/bin/java"
    assert config["freerouting_jar"] == "/opt/router.jar"
    assert config["kicad_cli"] == "kicad-cli"
    assert config["kicad_python"] == "python3"
