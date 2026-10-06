"""Generate this installation's MCP configuration without editing global clients."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="toolchain.unified.json")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config = (root / args.config).resolve(strict=True)
    target = root / ".mcp.json"
    settings = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"mcpServers": {}}
    if not isinstance(settings, dict) or not isinstance(settings.get("mcpServers"), dict):
        raise ValueError("Invalid existing MCP configuration")
    settings["mcpServers"]["pcb-weaver"] = {
        "command": sys.executable,
        "args": ["-m", "pcb_weaver.server"],
        "env": {"PCB_WEAVER_WORKSPACE": str(root / "data"), "PCB_WEAVER_CONFIG": str(config)}
    }
    target.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    print(json.dumps({"mcp_config": str(root / ".mcp.json"), "python": sys.executable}, indent=2))


if __name__ == "__main__":
    main()
