"""One explicit local configuration, independent of the agent's working directory."""
import os
from pathlib import Path

from .models import ToolchainConfig
from .storage import read_json


def load_runtime(root=None, config_path=None):
    workspace = Path(root or os.environ.get("PCB_WEAVER_WORKSPACE", Path.home() / "PCBWeaverData"))
    config_file = config_path or os.environ.get("PCB_WEAVER_CONFIG")
    config = read_json(Path(config_file)) if config_file else {}
    config = ToolchainConfig.model_validate(config).model_dump(exclude_none=True)
    if config.get("freerouting_jar") and config_file:
        jar = Path(config["freerouting_jar"])
        if not jar.is_absolute() and not (config.get("wsl_distro") and config["freerouting_jar"].startswith("/")):
            config["freerouting_jar"] = str((Path(config_file).resolve().parent / jar).resolve())
    if config_file:
        for key in ("java", "kicad_cli", "kicad_python"):
            value = config.get(key)
            if not value:
                continue
            if config.get("wsl_distro") and value.startswith("/"):
                continue
            executable = Path(value)
            if not executable.is_absolute() and (len(executable.parts) > 1 or value.startswith(("./", ".\\"))):
                config[key] = str((Path(config_file).resolve().parent / executable).resolve())
    return workspace, config
