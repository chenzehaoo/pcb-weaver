"""Auditable KiCad 8-10 subprocess adapters; no EDA engines are emulated here."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


def windows_to_wsl(path: str | Path) -> str:
    """Convert drive-absolute paths, preserving spaces as part of one argv entry."""
    text = str(path)
    win = PureWindowsPath(text)
    if win.drive and win.root:
        if len(win.drive) != 2 or win.drive[1] != ":":
            raise ValueError("WSL conversion requires a drive path, not a UNC path")
        parts = []
        for part in win.parts[1:]:
            if part == "..":
                if parts:
                    parts.pop()
            elif part != ".":
                parts.append(part)
        return "/mnt/" + win.drive[0].lower() + "/" + "/".join(parts)
    if win.drive or text.startswith("\\"):
        raise ValueError("Ambiguous Windows path; supply a drive-absolute path")
    return text


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def _legacy_fanout_rules(dsn: Path, output: Path) -> dict:
    """Derive a complete 1.9 routing-only override for canonical native KiCad DSN."""
    import sexpdata

    text = dsn.read_text(encoding="utf-8")
    # Specctra declares its quote character using a token not legal in Lisp.
    # Normalize only that declaration in memory; never rewrite the input DSN.
    normalized, count = re.subn(r'\(string_quote "\)', '(string_quote "double_quote")', text)
    if count != 1:
        raise ValueError("Fanout requires the canonical KiCad double-quote declaration")
    try:
        tree = sexpdata.loads(normalized)
    except Exception as exc:
        raise ValueError("Invalid DSN syntax for fanout settings") from exc
    def children(node, name):
        return [child for child in node if isinstance(child, list) and child and child[0] == sexpdata.Symbol(name)]
    def one(node, name):
        values = children(node, name)
        if len(values) != 1:
            raise ValueError("Fanout requires exactly one DSN " + name)
        return values[0]
    if not isinstance(tree, list) or not tree or tree[0] != sexpdata.Symbol("pcb"):
        raise ValueError("Fanout requires a native PCB DSN")
    parser = one(tree, "parser")
    if one(parser, "host_cad")[1:] != ["KiCad's Pcbnew"] or children(parser, "generated_by_freerouting"):
        raise ValueError("Fanout supports only native KiCad-generated DSN")
    structure = one(tree, "structure")
    if children(structure, "autoroute_settings"):
        raise ValueError("Fanout will not replace preexisting autoroute_settings")
    layers = children(structure, "layer")
    names = [str(layer[1]) for layer in layers]
    if len(layers) not in (2, 4, 6, 8) or len(set(names)) != len(names) or any(
            one(layer, "type")[1:] != [sexpdata.Symbol("signal")] for layer in layers):
        raise ValueError("Fanout requires a native 2/4/6/8 all-signal layer stack")
    resolution = one(tree, "resolution")
    if len(resolution) != 3 or str(resolution[1]) != "um" or type(resolution[2]) is not int or resolution[2] <= 0:
        raise ValueError("Unsupported DSN resolution for exact fanout defaults")
    boundary = one(structure, "boundary")
    if len(boundary) != 2 or not isinstance(boundary[1], list):
        raise ValueError("Fanout requires one explicit PCB boundary")
    shape = boundary[1]
    if len(shape) < 2 or str(shape[1]) != "pcb":
        raise ValueError("Fanout requires the PCB boundary")
    if str(shape[0]) == "path" and len(shape) >= 9 and shape[2] == 0:
        coordinates = shape[3:]
    elif str(shape[0]) == "rect" and len(shape) == 6:
        coordinates = shape[2:]
    else:
        raise ValueError("Fanout boundary must be a zero-width polygon path or rectangle")
    if len(coordinates) % 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in coordinates):
        raise ValueError("Invalid fanout boundary coordinates")
    bounds = [min(coordinates[::2]), min(coordinates[1::2]), max(coordinates[::2]), max(coordinates[1::2])]
    scale = resolution[2]
    if max(map(abs, bounds)) == 0:
        raise ValueError("Degenerate fanout boundary")
    while 5 * max(map(abs, bounds)) * scale >= 33554432:
        scale //= 10
    if scale <= 0:
        raise ValueError("Fanout boundary exceeds the verified native coordinate range")
    rounded = [math.floor(value * scale + 0.5) for value in bounds]
    width, height = rounded[2] - rounded[0] + 2000, rounded[3] - rounded[1] + 2000
    if rounded[2] <= rounded[0] or rounded[3] <= rounded[1]:
        raise ValueError("Degenerate fanout boundary")
    settings = {"fanout": True, "autoroute": True, "postroute": True, "vias": True,
                "via_costs": 50, "plane_via_costs": 5, "start_ripup_costs": 100, "start_pass_no": 1}
    layer_rules = []
    horizontal = width < height
    for index, name in enumerate(names):
        horizontal = not horizontal
        outer_cost = 0.2 * len(names) if len(names) > 2 and index in (0, len(names) - 1) else 0
        ratio = width / height if horizontal else height / width
        layer_rules.append({"name": name, "active": True,
            "preferred_direction": "horizontal" if horizontal else "vertical",
            "preferred_direction_trace_costs": 1 + outer_cost,
            "against_preferred_direction_trace_costs": 1 + outer_cost + 0.1 * math.floor(10 * ratio + 0.5)})
    scope = [sexpdata.Symbol("autoroute_settings")]
    for key, value in settings.items():
        scope.append([sexpdata.Symbol(key), sexpdata.Symbol("on" if value else "off") if isinstance(value, bool) else value])
    for layer in layer_rules:
        scope.append([sexpdata.Symbol("layer_rule"), layer["name"],
            *[[sexpdata.Symbol(key), sexpdata.Symbol("on") if key == "active" else
               sexpdata.Symbol(value) if key == "preferred_direction" else value]
              for key, value in layer.items() if key != "name"]])
    settings["layer_rules"] = layer_rules
    contents = sexpdata.dumps([sexpdata.Symbol("rules"), sexpdata.Symbol("PCB"), "input", scope]) + "\n"
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)
    return {"settings": settings, "settings_sha256": hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "rules_path": str(output), "rules_sha256": _sha256(output), "input_dsn_sha256": _sha256(dsn),
        "native_coordinate_scale": scale, "native_padded_bounds": [rounded[0] - 1000, rounded[1] - 1000, rounded[2] + 1000, rounded[3] + 1000],
        "physical_rule_overrides": False, "default_changes": ["fanout"],
        "derivation_source": "https://github.com/freerouting/freerouting/blob/v1.9.0/src/main/java/app/freerouting/interactive/AutorouteSettings.java"}


def _validate_gerbers(directory: Path, copper_count: int) -> dict:
    jobs = list(directory.glob("*.gbrjob"))
    if len(jobs) != 1 or not _nonempty(jobs[0]):
        raise ValueError("Exactly one nonempty KiCad Gerber job is required")
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    if job.get("GeneralSpecs", {}).get("LayerNumber") != copper_count:
        raise ValueError("Gerber job copper layer count differs from native board")
    required = {"Profile", "Legend,Top", "Legend,Bot", "SolderMask,Top", "SolderMask,Bot",
                "SolderPaste,Top", "SolderPaste,Bot"}
    required.update("Copper,L{},{}".format(i, "Top" if i == 1 else "Bot" if i == copper_count else "Inr")
                    for i in range(1, copper_count + 1))
    found, paths = set(), set()
    for entry in job.get("FilesAttributes", []):
        path = (directory / entry["Path"]).resolve()
        if not path.is_relative_to(directory.resolve()) or path.suffix != ".gbr" or not _nonempty(path):
            raise ValueError("Gerber job references a missing, empty or unsafe artifact")
        function = entry["FileFunction"]
        if function in found:
            raise ValueError("Duplicate Gerber file function: " + function)
        contents = path.read_text(encoding="ascii")
        header = re.search(r"%TF\.FileFunction,([^*]+)\*%", contents)
        # KiCad's job vocabulary differs from Gerber X2 for paste and mask.
        head, *tail = function.split(",")
        expected_header = ",".join([{"SolderPaste": "Paste", "SolderMask": "Soldermask"}.get(head, head), *tail])
        if not header or not (header[1] == expected_header or header[1].startswith(expected_header + ",")) or "M02*" not in contents:
            raise ValueError("Gerber header/completion does not match job: " + path.name)
        found.add(function)
        paths.add(path)
    if found != required or paths != {p.resolve() for p in directory.glob("*.gbr")}:
        raise ValueError("Manufacturing Gerber layer set is incomplete or unexpected")
    return {"job_path": str(jobs[0]), "sha256": _sha256(jobs[0]), "file_functions": sorted(found)}


def _parse_check(report: dict, kind: str) -> dict:
    """Validate the report structure before counting; an empty object is not a pass."""
    if not isinstance(report, dict):
        raise ValueError("Report must be a JSON object")
    for field in ("source", "date", "kicad_version"):
        if not isinstance(report.get(field), str) or not report[field]:
            raise ValueError(f"Report is missing {field}")
    if report.get("$schema", f"https://schemas.kicad.org/{kind}.v1.json") != (
        f"https://schemas.kicad.org/{kind}.v1.json"
    ):
        raise ValueError("Unsupported check report schema")

    def rows(container: dict, name: str) -> list:
        value = container.get(name)
        if not isinstance(value, list):
            raise ValueError(f"Report is missing the {name} array")
        return value

    unconnected = []
    if kind == "drc":
        unconnected = rows(report, "unconnected_items")
        entries = rows(report, "violations") + unconnected + rows(report, "schematic_parity")
    else:
        entries = []
        for sheet in rows(report, "sheets"):
            if not isinstance(sheet, dict):
                raise ValueError("Invalid ERC sheet")
            entries.extend(rows(sheet, "violations"))
    counts = {"errors": 0, "warnings": 0, "excluded": 0}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("severity") not in (
            "error", "warning", "exclusion", "ignore", "info"
        ):
            raise ValueError("Invalid check violation severity")
        if "excluded" in entry and not isinstance(entry["excluded"], bool):
            raise ValueError("Invalid exclusion flag")
        if entry.get("excluded") or entry["severity"] == "exclusion":
            counts["excluded"] += 1
        elif entry["severity"] == "error":
            counts["errors"] += 1
        elif entry["severity"] == "warning":
            counts["warnings"] += 1
    counts["unconnected"] = sum(
        not row.get("excluded", False) and row["severity"] not in ("exclusion", "ignore")
        for row in unconnected
    )
    return {
        **counts,
        "electrical_pass": counts["errors"] == 0 and counts["unconnected"] == 0,
        "report_version": report["kicad_version"],
        "included_severities": report.get("included_severities"),
        "ignored_checks": report.get("ignored_checks"),
    }


class Toolchain:
    """Paths for artifacts are host paths, shared into WSL through /mnt/<drive>."""

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self.distro = self.config.get("wsl_distro")
        if self.distro is not None and (
            not isinstance(self.distro, str) or not self.distro or self.distro.startswith("-")
        ):
            raise ValueError("wsl_distro must be a nonempty distribution name")
        self.timeout = self.config.get("timeout_seconds", 300)
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 0 < self.timeout < float("inf"):
            raise ValueError("timeout_seconds must be finite and positive")
        self.route_timeout = self.config.get("route_timeout_seconds", self.config.get("timeout_seconds", 1800))
        if isinstance(self.route_timeout, bool) or not isinstance(self.route_timeout, (int, float)) or not 0 < self.route_timeout < float("inf"):
            raise ValueError("route_timeout_seconds must be finite and positive")
        self.cli = self.config.get("kicad_cli") or "kicad-cli"
        self.python = self.config.get("kicad_python") or ("python3" if self.distro else sys.executable)
        self.java = self.config.get("java") or "java"
        self.jar = self.config.get("freerouting_jar")
        self.controlled_neckdown = self.config.get("controlled_neckdown", False)
        if not isinstance(self.controlled_neckdown, bool):
            raise ValueError("controlled_neckdown must be a boolean")
        self.fanout = self.config.get("fanout", False)
        self.optimizer_passes = self.config.get("router_optimizer_max_passes")
        if self.optimizer_passes is None:
            self.optimizer_passes = 100
        if type(self.optimizer_passes) is not int or not 1 <= self.optimizer_passes <= 100:
            raise ValueError("router_optimizer_max_passes must be an integer from 1 to 100")
        if not isinstance(self.fanout, bool):
            raise ValueError("fanout must be a boolean")
        self.router_edge = self.config.get("router_copper_to_edge_clearance_mm")
        if self.router_edge is not None and (isinstance(self.router_edge, bool) or
                not isinstance(self.router_edge, (int, float)) or not 0 <= self.router_edge <= 20):
            raise ValueError("router_copper_to_edge_clearance_mm must be finite and between 0 and 20")
        self.bridge = Path(__file__).with_name("native_bridge.py")

    def _path(self, path: str | Path) -> str:
        return windows_to_wsl(path) if self.distro else str(path)

    def _argv(self, executable: str, *args: str) -> list[str]:
        command = [self._path(executable), *map(str, args)]
        return ["wsl.exe", "-d", self.distro, "--", *command] if self.distro else command

    def _execute(self, executable: str, args: list[str], version: str | None = None, cwd: Path | None = None,
                 timeout_seconds: float | None = None) -> dict:
        timeout = self.timeout if timeout_seconds is None else timeout_seconds
        kill_grace = min(30.0, max(1.0, timeout * 0.1))
        host_timeout = timeout + kill_grace + 10 if self.distro else timeout
        argv = self._argv(executable, *args)
        if self.distro:
            # Also bound the Linux process lifetime; killing only wsl.exe can leave
            # an engine running and writing files after the host has timed out.
            argv = self._argv("timeout", f"--kill-after={kill_grace:g}s",
                              f"{timeout:g}s", self._path(executable), *args)
        started = time.time()
        result = {"argv": argv, "version": version, "started_at": started, "timeout_seconds": timeout,
                  "stdin": "DEVNULL",
                  "engine_timeout_seconds": timeout, "host_timeout_seconds": host_timeout,
                  "cwd": str(cwd) if cwd is not None else str(Path.cwd())}
        try:
            completed = subprocess.run(
                argv, shell=False, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=host_timeout, cwd=cwd, stdin=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
            result.update(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
            result["status"] = "ok" if completed.returncode == 0 else "failed"
            if completed.returncode:
                result["reason"] = f"Command exited with code {completed.returncode}"
                if self.distro and completed.returncode in (124, 137):
                    result.update(reason="WSL command timed out", timed_out=True)
                if completed.returncode == 127 or "not found" in completed.stderr.lower() or "no such file" in completed.stderr.lower():
                    result["status"] = "blocked"
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            result.update(status="failed", reason="Command timed out", returncode=None,
                          stdout=decoded(exc.stdout), stderr=decoded(exc.stderr), timed_out=True)
        except OSError as exc:
            result.update(status="blocked", reason=str(exc), returncode=None, stdout="", stderr=str(exc))
        result["elapsed_seconds"] = time.time() - started
        return result

    def _cli_version(self) -> dict:
        probe = self._execute(self.cli, ["version"])
        if probe["status"] == "ok":
            match = re.search(r"\b(\d+)\.\d+(?:\.\d+)?", probe["stdout"])
            if match:
                probe["version"] = match[0]
            if not match or int(match[1]) not in (8, 9, 10):
                probe.update(status="blocked", reason="This adapter supports KiCad CLI major versions 8-10")
        return probe

    def _cli_help(self, command: list[str], required: tuple[str, ...], version: str) -> dict:
        probe = self._execute(self.cli, [*command, "--help"], version)
        if probe["status"] == "ok":
            help_text = probe["stdout"] + probe["stderr"]
            missing = [flag for flag in required if flag not in help_text]
            if missing:
                probe.update(status="blocked", reason="CLI capability probe missing: " + ", ".join(missing))
        return probe

    def _native(self, operation: str, *paths: Path) -> dict:
        if not self.bridge.is_file():
            return {"status": "blocked", "reason": "Bundled native_bridge.py is missing", "argv": [], "version": None}
        process = self._execute(self.python, [self._path(self.bridge.resolve()), operation, *[self._path(p) for p in paths]])
        messages = [line[len("PCB_WEAVER_RESULT="):] for line in process.get("stdout", "").splitlines()
                    if line.startswith("PCB_WEAVER_RESULT=")]
        if messages:
            try:
                payload = json.loads(messages[-1])
                if not isinstance(payload, dict) or payload.get("status") not in ("ok", "blocked", "failed"):
                    raise ValueError("Invalid native bridge response")
                process["native"] = payload
                process["version"] = payload.get("version")
                if payload["status"] != "ok":
                    process.update(status=payload["status"], reason=payload.get("reason", "Native operation failed"))
            except (ValueError, TypeError) as exc:
                process.update(status="failed", reason=f"Invalid native bridge response: {exc}")
        elif process["status"] == "ok":
            process.update(status="failed", reason="Native helper did not return a result")
        return process

    def _finish(self, result: dict, commands: list[dict], log_dir: Path | None = None) -> dict:
        result["commands"] = commands
        result.setdefault("argv", commands[-1].get("argv", []) if commands else [])
        result.setdefault("version", commands[-1].get("version") if commands else None)
        if log_dir is not None:
            try:
                directory = log_dir / ".toolchain-logs"
                directory.mkdir(parents=True, exist_ok=True)
                log = directory / f"{uuid4().hex}.json"
                result["log_path"] = str(log)
                with log.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, indent=2)
            except OSError as exc:
                result.update(status="failed", reason=f"Could not persist operation log: {exc}")
        return result

    def _prepare(self, sources: list[Path], output: Path, suffix: str | None = None) -> dict | None:
        try:
            for source in sources:
                if _same_path(source, output):
                    return {"status": "blocked", "reason": "Source and destination must differ"}
                if not _nonempty(source):
                    return {"status": "blocked", "reason": f"Missing or empty input: {source}"}
            if output.exists() or output.is_symlink():
                return {"status": "blocked", "reason": f"Destination already exists: {output}"}
            if suffix and output.suffix.lower() != suffix:
                return {"status": "blocked", "reason": f"Output must use the {suffix} extension"}
            output.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            return {"status": "blocked", "reason": str(exc)}
        return None

    def doctor(self) -> dict:
        cli = self._cli_version()
        native = self._native("doctor")
        java = self._execute(self.java, ["-version"])
        java["version"] = self._java_version(java)
        jar = self._jar_info()
        tools = {"kicad_cli": cli, "pcbnew": native, "java": java, "freerouting": jar}
        if self.fanout and jar.get("version") not in ("1.9.0", "2.4.1"):
            tools["fanout_profile"] = {"status": "blocked", "argv": [], "version": jar.get("version"),
                                       "reason": "Fanout is supported only by the verified Freerouting 1.9.0 profile"}
        if jar["status"] == "ok" and jar.get("version") == "1.9.0":
            tools["legacy_display"] = self._legacy_display_probe()
        if jar["status"] == "ok" and jar.get("version") == "2.4.1":
            issue = self._unified_profile_issue(jar, java)
            tools["unified_profile"] = issue or self._unified_probe()
        return self._finish({
            "status": "ok" if all(t["status"] == "ok" for t in tools.values()) else "blocked",
            "tools": tools, "wsl_distro": self.distro,
            "capabilities": {"drc": cli["status"] == "ok", "erc": cli["status"] == "ok",
                             "copper_layer_counts": [2, 4, 6, 8], "high_speed": False,
                             "existing_copper": "Requires exact geometry verification on every SES import",
                             "native": native.get("native", {}).get("capabilities", {})},
        }, list(tools.values()))

    def _legacy_display_probe(self) -> dict:
        if not self.controlled_neckdown or (os.name == "nt" and not self.distro):
            return {"status": "blocked", "argv": [], "version": "1.9.0",
                    "reason": "Freerouting 1.9.0 requires controlled_neckdown=true and Linux/WSL Xvfb; no visible-window fallback"}
        working = None
        try:
            working = Path(tempfile.mkdtemp(prefix="pcb-display-probe-"))
            jar_path = str(self.jar) if self.distro and str(self.jar).startswith("/") else str(Path(self.jar).resolve())
            # The 1.9 entrypoint initializes AWT before handling -help. A wrapper
            # --help alone cannot detect missing xauth, X11 or libawt_xawt.so.
            process = self._execute("xvfb-run", ["-a",
                "--server-args=-screen 0 1280x1024x24 -nolisten tcp", self._path(self.java),
                "-Djava.awt.headless=false", "-Djava.io.tmpdir=.", "-jar",
                self._path(jar_path), "-da", "-help"], "1.9.0", cwd=working,
                timeout_seconds=min(self.timeout, 30))
            if process["status"] == "ok" and not all(flag in process.get("stdout", "")
                    for flag in ("-de", "-do", "-mp", "-mt")):
                process.update(status="blocked", reason="Legacy JAR did not return its expected CLI help")
            result = {"status": "ok" if process["status"] == "ok" else "blocked",
                      "display_backend": "xvfb", "working_dir": str(working)}
            if result["status"] != "ok":
                result["reason"] = "Legacy display probe failed; Xvfb, xauth and a full AWT JRE are required: " + process.get("reason", "unknown failure")
            return self._finish(result, [process], working)
        except OSError as exc:
            return {"status": "blocked", "argv": [], "version": "1.9.0",
                    "working_dir": str(working) if working else None, "reason": str(exc)}

    @staticmethod
    def _java_version(process: dict) -> str | None:
        text = process.get("stderr", "") + "\n" + process.get("stdout", "")
        match = re.search(r'(?:openjdk|java) (?:version\s+)?"?([\d][\d._+\-]*)', text)
        return match[1] if match else None

    def _unified_profile_issue(self, jar: dict, java: dict) -> dict | None:
        reason = None
        if jar.get("native", {}).get("sha256") != "251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9":
            reason = "The 2.4.1 profile requires the pinned official JAR SHA256"
        elif not re.match(r"(?:2[5-9]|[3-9]\d)\.", java.get("version") or ""):
            reason = "Freerouting 2.4.1 requires Java 25 or newer"
        elif os.name == "nt" and not self.distro:
            reason = "The isolated 2.4.1 profile currently requires Linux/WSL"
        elif self.router_edge is None:
            reason = "2.4.1 requires explicit router_copper_to_edge_clearance_mm matching the KiCad project rule"
        return {"status": "blocked", "reason": reason, "argv": [], "version": "2.4.1"} if reason else None

    def _unified_command(self, working: Path) -> list[str]:
        jar = str(self.jar) if self.distro and str(self.jar).startswith("/") else str(Path(self.jar).resolve())
        # A clean Linux environment and a unique settings root prevent inherited
        # FREEROUTING/JAVA options and previous GUI settings from changing a job.
        return ["-i", "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8",
                "HOME=" + self._path(working), self._path(self.java),
                "-Djava.awt.headless=true", "-Djava.io.tmpdir=.", "-Duser.language=en", "-jar", self._path(jar),
                "--user_data_path=" + self._path(working / "user-data"),
                "--gui.enabled=false", "--api_server.enabled=false", "--mcp_server.enabled=false",
                "--mcp_server.stdio=false", "--profile.allow_telemetry=false", "--profile.allow_contact=false",
                "--feature_flags.multi_threading=false", "-da", "--logging.console.level=INFO"]

    def _unified_probe(self) -> dict:
        working = Path(tempfile.mkdtemp(prefix="pcb-unified-probe-"))
        process = self._execute("env", [*self._unified_command(working), "-help"], "2.4.1",
                                cwd=working, timeout_seconds=min(self.timeout, 30))
        output = process.get("stdout", "") + process.get("stderr", "")
        if process["status"] == "ok" and (not all(flag in output for flag in ("-de", "-do", "-mp", "-mt")) or
                re.search(r"Unknown command line argument|Failed to apply", output)):
            process.update(status="blocked", reason="2.4.1 CLI help/settings probe failed")
        return self._finish({"status": process["status"], "working_dir": str(working),
                             "reason": process.get("reason"), "router_settings_runtime_verified": False,
                             "version_check_network_disabled": False}, [process], working)

    def _route_unified(self, dsn: Path, ses: Path, passes: int, jar: dict, java: dict) -> dict:
        issue = self._unified_profile_issue(jar, java)
        if issue:
            return self._finish(issue, [jar, java], ses.parent)
        working = None
        commands = [jar, java]
        source_hash = _sha256(dsn)
        result = {"status": "failed", "version": "2.4.1", "cli_profile": "2.4.1-unified",
                  "electrical_pass": False, "output_path": str(ses), "dsn_sha256": source_hash,
                  "jar_sha256": jar["native"]["sha256"], "java_version": java["version"],
                  "fanout": self.fanout, "automatic_neckdown": self.controlled_neckdown,
                  "requires_native_width_check_and_drc": True, "requested_passes": passes,
                  "pass_limit_enforced": True, "routing_threads": 1, "display_backend": "headless",
                  "determinism_guaranteed": False, "telemetry_disabled": True,
                  "api_server_disabled": True, "mcp_server_disabled": True,
                  "version_check_network_disabled": False,
                  "profile_source_revision": "ae3d377740b6ffa744bed1bab26625fe0278fa90",
                  "capability_evidence": "Tagged source and CLI probe; effective router settings require result JSON"}
        try:
            working = Path(tempfile.mkdtemp(prefix="pcb-route-241-", dir=ses.parent))
            result.update(working_dir=str(working), working_directory=str(working))
            shutil.copy2(dsn, working / "input.dsn")
            manifest_path = working / "router-result.json"
            settings = {
                "enabled": True, "algorithm": "freerouting-router", "max_passes": passes,
                "max_threads": 1, "automatic_neckdown": self.controlled_neckdown,
                "neck_width_um": 0.0, "strict_drc": True,
                "copper_to_edge_clearance_um": self.router_edge * 1000,
                "job_timeout": str(max(1, int(self.route_timeout) - 30)) + "s",
                "fanout.enabled": self.fanout, "fanout.max_passes": 20,
                "fanout.max_milliseconds_per_pin": 10000,
                "fanout.fallback_to_board_vias": False,
                "optimizer.enabled": True, "optimizer.max_threads": 1,
                "optimizer.max_passes": self.optimizer_passes, "optimizer.improvement_threshold": 0.01,
                "optimizer.item_selection_strategy": "SEQUENTIAL",
                "result_json": self._path(manifest_path),
            }
            result["requested_settings"] = settings
            audit_path = working / "requested-settings.json"
            audit_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            result.update(settings_path=str(audit_path), settings_sha256=_sha256(audit_path))
            flags = [f"--router.{key}={str(value).lower() if isinstance(value, bool) else value}"
                     for key, value in settings.items()]
            process = self._execute("env", [*self._unified_command(working), "-de", "input.dsn", "-do",
                                    self._path(ses), *flags], "2.4.1", cwd=working,
                                    timeout_seconds=self.route_timeout)
            commands.append(process)
            result["status"] = process["status"]
            if process["status"] != "ok":
                result["reason"] = process.get("reason", "Router process failed")
                result["timed_out"] = process.get("timed_out", False)
            if _nonempty(ses):
                result["ses_sha256"] = _sha256(ses)
            if not _nonempty(manifest_path):
                raise ValueError("Freerouting 2.4.1 did not produce its required result JSON")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result.update(router_result_path=str(manifest_path), router_result_sha256=_sha256(manifest_path),
                          router_result=manifest)
            if (manifest.get("schema_version") != 1 or manifest.get("app_version") != "2.4.1" or
                    manifest.get("fixture", {}).get("sha256") != source_hash):
                raise ValueError("Router result identity/schema does not match this input and version")
            if manifest.get("final_state") == "TIMED_OUT":
                result.update(timed_out=True, status="failed", reason="Freerouting internal job timeout; SES retained as evidence")
            elif manifest.get("final_state") != "COMPLETED":
                raise ValueError("Router did not reach COMPLETED state")
            if manifest.get("output_written") is not True or manifest.get("exit_code") != 0 or not _nonempty(ses):
                raise ValueError("Router did not report and produce a nonempty SES")
            snapshot = manifest.get("settings_snapshot", {})
            # Gson intentionally omits transient optimizer selection/result-path
            # fields. Verify every other requested scalar against engine readback.
            for key, expected in settings.items():
                if key in ("optimizer.item_selection_strategy", "result_json"):
                    continue
                actual = snapshot
                for part in key.split("."):
                    actual = actual.get(part) if isinstance(actual, dict) else None
                if actual != expected or (isinstance(expected, bool) and type(actual) is not bool):
                    raise ValueError(f"Router effective setting mismatch: {key}: {actual!r} != {expected!r}")
            result["effective_settings_verified"] = True
            if _sha256(dsn) != source_hash or _sha256(working / "input.dsn") != source_hash:
                raise ValueError("Router changed original or staged DSN")
            if re.search(r"Unknown command line argument|Failed to apply CLI router setting",
                         process.get("stdout", "") + process.get("stderr", "")):
                raise ValueError("Router rejected a requested setting")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            result.update(status="failed", validation_error=str(exc))
            result.setdefault("reason", str(exc))
        return self._finish(result, commands, ses.parent)

    def _run_check(self, source: Path, output: Path, kind: str) -> dict:
        source, output = Path(source).resolve(), Path(output).resolve()
        base = {"errors": 0, "warnings": 0, "unconnected": 0, "electrical_pass": False,
                "report_path": str(output), "report_valid": False}
        issue = self._prepare([source], output, ".json")
        if issue:
            return self._finish({**base, **issue}, [])
        probe = self._cli_version()
        commands = [probe]
        if probe["status"] != "ok":
            return self._finish({**base, "status": probe["status"], "reason": probe["reason"]}, commands, output.parent)
        capability = self._cli_help(["pcb" if kind == "drc" else "sch", kind],
                                    ("--format", "--output", "--exit-code-violations", "--severity-error", "--severity-warning", "--severity-exclusions"),
                                    probe["version"])
        commands.append(capability)
        if capability["status"] != "ok":
            return self._finish({**base, "status": capability["status"], "reason": capability["reason"]}, commands, output.parent)
        args = ["pcb" if kind == "drc" else "sch", kind, "--format", "json",
                "--severity-error", "--severity-warning", "--severity-exclusions", "--exit-code-violations",
                "--output", self._path(output)]
        parity = kind == "drc" and _nonempty(source.with_suffix(".kicad_sch"))
        if kind == "drc":
            help_text = capability["stdout"] + capability["stderr"]
            required = ["--all-track-errors"] + (["--schematic-parity"] if parity else [])
            if any(flag not in help_text for flag in required):
                return self._finish({**base, "status": "blocked", "reason": "CLI lacks required DRC/parity capabilities"}, commands, output.parent)
            args.append("--all-track-errors")
            if parity:
                args.append("--schematic-parity")
        args.append(self._path(source))
        process = self._execute(self.cli, args, probe["version"])
        commands.append(process)
        if process.get("returncode") not in (0, 5):
            return self._finish({**base, "status": process["status"], "reason": process["reason"]}, commands, output.parent)
        try:
            report = json.loads(output.read_text(encoding="utf-8-sig"))
            parsed = _parse_check(report, kind)
            if parsed["report_version"].split(".")[0] != probe["version"].split(".")[0]:
                raise ValueError("Report version does not match CLI version")
            result = {**base, **parsed, "status": "ok", "report_valid": True,
                      "report_sha256": _sha256(output), "source_sha256": _sha256(source),
                      "returncode": process["returncode"], "process_success": True,
                      "schematic_parity_checked": parity,
                      "zones_refilled": False}  # Refill explicitly via fill_zones before checking.
            if process["returncode"] == 5 and parsed["electrical_pass"] and not parsed["warnings"] and not parsed["excluded"]:
                raise ValueError("Exit code reports violations but JSON contains none")
        except (OSError, ValueError, TypeError) as exc:
            result = {**base, "status": "failed", "reason": f"Invalid or missing {kind.upper()} JSON: {exc}"}
        return self._finish(result, commands, output.parent)

    def run_drc(self, board: Path, output: Path) -> dict:
        return self._run_check(board, output, "drc")

    def run_erc(self, schematic: Path, output: Path) -> dict:
        return self._run_check(schematic, output, "erc")

    def _native_export(self, operation: str, sources: list[Path], output: Path, suffix: str) -> dict:
        sources, output = [Path(p).resolve() for p in sources], Path(output).resolve()
        issue = self._prepare(sources, output, suffix)
        if issue:
            return self._finish(issue, [])
        hashes = {str(p): _sha256(p) for p in sources}
        process = self._native(operation, *sources, output)
        result = {"status": process["status"], "output_path": str(output), "input_hashes": hashes}
        for key in ("copper_layers", "copper_preservation", "placement_preservation", "project_rules", "track_width_audit", "high_speed_supported"):
            if key in process.get("native", {}):
                result[key] = process["native"][key]
        if process["status"] != "ok":
            result["reason"] = process["reason"]
        elif not _nonempty(output):
            result.update(status="failed", reason="Native engine produced no nonempty artifact")
        elif any(_sha256(p) != hashes[str(p)] for p in sources):
            result.update(status="failed", reason="Native operation changed an input")
        else:
            result["sha256"] = _sha256(output)
        return self._finish(result, [process], output.parent)

    def export_dsn(self, board: Path, output: Path) -> dict:
        return self._native_export("export-dsn", [board], output, ".dsn")

    def inspect_board(self, board: Path) -> dict:
        board = Path(board).resolve()
        if not _nonempty(board):
            return self._finish({"status": "blocked", "reason": "Missing board"}, [])
        process = self._native("inspect", board)
        return self._finish({**process.get("native", {}), "status": process["status"],
                             **({"reason": process["reason"]} if "reason" in process else {})}, [process])

    def import_ses(self, board: Path, ses: Path, output: Path) -> dict:
        if self.controlled_neckdown:
            try:
                project = json.loads(Path(board).with_suffix(".kicad_pro").read_text(encoding="utf-8"))
                floor = project["board"]["design_settings"]["rules"]["min_track_width"]
                if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 < floor < float("inf"):
                    raise ValueError("Missing positive manufacturing track minimum")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return self._finish({"status": "blocked", "reason": "Controlled neckdown requires an explicit project track minimum: " + str(exc)}, [])
        return self._native_export("import-ses", [board, ses], output, ".kicad_pcb")

    def fill_zones(self, board: Path, output: Path) -> dict:
        return self._native_export("fill-zones", [board], output, ".kicad_pcb")

    def _jar_info(self) -> dict:
        if not self.jar:
            return {"status": "blocked", "reason": "freerouting_jar is not configured", "argv": [], "version": None}
        # A stdlib-only bridge command also works when the JAR lives inside WSL.
        return self._native("jar-info", Path(self.jar) if self.distro and str(self.jar).startswith("/") else Path(self.jar).resolve())

    def route(self, dsn: Path, ses: Path, passes: int = 10, nets=None) -> dict:
        if nets is not None:
            return self._finish({"status": "blocked", "reason": "Target-net isolation is not runtime-verified on Freerouting 2.4.1; native acceptance observed non-target routing. No scoped routing job started."}, [])
        dsn, ses = Path(dsn).resolve(), Path(ses).resolve()
        if isinstance(passes, bool) or not isinstance(passes, int) or not 1 <= passes <= 9999:
            return self._finish({"status": "blocked", "reason": "passes must be an integer from 1 to 9999"}, [])
        issue = self._prepare([dsn], ses, ".ses")
        if issue:
            return self._finish(issue, [])
        jar = self._jar_info()
        if jar["status"] != "ok":
            return self._finish({"status": jar["status"], "reason": jar["reason"]}, [jar], ses.parent)
        java = self._execute(self.java, ["-version"])
        java["version"] = self._java_version(java)
        if java["status"] != "ok":
            return self._finish({"status": java["status"], "reason": java["reason"]}, [jar, java], ses.parent)
        required_java = jar.get("native", {}).get("minimum_java")
        java_match = re.match(r'(\d+)(?:\.(\d+))?', java["version"] or "")
        java_major = int(java_match[2]) if java_match and java_match[1] == "1" and java_match[2] else int(java_match[1]) if java_match else None
        if required_java and (java_major is None or java_major < required_java):
            return self._finish({"status": "blocked", "reason": f"This JAR requires Java {required_java}; detected {java_major}",
                                 "java_version": java["version"]}, [jar, java], ses.parent)
        if jar.get("version") == "2.4.1":
            return self._route_unified(dsn, ses, passes, jar, java)
        if self.config.get("router_optimizer_max_passes") is not None:
            return self._finish({"status": "blocked", "reason": "Optimizer pass control requires the verified Freerouting 2.4.1 profile"}, [jar, java], ses.parent)
        jar_path = str(self.jar) if self.distro and str(self.jar).startswith("/") else str(Path(self.jar).resolve())
        family = re.match(r"(?:v)?(2\.[012])\.", jar.get("version") or "")
        legacy = jar.get("version") == "1.9.0"
        if self.fanout and not legacy:
            return self._finish({"status": "blocked", "reason": "Fanout is supported only by the verified Freerouting 1.9.0 profile"}, [jar, java], ses.parent)
        if not family and not legacy:
            return self._finish({"status": "blocked", "reason": "No verified CLI profile for this Freerouting version"}, [jar, java], ses.parent)
        fixed_profile = jar.get("version") == "2.0.1"
        probes = [jar, java]
        if self.controlled_neckdown and not (fixed_profile or legacy):
            return self._finish({"status": "blocked", "reason": "Controlled neckdown is verified only for Freerouting 1.9.0 and 2.0.1"}, probes, ses.parent)
        if legacy:
            if not self.controlled_neckdown or (os.name == "nt" and not self.distro):
                return self._finish({"status": "blocked", "reason": "Freerouting 1.9.0 requires controlled_neckdown=true and Linux/WSL Xvfb; no visible-window fallback"}, probes, ses.parent)
            display = self._execute("xvfb-run", ["--help"])
            probes.append(display)
            if display["status"] != "ok":
                return self._finish({"status": "blocked", "reason": "Freerouting 1.9.0 requires installed xvfb-run and xauth"}, probes, ses.parent)
            executable = "xvfb-run"
            args = ["-a", "--server-args=-screen 0 1280x1024x24 -nolisten tcp", self._path(self.java),
                    "-Djava.awt.headless=false", "-Djava.io.tmpdir=.", "-jar", self._path(jar_path),
                    "-da", "-de", "input.dsn", "-do", self._path(ses), "-mp", str(passes),
                    "-mt", "1", "-is", "seq", "-dct", "0", "-oit", "1"]
        else:
            executable = self.java
            args = ["-Djava.awt.headless=true", "-Djava.io.tmpdir=.", "-jar", self._path(jar_path), "--gui.enabled=false",
                    "--api_server.enabled=false", "--router.automatic_neckdown=" + str(self.controlled_neckdown).lower(),
                    "-da", "-de", "input.dsn", "-do", self._path(ses), "-mp", str(passes), "-mt", "1", "-is", "seq"]
            if fixed_profile:
                # The 2.0.1 headless loop reads stop_pass_no, not maxPasses (-mp).
                args.append(f"--router.stop_pass_no={passes}")
        source_hash = _sha256(dsn)
        # Preserve staging as audit evidence. WSL can retain a directory handle
        # briefly after exit, so synchronous Windows cleanup can mask the result.
        working = None
        fanout_audit = None
        try:
            working = Path(tempfile.mkdtemp(prefix="pcb-route-", dir=ses.parent))
            shutil.copy2(dsn, working / "input.dsn")
            if self.fanout:
                fanout_audit = _legacy_fanout_rules(working / "input.dsn", working / "input.rules")
                args.extend(["-dr", self._path(Path(fanout_audit["rules_path"]))])
            process = self._execute(executable, args, jar.get("version"), cwd=working, timeout_seconds=self.route_timeout)
        except (OSError, ValueError, TypeError, IndexError) as exc:
            return self._finish({"status": "failed", "reason": f"Cannot stage routing input: {exc}",
                                 "working_directory": str(working) if working else None}, probes, ses.parent)
        result = {"status": process["status"], "output_path": str(ses), "dsn_sha256": source_hash,
                  "working_dir": str(working), "working_directory": str(working),
                  "routing_threads": 1, "item_selection": "sequential", "determinism_guaranteed": False,
                  "automatic_neckdown": self.controlled_neckdown,
                  "fanout": self.fanout, "fanout_audit": fanout_audit,
                  "pass_limit_enforced": fixed_profile or legacy, "requested_passes": passes,
                  "display_backend": "xvfb" if legacy else "headless",
                  "neckdown_control": "legacy_native_default" if legacy else "cli_flag",
                  "optimizer_improvement_threshold_percent": 1 if legacy else None,
                  "requires_native_width_check_and_drc": self.controlled_neckdown,
                  "java_version": java["version"], "jar_sha256": jar.get("native", {}).get("sha256"),
                  "cli_profile": "1.9.0-xvfb" if legacy else family[1], "capability_evidence": "Reviewed upstream version profile; execution result is recorded separately",
                  "electrical_pass": False}
        if process["status"] != "ok":
            result["reason"] = process["reason"]
        elif not _nonempty(ses):
            result.update(status="failed", reason="Freerouting produced no nonempty SES")
        elif _sha256(dsn) != source_hash:
            result.update(status="failed", reason="Freerouting changed the input DSN")
        else:
            result["ses_sha256"] = _sha256(ses)
        if fanout_audit is not None:
            matches = re.findall(r"fanout pass:\s*(\d+), routed:\s*(\d+), not routed:\s*(\d+), errors:\s*(\d+)", process.get("stdout", ""))
            fanout_audit["passes"] = [dict(zip(("pass", "routed", "not_routed", "errors"), map(int, row))) for row in matches]
            fanout_audit["execution_observed"] = bool(matches)
            # The official 1.9 release suppresses BatchFanout's test-level pass
            # log. Audit the explicit rules load; native phase-only integration
            # separately verifies effective settings and generated copper.
            fanout_audit["rules_load_observed"] = (
                "Opening '" + self._path(Path(fanout_audit["rules_path"])) + "'" in process.get("stdout", "") and
                not re.search(r"RulesFile\.read:|AutorouteSettings\.read", process.get("stdout", "") + process.get("stderr", "")))
            fanout_audit["input_and_rules_unchanged"] = (
                _sha256(working / "input.dsn") == source_hash and
                _sha256(Path(fanout_audit["rules_path"])) == fanout_audit["rules_sha256"])
            if result["status"] == "ok" and (not fanout_audit["rules_load_observed"] or not fanout_audit["input_and_rules_unchanged"]):
                result.update(status="failed", reason="Fanout rules load was not observed or staged input/settings changed")
        return self._finish(result, [*probes, process], ses.parent)

    def export_netlist(self, schematic: Path, output: Path) -> dict:
        return self._cli_export(Path(schematic), Path(output), ["sch", "export", "netlist", "--format", "kicadxml"])

    def _cli_export(self, source: Path, output: Path, args: list[str]) -> dict:
        source, output = source.resolve(), output.resolve()
        issue = self._prepare([source], output)
        if issue:
            return self._finish(issue, [])
        probe = self._cli_version()
        if probe["status"] != "ok":
            return self._finish({"status": probe["status"], "reason": probe["reason"]}, [probe], output.parent)
        capability = self._cli_help(args[:3], tuple(arg for arg in args if arg.startswith("--")) + ("--output",), probe["version"])
        if capability["status"] != "ok":
            return self._finish({"status": capability["status"], "reason": capability["reason"]}, [probe, capability], output.parent)
        process = self._execute(self.cli, [*args, "--output", self._path(output), self._path(source)], probe["version"])
        result = {"status": process["status"], "output_path": str(output)}
        if process["status"] != "ok":
            result["reason"] = process["reason"]
        elif not _nonempty(output):
            result.update(status="failed", reason="CLI produced no nonempty artifact")
        else:
            result["sha256"] = _sha256(output)
        return self._finish(result, [probe, capability, process], output.parent)

    def export_manufacturing(self, board: Path, output_dir: Path) -> dict:
        board, output_dir = Path(board).resolve(), Path(output_dir).resolve()
        if not _nonempty(board):
            return self._finish({"status": "blocked", "reason": f"Missing board: {board}"}, [])
        inspected = self.inspect_board(board)
        if inspected["status"] != "ok":
            return inspected
        copper_layers = inspected.get("standard_copper_layers", inspected["copper_layers"])
        requested_layers = [*copper_layers, "F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
        if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
            return self._finish({"status": "blocked", "reason": "Manufacturing destination must be empty"}, [])
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._finish({"status": "blocked", "reason": str(exc)}, [])
        probe = self._cli_version()
        commands = [*inspected["commands"], probe]
        if probe["status"] != "ok":
            return self._finish({"status": probe["status"], "reason": probe["reason"]}, commands, output_dir)
        operations = [
            ("gerber", ["pcb", "export", "gerbers", "--no-protel-ext", "--layers", ",".join(requested_layers)], output_dir / "gerber", "*.gbr"),
            ("drill", ["pcb", "export", "drill", "--format", "excellon", "--drill-origin", "absolute"], output_dir / "drill", "*.drl"),
            ("pos", ["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp"], output_dir / "positions.csv", None),
        ]
        schematic = board.with_suffix(".kicad_sch")
        if _nonempty(schematic):
            operations.append(("bom", ["sch", "export", "bom", "--exclude-dnp"], output_dir / "bom.csv", None))
        artifacts = []
        for kind, args, target, pattern in operations:
            capability = self._cli_help(args[:3], tuple(arg for arg in args if arg.startswith("--")) + ("--output",), probe["version"])
            commands.append(capability)
            if capability["status"] != "ok":
                return self._finish({"status": capability["status"], "reason": capability["reason"], "artifacts": artifacts}, commands, output_dir)
            if pattern:
                target.mkdir()
            source = schematic if kind == "bom" else board
            process = self._execute(self.cli, [*args, "--output", self._path(target), self._path(source)], probe["version"])
            commands.append(process)
            if process["status"] != "ok":
                return self._finish({"status": process["status"], "reason": process["reason"], "artifacts": artifacts}, commands, output_dir)
            files = sorted(target.glob(pattern)) if pattern else [target]
            if not files or not all(_nonempty(path) for path in files):
                return self._finish({"status": "failed", "reason": f"No nonempty {kind} output", "artifacts": artifacts}, commands, output_dir)
            if kind == "gerber":
                try:
                    gerber_validation = _validate_gerbers(target, len(copper_layers))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    return self._finish({"status": "failed", "reason": str(exc), "artifacts": artifacts}, commands, output_dir)
                artifacts.append({"kind": "gerber_job", "path": gerber_validation["job_path"], "sha256": gerber_validation["sha256"]})
            artifacts.extend({"kind": kind, "path": str(path), "sha256": _sha256(path)} for path in files)
        return self._finish({"status": "ok", "output_dir": str(output_dir), "artifacts": artifacts,
                             "copper_layers": copper_layers, "gerber_validation": gerber_validation,
                             "bom_status": "ok" if _nonempty(schematic) else "blocked",
                             "bom_reason": None if _nonempty(schematic) else "Matching schematic is required for a schematic BOM",
                             "zones_refilled": False, "electrical_pass": False}, commands, output_dir)
