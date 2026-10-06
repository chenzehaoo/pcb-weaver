"""Generate a local MCP client entry for the Altium developer beta."""
import argparse
import json
from pathlib import Path
import sys

from altium_beta import Beta


def generate(project_roots, exe, run_root, output):
    beta = Beta(project_roots, run_root, exe)
    if not beta.exe.is_file():
        raise ValueError('Altium executable not found')
    if not Path(sys.executable).is_file():
        raise ValueError('Python interpreter not found')
    output = Path(output).absolute()
    if output.exists():
        raise ValueError('Configuration already exists; choose another output path')
    if not output.parent.is_dir():
        raise ValueError('Configuration parent directory missing')
    server = Path(__file__).with_name('altium_beta_mcp.py').absolute()
    config = {'mcpServers': {'altium-developer-beta': {
        'command': sys.executable, 'args': [str(server)],
        'env': {'ALTIUM_BETA_EXE': str(beta.exe),
                'ALTIUM_BETA_PROJECT_ROOTS': ';'.join(str(r) for r in beta.roots),
                'ALTIUM_BETA_RUN_ROOT': str(beta.run_root)}}}}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return {'config': str(output), 'project_roots': config['mcpServers']['altium-developer-beta']['env']['ALTIUM_BETA_PROJECT_ROOTS'],
            'python': sys.executable, 'altium': str(beta.exe)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', action='append', required=True)
    parser.add_argument('--altium-exe', required=True)
    parser.add_argument('--run-root', default=str(Path(__file__).resolve().parents[1] / 'docs/validation/altium-beta'))
    parser.add_argument('--output', default=str(Path(__file__).resolve().parents[1] / 'altium-beta.mcp.json'))
    args = parser.parse_args()
    print(json.dumps(generate(args.project_root, args.altium_exe, args.run_root, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
