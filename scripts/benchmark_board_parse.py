"""Compare cold and content-cached PCB reads without changing the source board."""
import argparse
from pathlib import Path
from time import perf_counter
from pcb_weaver import board
from pcb_weaver.repair_geometry import _read
from pcb_weaver.storage import digest,write_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    sha = digest(args.source)
    measurements = {}
    for mode in ("cold","content_cached"):
        board._cached_ast.cache_clear()
        start = perf_counter()
        for _ in range(3):
            for reader in (_read,board.read_board):
                if mode == "cold":
                    board._cached_ast.cache_clear()
                reader(args.source)
        measurements[mode] = {"seconds":perf_counter()-start,"reads":6}
    assert digest(args.source) == sha
    result = {"source_sha256":sha,"read_only":True,"measurements":measurements}
    write_json(args.output,result)
    print(result)
