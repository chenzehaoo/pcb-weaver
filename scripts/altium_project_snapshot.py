"""Byte-preserving offline snapshot of the fixed Altium project's six documents.

This collects explicit DocumentPath dependencies only, not external libraries or
proof of project compilation, SI completeness, or DRC acceptance.
"""
import argparse
import configparser
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import stat
import uuid


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROJECT = Path("D:/Altium/AD26-Examples/Examples/Mini PC/Mini PC - WiFi/WiFi_miniPCIe.PrjPcb")
SNAPSHOT_ROOT = ROOT / "docs/validation/altium-project"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _no_links(path):
    _require(path.is_absolute(), "Requires absolute path")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
                 "Link or reparse point rejected: " + str(item))


def _read(path):
    _no_links(path)
    _require(path.is_file(), "Missing dependency file: " + str(path))
    raw = path.read_bytes()
    _no_links(path)
    _require(bool(raw), "Empty dependency file: " + str(path))
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def document_paths(raw):
    """Parse the observed six-document profile; reject non-local filenames."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(raw.decode("utf-8-sig"))
    _require(not parser.defaults(), "Project DEFAULT inheritance is unsupported")
    sections = [section for section in parser.sections() if section.casefold().startswith("document")]
    _require(set(sections) == {"Document" + str(i) for i in range(1, 7)},
             "Requires exactly Document1 through Document6")
    _require(all(section in sections or "documentpath" not in parser[section] for section in parser.sections()),
             "DocumentPath outside document section")
    names, seen = [], set()
    for section in sorted(sections):
        name = parser[section].get("documentpath", "")
        win = PureWindowsPath(name)
        _require(bool(name) and not win.drive and not win.root and name not in {".", ".."}
                 and not any(char in name for char in '/\\:<>"|?*')
                 and not any(ord(char) < 32 for char in name)
                 and name == name.strip() and not name.endswith("."),
                 "DocumentPath must be a same-directory relative filename: " + name)
        _require(not re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.I),
                 "Reserved Windows filename")
        _require(name.casefold() not in seen, "Duplicate DocumentPath")
        seen.add(name.casefold())
        names.append(name)
    return names


def _write_new(path, raw):
    _no_links(path)
    with path.open("xb") as stream:
        stream.write(raw)


def snapshot_project(source_project=SOURCE_PROJECT, snapshot_root=SNAPSHOT_ROOT):
    """Create a fresh UUID directory; keep failures without claiming completion.

    Optional paths support offline tests. The CLI always uses the fixed source
    and repository evidence root. Source files are only ever opened for reading.
    """
    source, output = Path(source_project), Path(snapshot_root)
    folder = None
    result = {"schema": 1, "status": "failed", "source_project": str(source),
              "source_unchanged": False, "files": [], "native_invoked": False,
              "compile_verified": False, "native_drc_pass": False,
              "coverage": "Explicit project DocumentPath entries only; external dependencies unproven"}
    try:
        project_raw = _read(source)
        names = document_paths(project_raw)
        _require(source.name.casefold() not in {name.casefold() for name in names},
                 "Project cannot reference itself")
        _require("manifest.json" not in {name.casefold() for name in [source.name, *names]},
                 "Dependency conflicts with manifest filename")
        sources = [source, *(source.parent / name for name in names)]
        originals = [project_raw, *(_read(path) for path in sources[1:])]
        _no_links(output)
        _require(not output.is_relative_to(source.parent) and not source.parent.is_relative_to(output),
                 "Snapshot root must be independent of source directory")
        output.mkdir(parents=True, exist_ok=True)
        _no_links(output)
        candidate = output / uuid.uuid4().hex
        _no_links(candidate)
        candidate.mkdir(exist_ok=False)
        folder = candidate
        result.update(snapshot_directory=str(folder), project_path=str(folder / source.name))
        for path, raw in zip(sources, originals):
            _write_new(folder / path.name, raw)
        # Re-read every source and copy after all copying, against pre-copy bytes.
        for index, (path, raw) in enumerate(zip(sources, originals)):
            source_hash, copy_hash = _sha(_read(path)), _sha(_read(folder / path.name))
            expected = _sha(raw)
            _require(source_hash == copy_hash == expected, "Source/copy changed: " + path.name)
            result["files"].append({"role": "project" if index == 0 else "document",
                                    "source": str(path), "copy": str(folder / path.name),
                                    "relative_path": path.name, "bytes": len(raw),
                                    "source_sha256": expected, "source_reread_sha256": source_hash,
                                    "copy_sha256": copy_hash})
        result.update(status="completed", source_unchanged=True, document_count=6, file_count=7)
        _write_new(folder / "manifest.json", (json.dumps(result, indent=2) + "\n").encode("utf-8"))
        return result
    except (OSError, ValueError, configparser.Error) as error:
        result.update(status="failed", source_unchanged=False, errors=[str(error)])
        if folder is not None:
            try:
                _write_new(folder / "manifest.json", (json.dumps(result, indent=2) + "\n").encode("utf-8"))
            except (OSError, ValueError) as manifest_error:
                result["errors"].append("Failure manifest could not be written: " + str(manifest_error))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    result = snapshot_project()
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
