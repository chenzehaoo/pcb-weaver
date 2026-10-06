"""Adapter tests use explicit fakes; opt-in integration tests run real tools."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from pcb_weaver import native_bridge
from pcb_weaver.toolchain import Toolchain, _legacy_fanout_rules, _parse_check, windows_to_wsl


CLI_HELP = "--format --output --layers --exit-code-violations --severity-error --severity-warning --severity-exclusions --all-track-errors --schematic-parity --no-protel-ext --drill-origin --units --side --exclude-dnp"


def empty_native_board():
    return SimpleNamespace(BuildConnectivity=lambda: None, Zones=lambda: [], GetFootprints=lambda: [],
        GetTracks=lambda: [], GetCopperLayerCount=lambda: 2, GetLayerName=lambda i: ["F.Cu", "B.Cu"][i],
        GetEnabledLayers=lambda: SimpleNamespace(CuStack=lambda: [0, 1]))


def fake_inspection(monkeypatch):
    monkeypatch.setattr(Toolchain, "inspect_board", lambda self, board: {
        "status": "ok", "copper_layers": ["F.Cu", "B.Cu"], "commands": []})


def fake_gerbers(output, copper=2):
    functions = ["Profile", "Legend,Top", "Legend,Bot", "SolderMask,Top", "SolderMask,Bot",
                 "SolderPaste,Top", "SolderPaste,Bot"]
    functions += [f"Copper,L{i}," + ("Top" if i == 1 else "Bot" if i == copper else "Inr")
                  for i in range(1, copper + 1)]
    entries = []
    for i, function in enumerate(functions):
        name = f"layer-{i}.gbr"
        header_function = function.replace("SolderPaste", "Paste").replace("SolderMask", "Soldermask")
        (output / name).write_text(f"%TF.FileFunction,{header_function}*%\nM02*\n")
        entries.append({"Path": name, "FileFunction": function})
    (output / "board-job.gbrjob").write_text(json.dumps({"GeneralSpecs": {"LayerNumber": copper}, "FilesAttributes": entries}))


def report(kind="drc", version="8.0.9", **updates):
    value = {"$schema": f"https://schemas.kicad.org/{kind}.v1.json", "source": "board.kicad_pcb",
             "date": "2026-09-07T00:00:00Z", "kicad_version": version, "coordinate_units": "mm"}
    if kind == "drc":
        value.update(violations=[], unconnected_items=[], schematic_parity=[])
    else:
        value["sheets"] = [{"path": "/", "violations": []}]
    return {**value, **updates}


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_text("unit-test placeholder, not a KiCad board", encoding="utf-8")
    return path


def install_cli_fake(monkeypatch, version="8.0.9", payload=None, code=0, artifact=True):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] > 0
        if argv[-1] == "version":
            return subprocess.CompletedProcess(argv, 0, version + "\n", "")
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, CLI_HELP, "")
        if "--output" in argv and artifact:
            output = Path(argv[argv.index("--output") + 1])
            output.write_text(json.dumps(payload) if payload is not None else "artifact", encoding="utf-8")
        return subprocess.CompletedProcess(argv, code, "test stdout", "test stderr")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize("distro", [None, "Ubuntu"])
def test_engine_never_inherits_mcp_stdin(monkeypatch, distro):
    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert Toolchain({"wsl_distro": distro})._execute("java", ["-version"])["status"] == "ok"


def test_local_engine_stdin_is_eof():
    result = Toolchain({"timeout_seconds": 10})._execute(sys.executable,
        ["-c", "import sys; print(repr(sys.stdin.buffer.read(1)))"])
    assert result["status"] == "ok" and result["stdout"].strip() == "b''"


@pytest.mark.skipif(not os.environ.get("PCB_WEAVER_TEST_WSL"), reason="Opt-in real WSL stdin isolation")
def test_real_wsl_engine_stdin_is_eof(tmp_path):
    tc = Toolchain({"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"], "timeout_seconds": 10})
    process = tc._execute("python3", ["-c", "import sys; print(repr(sys.stdin.buffer.read(1)))"])
    result = tc._finish(dict(process), [process], tmp_path)
    assert result["status"] == "ok" and result["stdout"].strip() == "b''", result
    assert result["stdin"] == "DEVNULL"


@pytest.mark.parametrize("version", ["8.0.9", "9.0.8", "10.0.1"])
def test_drc_real_json_contract_across_versions(monkeypatch, board, version):
    calls = install_cli_fake(monkeypatch, version, report(version=version))
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "ok" and result["electrical_pass"]
    assert result["report_valid"] and result["version"] == version
    argv = calls[-1][0]
    assert argv[1:5] == ["pcb", "drc", "--format", "json"]
    assert "--refill-zones" not in argv and "--save-board" not in argv
    assert "--severity-error" in argv and "--severity-warning" in argv
    assert "--severity-exclusions" in argv
    assert Path(result["log_path"]).is_file()
    assert json.loads(Path(result["log_path"]).read_text())["argv"] == argv


def test_exit_five_is_completed_check_not_electrical_pass(monkeypatch, board):
    data = report(violations=[{"severity": "error"}], unconnected_items=[{"severity": "warning"}])
    install_cli_fake(monkeypatch, payload=data, code=5)
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "ok"
    assert (result["errors"], result["warnings"], result["unconnected"]) == (1, 1, 1)
    assert not result["electrical_pass"]


@pytest.mark.parametrize("payload", [{}, [], {"violations": []}, report(violations=[{}]), report(unconnected_items=None)])
def test_invalid_reports_never_pass(monkeypatch, board, payload):
    install_cli_fake(monkeypatch, payload=payload)
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "failed" and not result["electrical_pass"]
    assert not result["report_valid"]


def test_missing_report_never_passes(monkeypatch, board):
    install_cli_fake(monkeypatch, artifact=False)
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "failed"


def test_zero_exit_with_error_json_is_not_pass(monkeypatch, board):
    install_cli_fake(monkeypatch, payload=report(violations=[{"severity": "error"}]))
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "ok" and not result["electrical_pass"]


def test_erc_counts_all_sheets_and_exclusions(monkeypatch, board):
    data = report("erc", sheets=[{"violations": [{"severity": "error"}]},
                                  {"violations": [{"severity": "warning"}, {"severity": "error", "excluded": True}]}])
    install_cli_fake(monkeypatch, payload=data, code=5)
    result = Toolchain().run_erc(board, board.with_suffix(".json"))
    assert result["argv"][1:3] == ["sch", "erc"]
    assert (result["errors"], result["warnings"], result["excluded"]) == (1, 1, 1)


@pytest.mark.parametrize("matching", [False, True])
def test_parity_only_for_matching_schematic(monkeypatch, board, matching):
    if matching:
        board.with_suffix(".kicad_sch").write_text("schematic fixture")
    else:
        board.with_name("different.kicad_sch").write_text("other schematic")
    install_cli_fake(monkeypatch, payload=report())
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "ok"
    assert ("--schematic-parity" in result["argv"]) == matching
    assert result["schematic_parity_checked"] == matching


@pytest.mark.parametrize("version", ["7.0.11", "11.0.0", "unknown"])
def test_unsupported_cli_fails_explicitly(monkeypatch, board, version):
    calls = install_cli_fake(monkeypatch, version)
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "blocked" and len(calls) == 1


def test_missing_cli_capability_blocks_before_check(monkeypatch, board):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "9.0.1" if argv[-1] == "version" else "no JSON support", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = Toolchain().run_drc(board, board.with_suffix(".json"))
    assert result["status"] == "blocked" and "capability" in result["reason"]


def test_source_and_stale_output_rejected_before_launch(monkeypatch, board):
    calls = install_cli_fake(monkeypatch)
    toolchain = Toolchain()
    assert toolchain.export_dsn(board, board)["status"] == "blocked"
    output = board.with_suffix(".json")
    output.write_text(json.dumps(report()))
    assert toolchain.run_drc(board, output)["status"] == "blocked"
    assert not calls


def test_hardlink_source_alias_rejected(monkeypatch, board):
    alias = board.with_name("alias.kicad_pcb")
    os.link(board, alias)
    assert Toolchain().fill_zones(board, alias)["status"] == "blocked"


@pytest.mark.parametrize("error,status", [(FileNotFoundError("missing"), "blocked"),
                                         (subprocess.TimeoutExpired(["tool"], 1, b"partial"), "failed")])
def test_subprocess_failures_are_audited(monkeypatch, error, status):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", run)
    result = Toolchain()._execute("missing", ["a;b", "$(test)"])
    assert result["status"] == status and result["argv"] == ["missing", "a;b", "$(test)"]


@pytest.mark.parametrize("source,expected", [(r"C:\PCB Work\board.kicad_pcb", "/mnt/c/PCB Work/board.kicad_pcb"),
                                            ("D:/a/../b/file", "/mnt/d/b/file"), ("/usr/bin/python3", "/usr/bin/python3")])
def test_windows_path_conversion(source, expected):
    assert windows_to_wsl(source) == expected


@pytest.mark.parametrize("path", [r"\\server\share\file", r"C:relative", r"\relative"])
def test_ambiguous_wsl_paths_rejected(path):
    with pytest.raises(ValueError):
        windows_to_wsl(path)


def test_wsl_argv_is_array_with_spaces_preserved(monkeypatch):
    calls = install_cli_fake(monkeypatch)
    tc = Toolchain({"wsl_distro": "Ubuntu", "kicad_cli": "/usr/bin/kicad-cli"})
    tc._execute(tc.cli, ["pcb", "drc", tc._path(r"C:\A B\board.kicad_pcb")])
    assert calls[-1][0] == ["wsl.exe", "-d", "Ubuntu", "--", "timeout", "--kill-after=30s", "300s", "/usr/bin/kicad-cli", "pcb", "drc", "/mnt/c/A B/board.kicad_pcb"]


def test_linux_timeout_is_not_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 124, "partial", ""))
    result = Toolchain({"wsl_distro": "Ubuntu"})._execute("java", ["-version"])
    assert result["status"] == "failed" and result["timed_out"]


def test_java_version_excludes_wsl_warning():
    assert Toolchain._java_version({"stderr": 'w\x00s\x00l\x00 warning\nopenjdk version "21.0.12" 2026-07-21'}) == "21.0.12"


def test_netlist_uses_kicadxml_and_requires_output(monkeypatch, board):
    calls = install_cli_fake(monkeypatch)
    result = Toolchain().export_netlist(board, board.with_suffix(".xml"))
    assert result["status"] == "ok" and result["sha256"]
    assert calls[-1][0][1:6] == ["sch", "export", "netlist", "--format", "kicadxml"]


def test_missing_native_helper_blocks(board):
    tc = Toolchain()
    tc.bridge = board.parent / "missing.py"
    assert tc.export_dsn(board, board.with_suffix(".dsn"))["status"] == "blocked"


def test_native_module_absence_is_explicit(monkeypatch):
    def missing(name):
        raise ImportError("pcbnew not installed")
    monkeypatch.setattr(native_bridge.importlib, "import_module", missing)
    result = native_bridge.perform("doctor", [])
    assert result["status"] == "blocked" and "pcbnew" in result["reason"]


@pytest.mark.parametrize("operation", ["export-dsn", "import-ses", "fill-zones"])
def test_bridge_calls_native_engine_and_preserves_sidecars(monkeypatch, board, operation):
    calls = []
    token = empty_native_board()
    def export(brd, output):
        assert brd is token
        calls.append("export")
        Path(output).write_text("native DSN")
        return True
    def save(output, brd, skip):
        assert brd is token and skip is True
        assert Path(output).with_suffix(".kicad_pro").read_text() == '{"net_settings":{}}'
        calls.append("save")
        Path(output).write_text("native board")
        return True
    fake = SimpleNamespace(GetBuildVersion=lambda: "8.0.9", LoadBoard=lambda p: token,
                           FromMM=lambda v: round(v * 1000000), ToMM=lambda v: v / 1000000,
                           ExportSpecctraDSN=export, ImportSpecctraSES=lambda b, s: calls.append("import") or True,
                           SaveBoard=save, ZONE_FILLER=lambda b: SimpleNamespace(Fill=lambda zones: calls.append("fill") or True))
    monkeypatch.setattr(native_bridge.importlib, "import_module", lambda name: fake)
    board.with_suffix(".kicad_pro").write_text('{"net_settings":{}}')
    board.with_suffix(".kicad_dru").write_text("(version 1)")
    ses = board.with_suffix(".ses")
    ses.write_text("(session test (placement (resolution um 10)))")
    output = board.with_name("child.dsn" if operation == "export-dsn" else "child.kicad_pcb")
    paths = [board, ses, output] if operation == "import-ses" else [board, output]
    result = native_bridge.perform(operation, paths)
    assert result["status"] == "ok", result
    assert board.read_text() == "unit-test placeholder, not a KiCad board"
    if operation != "export-dsn":
        assert output.with_suffix(".kicad_dru").read_text() == "(version 1)"
    assert {"export-dsn": "export", "import-ses": "import", "fill-zones": "fill"}[operation] in calls


def test_conflicting_sidecar_never_overwritten(board):
    board.with_suffix(".kicad_pro").write_text("source rules")
    output = board.with_name("child.kicad_pcb")
    output.with_suffix(".kicad_pro").write_text("different rules")
    with pytest.raises(ValueError, match="Conflicting"):
        native_bridge._copy_sidecars(board, output)
    assert output.with_suffix(".kicad_pro").read_text() == "different rules"


@pytest.mark.parametrize("actual,status", [(400000, "ok"), (200000, "blocked")])
def test_declared_netclass_must_match_native_values(board, actual, status):
    board.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": {
        "classes": [{"name": "Default", "track_width": 0.4}]}}))
    native_board = SimpleNamespace(SynchronizeNetsAndNetClasses=lambda reset: None,
                                  GetAllNetClasses=lambda: {"Default": SimpleNamespace(GetTrackWidth=lambda: actual)})
    api = SimpleNamespace(FromMM=lambda v: round(v * 1000000), ToMM=lambda v: v / 1000000)
    if status == "blocked":
        with pytest.raises(ValueError, match="was not applied"):
            native_bridge._verify_project_netclasses(api, native_board, board)
    else:
        result = native_bridge._verify_project_netclasses(api, native_board, board)
        assert result["netclasses_verified"] and result["classes"]["Default"]["track_width"] == 0.4


@pytest.mark.parametrize("existing,expected", [(None, 3), ({"version": 2, "custom": "kept"}, 2)])
def test_compiler_supplies_missing_net_settings_schema(board, existing, expected):
    from pcb_weaver.compiler import compile_rules
    section = {} if existing is None else {"meta": existing}
    project = board.with_suffix(".kicad_pro")
    project.write_text(json.dumps({"net_settings": section}))
    compile_rules(board, {"fabrication": {"min_track_mm": 0.4, "min_clearance_mm": 0.3,
                                         "min_via_drill_mm": 0.4}, "net_rules": []})
    settings = json.loads(project.read_text())["net_settings"]
    assert settings["meta"]["version"] == expected
    if existing:
        assert settings["meta"] == existing
    assert settings["classes"][0]["track_width"] == 0.4


@pytest.mark.parametrize("resolution,valid", [("um 10", True), ("mm 10000", True),
                                              ("mm 1", False), ("um 0", False), ("bad 10", False)])
def test_ses_resolution_is_declared_and_bounded(tmp_path, resolution, valid):
    session = tmp_path / "input.ses"
    session.write_text('(session "(resolution mm 1)" (placement (resolution ' + resolution + ')))')
    if valid:
        assert native_bridge._session_placement_resolution_mm(session) == 0.0001
    else:
        with pytest.raises(ValueError):
            native_bridge._session_placement_resolution_mm(session)


@pytest.mark.parametrize("change", ["quantization", "large_move", "rotation", "layer", "net", "uuid"])
def test_ses_restores_exact_pose_only_for_bounded_quantization(change):
    state = {"x": 42108683, "y": 35000001, "angle": 12.345678, "layer": 0, "net": "GND", "uuid": "fp"}
    vector = lambda x, y: SimpleNamespace(x=x, y=y)
    pad = SimpleNamespace(m_Uuid=SimpleNamespace(AsString=lambda: "pad"), GetNumber=lambda: "1",
                          GetNetname=lambda: state["net"], GetPosition=lambda: vector(state["x"], state["y"]))
    def move(position):
        state.update(x=position.x, y=position.y)
    footprint = SimpleNamespace(m_Uuid=SimpleNamespace(AsString=lambda: state["uuid"]),
        GetReference=lambda: "C1", GetPosition=lambda: vector(state["x"], state["y"]),
        GetOrientationDegrees=lambda: state["angle"], GetLayer=lambda: state["layer"], Pads=lambda: [pad],
        SetPosition=move, SetOrientationDegrees=lambda angle: state.update(angle=angle))
    board = SimpleNamespace(GetFootprints=lambda: [footprint], BuildConnectivity=lambda: None)
    api = SimpleNamespace(FromMM=lambda v: round(v * 1000000), ToMM=lambda v: v / 1000000, VECTOR2I=vector)
    original = native_bridge._placement_snapshot(board)
    state.update(x=42108700, y=35000000)
    if change == "large_move":
        state["x"] += 1000
    elif change == "rotation":
        state["angle"] += 0.01
    elif change == "layer":
        state["layer"] = 31
    elif change == "net":
        state["net"] = "VCC"
    elif change == "uuid":
        state["uuid"] = "different"
    if change == "quantization":
        result = native_bridge._restore_session_placements(api, board, original, 0.0001)
        assert result["restored_exactly"] and result["max_axis_drift_mm"] == 0.000017
        assert native_bridge._placement_snapshot(board) == original
    else:
        with pytest.raises(ValueError):
            native_bridge._restore_session_placements(api, board, original, 0.0001)


def test_jar_identity_from_manifest_without_pcbnew(tmp_path):
    jar = tmp_path / "any-name.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: app.freerouting.Freerouting\nImplementation-Version: 2.1.0\n")
        archive.writestr("app/freerouting/Freerouting.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x41")
    result = native_bridge.perform("jar-info", [jar])
    assert result["status"] == "ok" and result["version"] == "2.1.0" and result["sha256"]
    assert result["minimum_java"] == 21


def test_jar_newer_than_java_blocks_before_routing(monkeypatch, board):
    tc = Toolchain({"freerouting_jar": "example.jar"})
    monkeypatch.setattr(tc, "_jar_info", lambda: {"status": "ok", "version": "2.2.4", "argv": [], "native": {"minimum_java": 25}})
    monkeypatch.setattr(tc, "_execute", lambda *args, **kwargs: {"status": "ok", "argv": ["java", "-version"], "stderr": 'openjdk version "21.0.8"'})
    result = tc.route(board, board.with_suffix(".ses"))
    assert result["status"] == "blocked" and "Java 25" in result["reason"]


def test_unspecified_manifest_uses_verified_hash_not_filename(monkeypatch, tmp_path):
    jar = tmp_path / "freerouting-99.0.0.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: app.freerouting.Freerouting\nImplementation-Version: unspecified\nBuiltRevision: test-revision\n")
        archive.writestr("app/freerouting/Freerouting.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x41")
    assert native_bridge.perform("jar-info", [jar])["status"] == "blocked"
    monkeypatch.setattr(native_bridge, "PINNED_RELEASES", {native_bridge._hash(jar): "2.0.1"})
    result = native_bridge.perform("jar-info", [jar])
    assert result["status"] == "ok" and result["version"] == "2.0.1"
    assert result["version_source"] == "pinned_release_sha256"
    assert result["manifest_version"] == "unspecified"
    assert result["build_revision"] == "test-revision"


@pytest.mark.parametrize("value", [True, "0.5", -0.1, 21, float("nan"), float("inf")])
def test_unified_edge_setting_is_strict(value):
    from pcb_weaver.models import ToolchainConfig
    with pytest.raises(ValueError):
        ToolchainConfig(router_copper_to_edge_clearance_mm=value)
    with pytest.raises(ValueError):
        Toolchain({"router_copper_to_edge_clearance_mm": value})


def unified_test_toolchain(monkeypatch):
    tc = Toolchain({"wsl_distro": "Ubuntu", "java": "/isolated/jre25/bin/java",
                    "freerouting_jar": "/official/freerouting.jar", "fanout": True,
                    "controlled_neckdown": True, "router_copper_to_edge_clearance_mm": 0.5})
    digest = next(key for key, value in native_bridge.PINNED_RELEASES.items() if value == "2.4.1")
    monkeypatch.setattr(tc, "_jar_info", lambda: {"status": "ok", "version": "2.4.1", "argv": [],
                        "native": {"sha256": digest, "minimum_java": 25}})
    return tc


def test_optimizer_budget_is_not_silently_ignored_by_other_engines(monkeypatch, tmp_path):
    tc = Toolchain({"router_optimizer_max_passes": 1, "freerouting_jar": "fixture.jar"})
    monkeypatch.setattr(tc, "_jar_info", lambda: {"status": "ok", "version": "1.9.0"})
    monkeypatch.setattr(tc, "_execute", lambda *a, **kw: {"status": "ok", "stderr": 'openjdk version "25.0.4"'})
    dsn = tmp_path / "input.dsn"
    dsn.write_text("test fixture")
    result = tc.route(dsn, tmp_path / "output.ses")
    assert result["status"] == "blocked"
    assert "2.4.1" in result["reason"]


@pytest.mark.parametrize("failure", [None, "timeout", "missing-json", "empty-ses", "wrong-input",
                                     "settings", "rejected-flag", "changed-staged", "process"])
@pytest.mark.parametrize("optimizer_passes", [None, 1])
def test_unified_profile_requires_real_result_and_effective_settings(monkeypatch, tmp_path, failure, optimizer_passes):
    tc = unified_test_toolchain(monkeypatch)
    if optimizer_passes is not None:
        tc.optimizer_passes = optimizer_passes
    dsn = tmp_path / "source.dsn"
    dsn.write_text("Explicit simulated DSN fixture")
    original = dsn.read_bytes()
    ses = tmp_path / "output.ses"

    def execute(executable, args, version=None, cwd=None, **kwargs):
        if args == ["-version"]:
            return {"status": "ok", "stderr": 'openjdk version "25.0.4"', "argv": []}
        assert executable == "env" and args[0] == "-i"
        assert "--api_server.enabled=false" in args and "--mcp_server.enabled=false" in args
        assert "--profile.allow_telemetry=false" in args and "-da" in args
        assert "--logging.console.level=INFO" in args
        assert "--logging.console.level=DEBUG" not in args
        assert "-dr" not in args and "-is" not in args and "--router.stop_pass_no=10" not in args
        assert "--router.neck_width_um=0.0" in args and "--router.strict_drc=true" in args
        assert "--router.fanout.fallback_to_board_vias=false" in args
        assert f"--router.optimizer.max_passes={optimizer_passes or 100}" in args
        assert kwargs["timeout_seconds"] == tc.route_timeout
        settings = json.loads((cwd / "requested-settings.json").read_text())
        snapshot = {}
        for key, value in settings.items():
            if key in ("optimizer.item_selection_strategy", "result_json"):
                continue
            parts = key.split(".")
            target = snapshot
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        manifest = {"schema_version": 1, "app_version": "2.4.1",
                    "fixture": {"sha256": native_bridge._hash(dsn)}, "settings_snapshot": snapshot,
                    "final_state": "TIMED_OUT" if failure == "timeout" else "COMPLETED",
                    "exit_code": 0, "output_written": True}
        if failure == "wrong-input":
            manifest["fixture"]["sha256"] = "different"
        if failure == "settings":
            snapshot["fanout"]["enabled"] = False
        if failure != "missing-json":
            (cwd / "router-result.json").write_text(json.dumps(manifest))
        if failure != "empty-ses":
            ses.write_text("Explicit simulated SES fixture")
        if failure == "changed-staged":
            (cwd / "input.dsn").write_text("changed")
        return {"status": "failed" if failure == "process" else "ok", "reason": "process error",
                "stdout": "Failed to apply CLI router setting: x" if failure == "rejected-flag" else "",
                "stderr": "", "argv": [executable, *args], "version": version}

    monkeypatch.setattr(tc, "_execute", execute)
    result = tc.route(dsn, ses, passes=30)
    assert result["status"] == ("failed" if failure else "ok"), result
    assert not result["electrical_pass"] and not result["version_check_network_disabled"]
    assert dsn.read_bytes() == original and Path(result["working_dir"]).is_dir()
    assert result["requested_settings"]["max_passes"] == 30
    assert result["requested_settings"]["copper_to_edge_clearance_um"] == 500
    if failure == "timeout":
        assert result["timed_out"] and result["ses_sha256"]
    if not failure:
        assert result["effective_settings_verified"] and result["cli_profile"] == "2.4.1-unified"


@pytest.mark.parametrize("failure", ["hash", "java", "edge"])
def test_unified_profile_preflight_blocks(monkeypatch, tmp_path, failure):
    tc = unified_test_toolchain(monkeypatch)
    jar = tc._jar_info()
    if failure == "hash":
        jar["native"]["sha256"] = "not-official"
    if failure == "edge":
        tc.router_edge = None
    monkeypatch.setattr(tc, "_jar_info", lambda: jar)
    monkeypatch.setattr(tc, "_execute", lambda *a, **kw: {"status": "ok", "argv": [],
                "stderr": 'openjdk version "21.0.8"' if failure == "java" else 'openjdk version "25.0.4"'})
    dsn = tmp_path / "in.dsn"
    dsn.write_text("fixture")
    result = tc.route(dsn, tmp_path / "out.ses")
    assert result["status"] == "blocked"
    assert len(list(tmp_path.glob("pcb-route-*"))) == 0


@pytest.mark.skipif(not all(os.environ.get(key) for key in
                    ("PCB_WEAVER_TEST_WSL", "PCB_WEAVER_TEST_241_JAR", "PCB_WEAVER_TEST_241_JAVA")),
                   reason="Opt-in isolated 2.4.1 native small-board integration")
@pytest.mark.parametrize("fanout", [False, True])
def test_real_wsl_unified_241_small(tmp_path, fanout):
    from pcb_weaver.compiler import compile_rules
    tc = Toolchain({"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"],
                    "java": os.environ["PCB_WEAVER_TEST_241_JAVA"],
                    "freerouting_jar": os.environ["PCB_WEAVER_TEST_241_JAR"],
                    "controlled_neckdown": True, "fanout": fanout,
                    "router_copper_to_edge_clearance_mm": 0.5,
                    "route_timeout_seconds": 120})
    board = tmp_path / "sample.kicad_pcb"
    assert tc._native("sample-fanout-4", board)["status"] == "ok"
    compile_rules(board, {"fabrication": {"min_track_mm": 0.25, "min_clearance_mm": 0.2,
                                        "min_via_drill_mm": 0.3},
                          "net_rules": [{"nets": ["POWER"], "min_width_mm": 0.6}]},
                  native_state=tc.inspect_board(board))
    dsn = tmp_path / "sample.dsn"
    exported = tc.export_dsn(board, dsn)
    assert exported["status"] == "ok", exported
    hashes = {str(p): native_bridge._hash(p) for p in [board, dsn, board.with_suffix(".kicad_pro")]}
    ses = tmp_path / "output.ses"
    routed = tc.route(dsn, ses, passes=5)
    assert routed["status"] == "ok", routed
    assert routed["effective_settings_verified"] and not routed["electrical_pass"]
    final = tmp_path / "output.kicad_pcb"
    imported = tc.import_ses(board, ses, final)
    assert imported["status"] == "ok", imported
    drc = tc.run_drc(final, tmp_path / "drc.json")
    evidence = {"release_check": False, "source_hashes": hashes,
                "export": exported, "route": routed, "import": imported, "drc": drc}
    (tmp_path / "unified-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert drc["status"] == "ok" and drc["errors"] == 0 and drc["unconnected"] == 0, drc
    assert imported["placement_preservation"]["restored_exactly"]
    assert imported["track_width_audit"]["observed_minimum_mm"]["POWER"] >= 0.6
    assert imported["track_width_audit"]["observed_minimum_mm"]["LINK"] >= 0.25
    assert hashes == {path: native_bridge._hash(Path(path)) for path in hashes}


@pytest.mark.skipif(not all(os.environ.get(key) for key in (
    "PCB_WEAVER_TEST_WSL", "PCB_WEAVER_TEST_241_JAR", "PCB_WEAVER_TEST_241_JAVA",
    "PCB_WEAVER_TEST_VIA_ROUNDTRIP")), reason="Opt-in corrected 0.6/0.4 mm native via roundtrip")
def test_real_wsl_declared_via_floor_roundtrip(tmp_path):
    from pcb_weaver.board import read_board
    from pcb_weaver.compiler import compile_rules
    tc = Toolchain({"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"],
                    "java": os.environ["PCB_WEAVER_TEST_241_JAVA"],
                    "freerouting_jar": os.environ["PCB_WEAVER_TEST_241_JAR"],
                    "controlled_neckdown": True, "fanout": True,
                    "router_copper_to_edge_clearance_mm": 0.5, "route_timeout_seconds": 120})
    board = tmp_path / "sample.kicad_pcb"
    assert tc._native("sample-fanout-4", board)["status"] == "ok"
    default = {"name": "Default", "clearance": 0.2, "track_width": 0.25,
               "via_diameter": 0.6, "via_drill": 0.4, "microvia_diameter": 0.3, "microvia_drill": 0.1,
               "diff_pair_width": 0.25, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
               "bus_width": 12, "wire_width": 6, "priority": 2147483647}
    project = {"meta": {"version": 1}, "board": {"design_settings": {"rules": {
        "min_via_annular_width": 0.05, "min_via_diameter": 0.5, "min_through_hole_diameter": 0.4}}},
        "net_settings": {"meta": {"version": 4}, "classes": [default,
            {**default, "name": "POWER", "via_diameter": 0.8, "priority": 0}],
            "netclass_assignments": {"POWER": ["POWER"]}}}
    pro = board.with_suffix(".kicad_pro")
    pro.write_text(json.dumps(project), encoding="utf-8")
    compiled = compile_rules(board, {"fabrication": {"min_track_mm": 0.25, "min_clearance_mm": 0.2,
                                                   "min_via_drill_mm": 0.3},
                                    "net_rules": [{"nets": ["POWER"], "min_width_mm": 0.6}]},
                             native_state=tc.inspect_board(board))
    inspected = tc.inspect_board(board)
    for net, diameter in (("LINK", 0.6), ("POWER", 0.8)):
        assert inspected["nets"][net]["via_diameter"] == diameter, inspected
        assert inspected["nets"][net]["via_drill"] == 0.4, inspected
    hashes = {str(p): native_bridge._hash(p) for p in (board, pro)}
    dsn, ses = tmp_path / "input.dsn", tmp_path / "output.ses"
    exported = tc.export_dsn(board, dsn)
    assert exported["status"] == "ok", exported
    routed = tc.route(dsn, ses, passes=5)
    assert routed["status"] == "ok", routed
    final = tmp_path / "output.kicad_pcb"
    imported = tc.import_ses(board, ses, final)
    assert imported["status"] == "ok", imported
    vias = read_board(final)["via_items"]
    drc = tc.run_drc(final, tmp_path / "drc.json")
    evidence = {"release_check": False, "source_hashes": hashes, "compile": compiled,
                "native_rules": inspected, "export": exported, "route": routed,
                "import": imported, "vias": vias, "drc": drc}
    (tmp_path / "via-floor-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert {v["net"] for v in vias} == {"LINK", "POWER"}, vias
    for via in vias:
        assert via["drill_mm"] == 0.4
        assert via["diameter_mm"] == (0.6 if via["net"] == "LINK" else 0.8)
    assert drc["status"] == "ok" and drc["errors"] == 0 and drc["unconnected"] == 0, drc
    assert imported["placement_preservation"]["restored_exactly"]
    assert hashes == {path: native_bridge._hash(Path(path)) for path in hashes}


@pytest.mark.parametrize("version", ["2.0.1", "2.1.0", "2.2.4"])
@pytest.mark.parametrize("neckdown", [False, True])
def test_route_stages_space_paths_and_does_not_assume_electrical_pass(monkeypatch, tmp_path, version, neckdown):
    directory = tmp_path / "Space + Directory"
    directory.mkdir()
    dsn = directory / "a b.dsn"
    dsn.write_text("DSN unit fixture")
    tc = Toolchain({"freerouting_jar": "example.jar", "controlled_neckdown": neckdown})
    monkeypatch.setattr(tc, "_jar_info", lambda: {"status": "ok", "version": version, "argv": [], "native": {"sha256": "jarhash"}})
    def run(argv, **kwargs):
        if argv[-1] == "-version":
            return subprocess.CompletedProcess(argv, 0, "", 'openjdk version "21.0.8"')
        assert argv[argv.index("-de") + 1] == "input.dsn"
        assert argv[argv.index("-mt") + 1] == "1"
        assert argv[argv.index("-is") + 1] == "seq"
        assert "--router.automatic_neckdown=" + str(neckdown).lower() in argv
        assert ("--router.stop_pass_no=10" in argv) == (version == "2.0.1")
        assert "-drc" not in argv
        assert (Path(kwargs["cwd"]) / "input.dsn").read_text() == "DSN unit fixture"
        Path(argv[argv.index("-do") + 1]).write_text("session")
        return subprocess.CompletedProcess(argv, 0, "finished", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = tc.route(dsn, directory / "output.ses")
    if neckdown and version != "2.0.1":
        assert result["status"] == "blocked" and "only for Freerouting 1.9.0 and 2.0.1" in result["reason"]
        return
    assert result["status"] == "ok" and result["version"] == version
    assert result["automatic_neckdown"] == neckdown
    assert result["pass_limit_enforced"] == (version == "2.0.1")
    assert not result["electrical_pass"] and result["jar_sha256"] == "jarhash"
    assert Path(result["commands"][-1]["cwd"]) == Path(result["working_directory"])
    assert result["working_dir"] == result["working_directory"]
    assert (Path(result["working_directory"]) / "input.dsn").read_bytes() == dsn.read_bytes()


def test_controlled_neckdown_requires_explicit_positive_floor(board):
    with pytest.raises(ValueError, match="boolean"):
        Toolchain({"controlled_neckdown": "true"})
    tc = Toolchain({"controlled_neckdown": True})
    assert tc.import_ses(board, board.with_suffix(".ses"), board.parent / "out.kicad_pcb")["status"] == "blocked"


def test_legacy_jar_entrypoint_requires_verified_version(monkeypatch, tmp_path):
    jar = tmp_path / "legacy.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: app.freerouting.gui.MainApplication\nImplementation-Version: unspecified\nBuild-Revision: legacy-test\n")
        archive.writestr("app/freerouting/gui/MainApplication.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x3d")
    assert native_bridge.perform("jar-info", [jar])["status"] == "blocked"
    monkeypatch.setattr(native_bridge, "PINNED_RELEASES", {native_bridge._hash(jar): "1.9.0"})
    result = native_bridge.perform("jar-info", [jar])
    assert result["status"] == "ok" and result["minimum_java"] == 17
    assert result["main_class"] == "app.freerouting.gui.MainApplication"
    assert result["version_source"] == "pinned_release_sha256" and result["build_revision"] == "legacy-test"


@pytest.mark.parametrize("neckdown,display", [(True, True), (True, False), (False, True)])
def test_legacy_profile_isolated_and_never_opens_visible_window(monkeypatch, tmp_path, neckdown, display):
    dsn, ses = tmp_path / "input.dsn", tmp_path / "result.ses"
    dsn.write_text("DSN fixture")
    tc = Toolchain({"wsl_distro": "Ubuntu", "freerouting_jar": "legacy.jar", "controlled_neckdown": neckdown})
    monkeypatch.setattr(tc, "_jar_info", lambda: {"status": "ok", "version": "1.9.0", "argv": [], "native": {"minimum_java": 17}})
    calls = []
    def execute(executable, args, version=None, **kwargs):
        calls.append((executable, args))
        result = {"status": "ok", "argv": [executable, *args], "version": version, "stdout": "", "stderr": ""}
        if args == ["-version"]:
            result["stderr"] = 'openjdk version "21.0.12"'
        elif args == ["--help"]:
            assert executable == "xvfb-run"
            if not display:
                result.update(status="blocked", reason="missing xvfb")
        else:
            assert executable == "xvfb-run" and args[0] == "-a"
            assert "-Djava.awt.headless=false" in args and "-nolisten tcp" in args[1]
            assert "--gui.enabled=false" not in args and not any(a.startswith("--router.") for a in args)
            assert args[args.index("-mp") + 1] == "3" and args[args.index("-mt") + 1] == "1"
            assert args[args.index("-dct") + 1] == "0" and args[args.index("-oit") + 1] == "1"
            ses.write_text("SES fixture")
        return result
    monkeypatch.setattr(tc, "_execute", execute)
    result = tc.route(dsn, ses, passes=3)
    assert result["status"] == ("ok" if neckdown and display else "blocked"), result
    if result["status"] == "ok":
        assert result["display_backend"] == "xvfb" and result["cli_profile"] == "1.9.0-xvfb"
        assert result["pass_limit_enforced"] and not result["electrical_pass"]
    else:
        assert not ses.exists()


@pytest.mark.parametrize("failure", [None, "awt", "missing_help"])
def test_legacy_doctor_exercises_awt_and_persists_evidence(monkeypatch, tmp_path, failure):
    tc = Toolchain({"wsl_distro": "Ubuntu", "freerouting_jar": "legacy.jar", "controlled_neckdown": True})
    monkeypatch.setattr(tc, "_cli_version", lambda: {"status": "ok", "argv": [], "version": "9.0.9"})
    monkeypatch.setattr(tc, "_native", lambda *a: {"status": "ok", "argv": [], "version": "9.0.9"})
    monkeypatch.setattr(tc, "_jar_info", lambda: {"status": "ok", "argv": [], "version": "1.9.0"})
    working = tmp_path / "display"
    working.mkdir()
    monkeypatch.setattr("pcb_weaver.toolchain.tempfile.mkdtemp", lambda **kw: str(working))
    def execute(executable, args, version=None, **kwargs):
        result = {"status": "ok", "argv": [executable, *args], "version": version,
                  "stdout": "-de -do -mp -mt", "stderr": "", "returncode": 0}
        if args == ["-version"]:
            result["stderr"] = 'openjdk version "21.0.12"'
            return result
        assert executable == "xvfb-run" and args[-2:] == ["-da", "-help"]
        assert "-Djava.awt.headless=false" in args and "-nolisten tcp" in args[1]
        assert kwargs["cwd"] == working and kwargs["timeout_seconds"] <= 30
        if failure == "awt":
            result.update(status="failed", returncode=1, reason="Command exited with code 1", stderr="libawt_xawt.so missing")
        elif failure == "missing_help":
            result["stdout"] = "startup failed"
        return result
    monkeypatch.setattr(tc, "_execute", execute)
    result = tc.doctor()
    display = result["tools"]["legacy_display"]
    assert result["status"] == ("blocked" if failure else "ok")
    assert display["status"] == result["status"]
    assert Path(display["log_path"]).is_file() and working.exists()
    assert display["version"] == "1.9.0" and display["argv"][0] == "xvfb-run"


def test_legacy_doctor_requires_neckdown_optin_without_opening_display(monkeypatch):
    tc = Toolchain({"wsl_distro": "Ubuntu", "freerouting_jar": "legacy.jar"})
    def unexpected(*args, **kwargs):
        pytest.fail("Misconfigured legacy profile must not open any display")
    monkeypatch.setattr(tc, "_execute", unexpected)
    result = tc._legacy_display_probe()
    assert result["status"] == "blocked" and "controlled_neckdown=true" in result["reason"]


def fanout_dsn_fixture(path):
    path.write_text('''(pcb "fixture" (parser (string_quote ") (host_cad "KiCad's Pcbnew"))
      (resolution um 10) (unit um)
      (structure (layer F.Cu (type signal)) (layer In1.Cu (type signal))
        (layer In2.Cu (type signal)) (layer B.Cu (type signal))
        (boundary (path pcb 0 20000 20000 60000 20000 60000 45000 20000 45000 20000 20000))
        (rule (width 250) (clearance 200))))''')


def test_fanout_rules_preserve_physics_and_complete_native_defaults(tmp_path):
    import sexpdata
    dsn = tmp_path / "input.dsn"
    fanout_dsn_fixture(dsn)
    original = dsn.read_bytes()
    audit = _legacy_fanout_rules(dsn, tmp_path / "input.rules")
    assert dsn.read_bytes() == original
    assert audit["default_changes"] == ["fanout"] and not audit["physical_rule_overrides"]
    assert audit["native_coordinate_scale"] == 10
    assert audit["native_padded_bounds"] == [199000, 199000, 601000, 451000]
    settings = audit["settings"]
    assert {k:settings[k] for k in ["fanout", "autoroute", "postroute", "vias"]} == dict.fromkeys(["fanout", "autoroute", "postroute", "vias"], True)
    assert settings["via_costs"] == 50 and settings["plane_via_costs"] == 5
    assert settings["start_ripup_costs"] == 100 and settings["start_pass_no"] == 1
    layers = settings["layer_rules"]
    assert [r["preferred_direction"] for r in layers] == ["horizontal", "vertical"] * 2
    assert [r["preferred_direction_trace_costs"] for r in layers] == [1.8, 1, 1, 1.8]
    assert [r["against_preferred_direction_trace_costs"] for r in layers] == pytest.approx([3.4, 1.6, 2.6, 2.4])
    tree = sexpdata.loads((tmp_path / "input.rules").read_text())
    assert str(tree[0]) == "rules" and str(tree[1]) == "PCB" and tree[2] == "input"
    assert len(tree) == 4 and str(tree[3][0]) == "autoroute_settings"
    assert audit["rules_sha256"] and audit["settings_sha256"]


@pytest.mark.parametrize("value", ["true", 1, None])
def test_fanout_requires_strict_bool_in_runtime_and_adapter(value):
    from pcb_weaver.models import ToolchainConfig
    with pytest.raises(ValueError):
        ToolchainConfig(fanout=value)
    with pytest.raises(ValueError):
        Toolchain({"fanout": value})
    assert not ToolchainConfig().fanout and not Toolchain().fanout


def test_fanout_field_survives_runtime(tmp_path):
    from pcb_weaver.runtime import load_runtime
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"fanout": True, "controlled_neckdown": True}))
    _, values = load_runtime(str(tmp_path / "data"), str(config))
    assert values["fanout"] is True


@pytest.mark.parametrize("change", ["existing", "power", "invalid", "quote"])
def test_fanout_rejects_unverified_settings_without_writing(tmp_path, change):
    dsn = tmp_path / "input.dsn"
    fanout_dsn_fixture(dsn)
    text = dsn.read_text()
    if change == "existing":
        text = text.replace("(structure", "(structure (autoroute_settings (fanout off))")
    elif change == "power":
        text = text.replace("(type signal)", "(type power)", 1)
    elif change == "invalid":
        text = text[:-1]
    else:
        text = text.replace('(string_quote ")', '(string_quote x)')
    dsn.write_text(text)
    with pytest.raises(ValueError):
        _legacy_fanout_rules(dsn, tmp_path / "input.rules")
    assert not (tmp_path / "input.rules").exists()


@pytest.mark.parametrize("version,observed", [("1.9.0", True), ("1.9.0", False), ("2.0.1", True)])
def test_fanout_route_requires_legacy_execution_evidence(monkeypatch, tmp_path, version, observed):
    dsn, ses = tmp_path / "input.dsn", tmp_path / "output.ses"
    fanout_dsn_fixture(dsn)
    tc = Toolchain({"wsl_distro": "Ubuntu", "freerouting_jar": "legacy.jar", "controlled_neckdown": True, "fanout": True})
    monkeypatch.setattr(tc, "_jar_info", lambda: {"status": "ok", "version": version, "argv": []})
    def execute(executable, args, version=None, **kwargs):
        result = {"status": "ok", "argv": [executable, *args], "version": version, "stdout": "", "stderr": ""}
        if args == ["-version"]:
            result["stderr"] = 'openjdk version "21.0.12"'
        elif "-jar" in args:
            assert args[args.index("-dr") + 1].endswith("/input.rules")
            assert (kwargs["cwd"] / "input.rules").is_file()
            ses.write_text("session")
            if observed:
                result["stdout"] = "Opening '" + args[args.index("-dr") + 1] + "'..."
        return result
    monkeypatch.setattr(tc, "_execute", execute)
    result = tc.route(dsn, ses)
    assert result["status"] == ("blocked" if version != "1.9.0" else "ok" if observed else "failed")
    if version == "1.9.0":
        assert result["fanout_audit"]["input_and_rules_unchanged"]
        assert result["fanout_audit"]["rules_load_observed"] == observed
        assert not result["fanout_audit"]["execution_observed"]


@pytest.mark.parametrize("width,declared,accepted", [(0.3, None, True), (0.199999, None, False),
    (0.3, 0.35, False), (0.35, 0.35, True)])
def test_native_neckdown_checks_hard_minimum_not_nominal(tmp_path, width, declared, accepted):
    source = tmp_path / "board.kicad_pcb"
    project = {"board": {"design_settings": {"rules": {"min_track_width": 0.2}}},
               "net_settings": {"classes": [{"name": "POWER", "track_width": 0.4}]}}
    if declared is not None:
        project["pcb_weaver_net_rules"] = {"POWER": {"declared_min_width_mm": declared,
                                                    "minimums": {"track_width": 0.4}}}
    source.with_suffix(".kicad_pro").write_text(json.dumps(project))
    via_type = type("Via", (), {})
    pcbnew = SimpleNamespace(PCB_VIA=via_type, FromMM=lambda mm: round(mm * 1e6), ToMM=lambda iu: iu / 1e6)
    board = SimpleNamespace(GetTracks=lambda: [SimpleNamespace(GetNetname=lambda: "POWER", GetWidth=lambda: round(width * 1e6)), via_type()])
    if not accepted:
        with pytest.raises(ValueError, match="below declared minimum"):
            native_bridge._verify_track_minima(pcbnew, board, source)
    else:
        audit = native_bridge._verify_track_minima(pcbnew, board, source)
        assert audit["verified"] and audit["requires_drc"] and audit["segments_checked"] == 1
        assert audit["observed_minimum_mm"] == {"POWER": width}


def test_manufacturing_requires_each_real_artifact(monkeypatch, board):
    fake_inspection(monkeypatch)
    def run(argv, **kwargs):
        if argv[-1] == "version":
            return subprocess.CompletedProcess(argv, 0, "8.0.9", "")
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, CLI_HELP, "")
        target = Path(argv[argv.index("--output") + 1])
        if "gerbers" in argv:
            fake_gerbers(target)
        # Simulate a CLI reporting success without creating drill files.
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = Toolchain().export_manufacturing(board, board.parent / "manufacturing")
    assert result["status"] == "failed" and "drill" in result["reason"]
    assert len(result["artifacts"]) == 10


def test_complete_manufacturing_includes_schematic_bom(monkeypatch, board):
    fake_inspection(monkeypatch)
    board.with_suffix(".kicad_sch").write_text("schematic fixture")
    def run(argv, **kwargs):
        if argv[-1] == "version":
            return subprocess.CompletedProcess(argv, 0, "8.0.9", "")
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, CLI_HELP, "")
        output = Path(argv[argv.index("--output") + 1])
        if "gerbers" in argv:
            fake_gerbers(output)
            return subprocess.CompletedProcess(argv, 0, "", "")
        elif "drill" in argv:
            output = output / "board.drl"
        output.write_text("test artifact")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = Toolchain().export_manufacturing(board, board.parent / "fab")
    assert result["status"] == "ok" and result["bom_status"] == "ok"
    assert {a["kind"] for a in result["artifacts"]} == {"gerber", "gerber_job", "drill", "pos", "bom"}
    assert all(a["sha256"] and Path(a["path"]).is_file() for a in result["artifacts"])


@pytest.mark.parametrize("tamper", ["missing", "wrong_layer", "empty", "incomplete", "escape"])
def test_gerber_layer_validation_fails_closed(tmp_path, tamper):
    from pcb_weaver.toolchain import _validate_gerbers
    fake_gerbers(tmp_path, 4)
    jobpath = tmp_path / "board-job.gbrjob"
    job = json.loads(jobpath.read_text())
    target = tmp_path / job["FilesAttributes"][-2]["Path"]
    if tamper == "missing":
        target.unlink()
    elif tamper == "wrong_layer":
        target.write_text("%TF.FileFunction,Copper,L1,Top*%\nM02*\n")
    elif tamper == "empty":
        target.write_text("")
    elif tamper == "incomplete":
        target.write_text(target.read_text().replace("M02*", ""))
    else:
        job["FilesAttributes"][-2]["Path"] = "../escape.gbr"
        jobpath.write_text(json.dumps(job))
    with pytest.raises(ValueError):
        _validate_gerbers(tmp_path, 4)


def test_routing_timeout_has_its_own_audit_budget(monkeypatch):
    calls = install_cli_fake(monkeypatch)
    tc = Toolchain({"wsl_distro": "Ubuntu", "timeout_seconds": 60, "route_timeout_seconds": 2400})
    result = tc._execute("java", ["-version"], timeout_seconds=tc.route_timeout)
    assert calls[-1][1]["timeout"] == 2440
    assert result["engine_timeout_seconds"] == 2400 and "2400s" in result["argv"]
    assert Toolchain({"timeout_seconds": 900}).route_timeout == 900


def test_failed_native_export_never_claims_success(monkeypatch, board):
    fake = SimpleNamespace(GetBuildVersion=lambda: "9.0.1", LoadBoard=lambda p: empty_native_board(), ExportSpecctraDSN=lambda b, p: False)
    monkeypatch.setattr(native_bridge.importlib, "import_module", lambda name: fake)
    result = native_bridge.perform("export-dsn", [board, board.with_suffix(".dsn")])
    assert result["status"] == "failed" and "returned false" in result["reason"]


@pytest.mark.skipif(not os.environ.get("PCB_WEAVER_TEST_WSL"), reason="Opt-in real KiCad WSL integration")
def test_real_wsl_kicad_roundtrip(tmp_path):
    from pcb_weaver.board import read_board

    evidence = {}
    config = {"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"], "timeout_seconds": 120,
              "freerouting_jar": os.environ.get("PCB_WEAVER_TEST_JAR")}
    tc = Toolchain(config)
    board = tmp_path / "source.kicad_pcb"
    sample = tc._native("sample-board", board)
    assert sample["status"] == "ok", sample
    evidence["sample"] = sample
    # Deliberately non-default track width verifies the project sidecar survives
    # the native copy/load/export/import path, rather than silently using defaults.
    project = {"meta": {"version": 1}, "net_settings": {"classes": [
        {"name": "Default", "clearance": 0.3, "track_width": 0.4,
         "via_diameter": 0.8, "via_drill": 0.4, "bus_width": 12, "wire_width": 6,
         "diff_pair_width": 0.4, "diff_pair_gap": 0.3, "diff_pair_via_gap": 0.3}]}}
    board.with_suffix(".kicad_pro").write_text(json.dumps(project))
    from pcb_weaver.compiler import compile_rules
    evidence["compile"] = compile_rules(board, {"fabrication": {"min_track_mm": 0.4,
        "min_clearance_mm": 0.3, "min_via_drill_mm": 0.4}, "net_rules": []})
    assert json.loads(board.with_suffix(".kicad_pro").read_text())["net_settings"]["meta"]["version"] == 3
    original_geometry = read_board(board)
    result = tc.run_drc(board, tmp_path / "before.json")
    assert result["status"] == "ok" and result["report_valid"], result
    assert result["unconnected"] > 0 and not result["electrical_pass"]
    evidence["before_drc"] = result
    filled = tmp_path / "filled.kicad_pcb"
    evidence["fill"] = tc.fill_zones(board, filled)
    assert evidence["fill"]["status"] == "ok", evidence["fill"]
    assert filled.with_suffix(".kicad_pro").read_bytes() == board.with_suffix(".kicad_pro").read_bytes()
    dsn = tmp_path / "input.dsn"
    evidence["dsn"] = tc.export_dsn(filled, dsn)
    assert evidence["dsn"]["status"] == "ok", evidence["dsn"]
    fabrication = tc.export_manufacturing(filled, tmp_path / "fab")
    assert fabrication["status"] == "ok", fabrication
    assert {a["kind"] for a in fabrication["artifacts"]} >= {"gerber", "drill", "pos"}
    evidence["manufacturing"] = fabrication
    # An original minimal schematic exercises real ERC and XML netlist exporters.
    schematic = tmp_path / "empty.kicad_sch"
    schematic.write_text('(kicad_sch (version 20231120) (generator eeschema) '
                         '(uuid "33b24925-899b-481a-86aa-d810ea74595a") (paper "A4") (lib_symbols))')
    erc = tc.run_erc(schematic, tmp_path / "erc.json")
    assert erc["status"] == "ok" and erc["report_valid"], erc
    evidence["erc"] = erc
    xml = tc.export_netlist(schematic, tmp_path / "nets.xml")
    assert xml["status"] == "ok", xml
    import xml.etree.ElementTree as ET
    assert ET.parse(xml["output_path"]).getroot().tag == "export"
    evidence["netlist"] = xml
    board.with_suffix(".kicad_sch").write_bytes(schematic.read_bytes())
    parity = tc.run_drc(board, tmp_path / "parity.json")
    assert parity["status"] == "ok" and parity["schematic_parity_checked"], parity
    evidence["parity"] = parity
    fabrication_with_bom = tc.export_manufacturing(board, tmp_path / "fab-with-bom")
    assert fabrication_with_bom["status"] == "ok" and fabrication_with_bom["bom_status"] == "ok", fabrication_with_bom
    evidence["manufacturing_with_bom"] = fabrication_with_bom
    if config["freerouting_jar"]:
        ses = tmp_path / "routed.ses"
        routed = tc.route(dsn, ses)
        assert routed["status"] == "ok", routed
        evidence["route"] = routed
        final = tmp_path / "routed.kicad_pcb"
        imported = tc.import_ses(filled, ses, final)
        assert imported["status"] == "ok", imported
        evidence["ses_import"] = imported
        preservation = imported["commands"][-1]["native"]["placement_preservation"]
        assert preservation["restored_exactly"] and 0 < preservation["max_axis_drift_mm"] <= 0.0001
        assert final.with_suffix(".kicad_pro").read_bytes() == filled.with_suffix(".kicad_pro").read_bytes()
        routed_geometry = read_board(final)
        assert routed_geometry["tracks"] > 0
        import sexpdata
        tree = sexpdata.loads(final.read_text(encoding="utf-8"))
        widths = [float(field[1]) for node in tree if isinstance(node, list) and node and node[0] == sexpdata.Symbol("segment")
                  for field in node if isinstance(field, list) and field and field[0] == sexpdata.Symbol("width")]
        assert widths and all(width >= 0.4 for width in widths), widths
        def signature(data):
            return sorted((f["reference"], f["x"], f["y"], f["rotation"], f["layer"],
                           tuple(sorted((p["number"], p["net"]) for p in f["pads"]))) for f in data["footprints"])
        assert signature(routed_geometry) == signature(original_geometry)
        checked = tc.run_drc(final, tmp_path / "after.json")
        assert checked["status"] == "ok" and checked["unconnected"] == 0, checked
        evidence["after_drc"] = checked
        evidence["geometry"] = {"tracks": routed_geometry["tracks"], "vias": routed_geometry["vias"], "signature_preserved": True,
                                "minimum_track_width_mm": min(widths)}
    (tmp_path / "integration-results.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("PCB_WEAVER_TEST_WSL"), reason="Opt-in real KiCad WSL demo integration")
def test_real_wsl_demo_reports_unconnected_and_preserves_geometry(tmp_path):
    from pcb_weaver.board import read_board
    from pcb_weaver.toolchain import _sha256

    example = Path(__file__).resolve().parents[1] / "examples" / "routing-demo"
    original_hashes = {p.relative_to(example): _sha256(p) for p in example.rglob("*") if p.is_file()}
    design = tmp_path / "input-design"
    shutil.copytree(example, design)
    board = design / "two-layer.kicad_pcb"
    tc = Toolchain({"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"], "timeout_seconds": 180,
                    "freerouting_jar": os.environ.get("PCB_WEAVER_TEST_JAR")})
    evidence = {}
    before = tc.run_drc(board, tmp_path / "before.json")
    evidence["before_drc"] = before
    assert before["status"] == "ok" and before["unconnected"] > 0, before
    assert before["schematic_parity_checked"] and not before["electrical_pass"]
    evidence["erc"] = tc.run_erc(board.with_suffix(".kicad_sch"), tmp_path / "erc.json")
    assert evidence["erc"]["status"] == "ok", evidence["erc"]
    dsn = tmp_path / "demo.dsn"
    evidence["dsn"] = tc.export_dsn(board, dsn)
    assert evidence["dsn"]["status"] == "ok", evidence["dsn"]
    initial = read_board(board)
    if tc.jar:
        ses = tmp_path / "demo.ses"
        evidence["route"] = tc.route(dsn, ses)
        assert evidence["route"]["status"] == "ok", evidence["route"]
        # Copy the complete project so relative footprint/symbol libraries still
        # resolve when the returned board is checked with schematic parity.
        routed_design = tmp_path / "routed-design"
        shutil.copytree(design, routed_design)
        routed = routed_design / board.name
        routed.unlink()
        evidence["import"] = tc.import_ses(board, ses, routed)
        assert evidence["import"]["status"] == "ok", evidence["import"]
        final = read_board(routed)
        def signature(data):
            return sorted((f["reference"], f["x"], f["y"], f["rotation"], f["layer"],
                           tuple(sorted((p["number"], p["net"]) for p in f["pads"]))) for f in data["footprints"])
        assert signature(initial) == signature(final)
        after = tc.run_drc(routed, tmp_path / "after.json")
        evidence["after_drc"] = after
        assert after["status"] == "ok" and after["unconnected"] == 0 and after["schematic_parity_checked"], after
        evidence["geometry"] = {"tracks": final["tracks"], "vias": final["vias"], "signature_preserved": True}
    assert original_hashes == {p.relative_to(example): _sha256(p) for p in example.rglob("*") if p.is_file()}
    (tmp_path / "demo-integration-results.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("PCB_WEAVER_TEST_WSL") or not os.environ.get("PCB_WEAVER_TEST_FANOUT_JAR"),
                    reason="Opt-in real Freerouting 1.9 fanout A/B integration")
def test_real_wsl_legacy_fanout_same_dsn_ab(tmp_path):
    from pcb_weaver.board import read_board
    from pcb_weaver.compiler import compile_rules
    from pcb_weaver.toolchain import _sha256

    config = {"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"],
              "freerouting_jar": os.environ["PCB_WEAVER_TEST_FANOUT_JAR"],
              "controlled_neckdown": True, "timeout_seconds": 120, "route_timeout_seconds": 240}
    tc = Toolchain(config)
    source = tmp_path / "sample.kicad_pcb"
    created = tc._native("sample-fanout-4", source)
    assert created["status"] == "ok", created
    before = read_board(source)
    assert before["tracks"] == 0 and before["vias"] == 0
    assert {p["type"] for f in before["footprints"] for p in f["pads"]} == {"smd"}
    compile_rules(source, {"fabrication": {"min_track_mm": 0.25, "min_clearance_mm": 0.2, "min_via_drill_mm": 0.3},
                          "net_rules": [{"nets": ["POWER"], "min_width_mm": 0.6}]}, native_state=tc.inspect_board(source))
    dsn = tmp_path / "input.dsn"
    exported = tc.export_dsn(source, dsn)
    assert exported["status"] == "ok", exported
    protected_paths = [source, dsn, *[p for p in (source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru")) if p.is_file()]]
    protected = {p.name: _sha256(p) for p in protected_paths}
    evidence = {"source_hashes": protected, "export": exported, "runs": {}}
    for enabled in (False, True):
        tc = Toolchain({**config, "fanout": enabled})
        directory = tmp_path / ("fanout-on" if enabled else "fanout-off")
        routed = tc.route(dsn, directory / "result.ses", passes=5)
        evidence["runs"][str(enabled).lower()] = {"route": routed}
        assert routed["status"] == "ok", routed
        final = directory / "result.kicad_pcb"
        imported = tc.import_ses(source, directory / "result.ses", final)
        assert imported["status"] == "ok", imported
        board = read_board(final)
        drc = tc.run_drc(final, directory / "drc.json")
        evidence["runs"][str(enabled).lower()].update(import_result=imported, drc=drc,
            vias=board["vias"], tracks=board["tracks"], observed_widths=imported["track_width_audit"]["observed_minimum_mm"])
        assert drc["status"] == "ok" and drc["errors"] == 0 and drc["unconnected"] == 0, drc
        assert imported["placement_preservation"]["restored_exactly"]
        assert board["vias"] > 0
        assert imported["track_width_audit"]["observed_minimum_mm"]["POWER"] >= 0.6
        assert imported["track_width_audit"]["observed_minimum_mm"]["LINK"] >= 0.25
        if enabled:
            assert routed["fanout_audit"]["rules_load_observed"]
        assert protected == {p.name: _sha256(p) for p in protected_paths}
    # Isolate the fanout phase on the same no-copper DSN. No autoroute or
    # optimizer can create the vias observed in this diagnostic export.
    import sexpdata
    phase = tmp_path / "fanout-phase-only"
    phase.mkdir()
    shutil.copy2(dsn, phase / "input.dsn")
    derived = _legacy_fanout_rules(phase / "input.dsn", phase / "input.rules")
    rules = sexpdata.loads((phase / "input.rules").read_text())
    for entry in rules[3][1:]:
        if str(entry[0]) in ("autoroute", "postroute"):
            entry[1] = sexpdata.Symbol("off")
    (phase / "input.rules").write_text(sexpdata.dumps(rules), encoding="utf-8")
    output = phase / "native-output.dsn"
    command = tc._execute("xvfb-run", ["-a", "--server-args=-screen 0 1280x1024x24 -nolisten tcp", tc._path(tc.java),
        "-Djava.awt.headless=false", "-Djava.io.tmpdir=.", "-jar", tc._path(Path(config["freerouting_jar"]).resolve()),
        "-da", "-de", "input.dsn", "-do", tc._path(output), "-dr", tc._path(phase / "input.rules"), "-mp", "5", "-mt", "1", "-is", "seq", "-dct", "0", "-oit", "1"],
        "1.9.0", cwd=phase, timeout_seconds=120)
    assert command["status"] == "ok" and output.stat().st_size > 0, command
    tree = sexpdata.loads(re.sub(r'\(string_quote "\)', '(string_quote "double_quote")', output.read_text()))
    def child(node, key):
        return next(v for v in node if isinstance(v, list) and v and str(v[0]) == key)
    effective = child(child(tree, "structure"), "autoroute_settings")
    assert str(child(effective, "fanout")[1]) == "on"
    assert str(child(effective, "autoroute")[1]) == "off" and str(child(effective, "postroute")[1]) == "off"
    for key in ("via_costs", "plane_via_costs", "start_ripup_costs", "start_pass_no"):
        assert child(effective, key)[1] == derived["settings"][key]
    actual_layers = [v for v in effective[1:] if str(v[0]) == "layer_rule"]
    for expected in derived["settings"]["layer_rules"]:
        actual = next(v for v in actual_layers if str(v[1]) == expected["name"])
        assert str(child(actual, "active")[1]) == "on"
        assert str(child(actual, "preferred_direction")[1]) == expected["preferred_direction"]
        for key in ("preferred_direction_trace_costs", "against_preferred_direction_trace_costs"):
            assert child(actual, key)[1] == pytest.approx(expected[key], abs=1e-12)
    fanout_vias = [v for v in child(tree, "wiring")[1:] if str(v[0]) == "via"]
    assert len(fanout_vias) > 0
    evidence["fanout_phase_only"] = {"command": command, "rules_sha256": _sha256(phase / "input.rules"),
        "input_dsn_sha256": _sha256(phase / "input.dsn"), "native_output_sha256": _sha256(output),
        "settings_readback": sexpdata.dumps(effective), "effective_settings_verified": True,
        "fanout_generated_vias": len(fanout_vias), "autoroute": False, "postroute": False, "release_check": False}
    assert protected == {p.name: _sha256(p) for p in protected_paths}
    (tmp_path / "fanout-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("PCB_WEAVER_TEST_WSL") or not os.environ.get("PCB_WEAVER_TEST_JAR"),
                    reason="Opt-in real multilayer routing matrix")
@pytest.mark.parametrize("layers", [2, 4, 6, 8])
@pytest.mark.parametrize("neckdown", [False, True])
def test_real_wsl_multilayer_per_net_and_continuation(tmp_path, layers, neckdown):
    import sexpdata
    from pcb_weaver.compiler import compile_rules
    tc = Toolchain({"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"], "freerouting_jar": os.environ["PCB_WEAVER_TEST_JAR"],
                    "timeout_seconds": 120, "route_timeout_seconds": 240, "controlled_neckdown": neckdown})
    board = tmp_path / "platform.kicad_pcb"
    assert tc._native(f"sample-platform-{layers}", board)["status"] == "ok"
    if layers in (4, 8):
        board.with_suffix(".kicad_pro").write_text(json.dumps({"meta": {"version": 1}, "net_settings": {
            "meta": {"version": 4}, "classes": [
                {"name": "Default", "priority": 2147483647, "track_width": 0.25, "clearance": 0.2,
                 "via_diameter": 0.8, "via_drill": 0.4},
                {"name": "PowerBase", "priority": 0, "track_width": 0.45, "clearance": 0.3, "diff_pair_gap": 0.42},
                {"name": "Analog", "priority": 1, "track_width": None, "clearance": 0.25}],
            "netclass_assignments": {"POWER": ["PowerBase"]},
            "netclass_patterns": [{"pattern": "*", "netclass": "Analog"}]}}))
        board.with_suffix(".kicad_dru").write_text('(version 1)\n(rule original_power (condition "A.hasNetclass(\'PowerBase\')") (constraint clearance (min 0.3mm)))')
    inspection = tc.inspect_board(board)
    assert inspection["status"] == "ok" and len(inspection["copper_layers"]) == layers, inspection
    compiled = compile_rules(board, {"fabrication": {"min_track_mm": 0.25, "min_clearance_mm": 0.2, "min_via_drill_mm": 0.3},
        "net_rules": [{"nets": ["POWER"], "min_width_mm": 0.6}]}, native_state=inspection)
    loaded = tc.inspect_board(board)
    assert loaded["nets"]["POWER"]["track_width"] == 0.6, loaded
    assert loaded["nets"]["LINK"]["track_width"] == 0.25, loaded
    if layers in (4, 8):
        assert {"PowerBase", "Analog"}.issubset(loaded["nets"]["POWER"]["class_names"]), loaded
        assert loaded["nets"]["POWER"]["diff_pair_gap"] == 0.42
    dsn, ses = tmp_path / "platform.dsn", tmp_path / "platform.ses"
    exported = tc.export_dsn(board, dsn)
    assert exported["status"] == "ok", exported
    routed = tc.route(dsn, ses, passes=5)
    assert routed["status"] == "ok", routed
    assert routed["automatic_neckdown"] == neckdown and routed["pass_limit_enforced"], routed
    final = tmp_path / "routed.kicad_pcb"
    imported = tc.import_ses(board, ses, final)
    assert imported["status"] == "ok", imported
    assert imported["track_width_audit"]["verified"], imported
    assert imported["track_width_audit"]["per_net_minimum_mm"]["POWER"] == 0.6
    tree = sexpdata.loads(final.read_text(encoding="utf-8"))
    netcodes = {node[1]: node[2] for node in tree if isinstance(node, list) and node and node[0] == sexpdata.Symbol("net")}
    widths = {}
    for node in tree:
        if isinstance(node, list) and node and node[0] == sexpdata.Symbol("segment"):
            fields = {str(f[0]): f[1:] for f in node[1:] if isinstance(f, list)}
            widths.setdefault(netcodes[fields["net"][0]], set()).add(float(fields["width"][0]))
    assert widths["POWER"] == {0.6} and widths["LINK"] == {0.25}, widths
    drc = tc.run_drc(final, tmp_path / "drc.json")
    assert drc["status"] == "ok" and drc["errors"] == 0 and drc["unconnected"] == 0, drc
    manufacturing = tc.export_manufacturing(final, tmp_path / "fab")
    assert manufacturing["status"] == "ok" and len(manufacturing["copper_layers"]) == layers, manufacturing
    evidence = {"layers": layers, "controlled_neckdown": neckdown, "inspect": inspection, "compiled": compiled, "loaded": loaded, "export": exported,
                "route": routed, "import": imported, "widths": {net: sorted(w) for net, w in widths.items()},
                "drc": drc, "manufacturing": manufacturing}
    if neckdown:
        # Replay the real SES against an independently strengthened project.
        # Reject its 0.6 mm POWER tracks before saving, not just in a mock audit.
        stricter = tmp_path / "strict.kicad_pcb"
        shutil.copy2(board, stricter)
        project = json.loads(board.with_suffix(".kicad_pro").read_text())
        entry = project["pcb_weaver_net_rules"]["POWER"]
        entry["declared_min_width_mm"] = 0.65
        entry["minimums"]["track_width"] = 0.65
        next(c for c in project["net_settings"]["classes"] if c["name"] == entry["class"])["track_width"] = 0.65
        stricter.with_suffix(".kicad_pro").write_text(json.dumps(project))
        if board.with_suffix(".kicad_dru").exists():
            shutil.copy2(board.with_suffix(".kicad_dru"), stricter.with_suffix(".kicad_dru"))
        rejected_path = tmp_path / "rejected.kicad_pcb"
        rejection = tc.import_ses(stricter, ses, rejected_path)
        assert rejection["status"] == "blocked" and "below declared minimum on POWER" in rejection["reason"], rejection
        assert not rejected_path.exists()
        evidence["stronger_minimum_rejected"] = rejection
    repeated_dsn, repeated_ses = tmp_path / "continue.dsn", tmp_path / "continue.ses"
    evidence["continue_export"] = tc.export_dsn(final, repeated_dsn)
    assert evidence["continue_export"]["status"] == "ok", evidence["continue_export"]
    evidence["continue_route"] = tc.route(repeated_dsn, repeated_ses, passes=2)
    if evidence["continue_route"]["status"] != "ok":
        # Freerouting 2.0.1 can terminate an already-complete job without writing
        # SES. This is negative capability evidence, never a routing success.
        assert evidence["continue_route"]["status"] == "failed" and "no nonempty SES" in evidence["continue_route"]["reason"]
        evidence["continuation_verified"] = False
        (tmp_path / "multilayer-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        return
    evidence["continue_import"] = tc.import_ses(final, repeated_ses, tmp_path / "continued.kicad_pcb")
    continued = evidence["continue_import"]
    if continued["status"] == "ok":
        assert continued["commands"][-1]["native"]["copper_preservation"]["preserved_exactly"]
        evidence["continue_drc"] = tc.run_drc(tmp_path / "continued.kicad_pcb", tmp_path / "continue-drc.json")
        assert evidence["continue_drc"]["errors"] == 0 and evidence["continue_drc"]["unconnected"] == 0
    else:
        assert continued["status"] == "blocked" and "preserve existing copper" in continued["reason"], continued
    (tmp_path / "multilayer-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("PCB_WEAVER_TEST_WSL") or not os.environ.get("PCB_WEAVER_TEST_JAR"),
                    reason="Opt-in native partial-copper continuation evidence")
@pytest.mark.parametrize("layers", [2, 4, 6, 8])
def test_real_wsl_partial_copper_is_never_silently_removed(tmp_path, layers):
    from pcb_weaver.compiler import compile_rules
    tc = Toolchain({"wsl_distro": os.environ["PCB_WEAVER_TEST_WSL"], "freerouting_jar": os.environ["PCB_WEAVER_TEST_JAR"],
                    "timeout_seconds": 120, "route_timeout_seconds": 240})
    board = tmp_path / "partial.kicad_pcb"
    created = tc._native(f"sample-partial-{layers}", board)
    assert created["status"] == "ok", created
    inspection = tc.inspect_board(board)
    assert inspection["status"] == "ok" and inspection["existing_copper_items"] == 3, inspection
    assert inspection["zones"] == 1
    compile_rules(board, {"fabrication": {"min_track_mm": 0.25, "min_clearance_mm": 0.2, "min_via_drill_mm": 0.3},
        "net_rules": [{"nets": ["POWER"], "min_width_mm": 0.6}]}, native_state=inspection)
    filled = tmp_path / "filled.kicad_pcb"
    filling = tc.fill_zones(board, filled)
    assert filling["status"] == "ok", filling
    board = filled
    before = tc.run_drc(board, tmp_path / "before.json")
    assert before["status"] == "ok" and before["unconnected"] > 0, before
    dsn, ses = tmp_path / "partial.dsn", tmp_path / "partial.ses"
    exported = tc.export_dsn(board, dsn)
    assert exported["status"] == "ok", exported
    routed = tc.route(dsn, ses, passes=10)
    assert routed["status"] == "ok", routed
    output = tmp_path / "continued.kicad_pcb"
    imported = tc.import_ses(board, ses, output)
    evidence = {"layers": layers, "before": before, "export": exported, "route": routed, "import": imported,
                "continuation_verified": imported["status"] == "ok"}
    if imported["status"] == "ok":
        audit = imported["commands"][-1]["native"]["copper_preservation"]
        assert audit["preserved_exactly"] and audit["input_items"] == 3 and audit["zones_preserved"] == 1
        after = tc.run_drc(output, tmp_path / "after.json")
        assert after["status"] == "ok" and after["errors"] == 0 and after["unconnected"] == 0, after
        evidence["after"] = after
    else:
        assert imported["status"] == "blocked" and "preserve existing copper" in imported["reason"], imported
        assert not output.exists()
    (tmp_path / "partial-copper-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
