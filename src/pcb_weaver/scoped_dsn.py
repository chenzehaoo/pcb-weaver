"""Split native DSN classes for target selection without altering physical rules."""
from copy import deepcopy
from pathlib import Path
import re

import sexpdata

from .repair_dsn import _parse, _serialize, _children, _one, _validate_rules
from .storage import digest


def prepare(source, output, nets):
    source, output = Path(source), Path(output)
    if (not isinstance(nets, list) or not 1 <= len(nets) <= 8 or
            any(not isinstance(n, str) or not n for n in nets) or len(set(nets)) != len(nets)):
        raise ValueError("Scoped routing requires 1-8 unique nets")
    if output.exists() or source.resolve() == output.resolve():
        raise ValueError("Scoped DSN output must be new")
    sha = digest(source)
    tree = _parse(source.read_text(encoding="utf-8"))
    original = deepcopy(tree)
    network = _one(tree, "network")
    classes = _children(network, "class")
    declared = {str(n[1]) for n in _children(network, "net")}
    if not set(nets) <= declared or not classes:
        raise ValueError("Requested nets must exactly match native DSN declarations")
    ignored, selected, mapping, seen = [], [], {}, set()
    for i, group in enumerate(classes):
        name = str(group[1])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in ignored or name.startswith("pcb_weaver_target_"):
            raise ValueError("Ambiguous or unsupported class name for ignore-net-class CLI")
        members = [n for n in group[2:] if not isinstance(n, list)]
        names = list(map(str, members))
        if len(names) != len(set(names)) or seen.intersection(names) or not set(names) <= declared:
            raise ValueError("DSN net classes must have unique, declared membership")
        seen.update(names)
        rules = [n for n in group[2:] if isinstance(n, list)]
        if any(str(n[0]) not in {"rule", "circuit", "layer_rule"} for n in rules):
            raise ValueError("Unsupported class rule semantics")
        _validate_rules(group)
        chosen = [n for n in members if str(n) in nets]
        ignored.append(name)
        if chosen:
            target_name = "pcb_weaver_target_" + str(i)
            selected.append([sexpdata.Symbol("class"), sexpdata.Symbol(target_name), *chosen, *deepcopy(rules)])
            mapping.update({str(n): {"source_class": name, "routing_class": target_name} for n in chosen})
            group[2:] = [n for n in group[2:] if isinstance(n, list) or str(n) not in nets]
    if seen != declared or set(mapping) != set(nets):
        raise ValueError("Every native net must have one explicit class")
    network.extend(selected)
    declaration = _one(_one(tree, "parser"), "string_quote")
    payload = _serialize(tree, declaration)
    if _parse(payload) != tree:
        raise ValueError("Scoped DSN serialization changed semantics")
    # Restore only class membership/clone changes and prove all other AST content identical.
    restored = deepcopy(tree)
    restored_network = _one(restored, "network")
    restored_network[:] = [n for n in restored_network if not (isinstance(n, list) and n and str(n[0]) == "class")]
    old_network = _one(original, "network")
    old_nonclasses = [n for n in old_network if not (isinstance(n, list) and n and str(n[0]) == "class")]
    if restored_network != old_nonclasses:
        raise ValueError("Non-class network content changed")
    restored_network[:] = deepcopy(old_network)
    if restored != original or digest(source) != sha:
        raise ValueError("DSN content outside class split changed")
    with output.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    return {"source_sha256": sha, "output_sha256": digest(output), "target_nets": nets,
            "ignore_classes": ignored, "class_mapping": mapping, "physical_rules_preserved": True,
            "pins_placements_and_copper_preserved": True, "manufacturing_authorized": False,
            "runtime_scope_verified": False}
