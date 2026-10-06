"""Generate machine-local settings for the Altium developer-beta MCP and worker."""
import argparse
import json
import os
from pathlib import Path
import sys

from altium_beta import Beta


ROOT = Path(__file__).resolve().parents[1]


def generate(project_roots, exe, run_root, service_root, output, python_exe=None):
    beta = Beta(project_roots, run_root, exe or None)
    if exe and not beta.exe.is_file():
        raise ValueError('Altium executable not found')
    service = Path(service_root).resolve()
    if any(service.is_relative_to(root) or root.is_relative_to(service) for root in beta.roots):
        raise ValueError('Service database must be independent of source project roots')
    if service.is_relative_to(beta.run_root) or beta.run_root.is_relative_to(service):
        raise ValueError('Service database and native evidence roots must be independent')
    interpreter = Path(python_exe or sys.executable).resolve(strict=True)
    server = Path(__file__).with_name('altium_service_mcp.py').resolve(strict=True)
    target = Path(output).resolve()
    if not target.parent.is_dir():
        raise ValueError('Configuration parent directory missing')
    env = {
        'ALTIUM_BETA_EXE': str(beta.exe) if exe else '',
        'ALTIUM_BETA_PROJECT_ROOTS': os.pathsep.join(str(root) for root in beta.roots),
        'ALTIUM_BETA_RUN_ROOT': str(beta.run_root),
        'ALTIUM_SERVICE_ROOT': str(service),
        'ALTIUM_SERVICE_NATIVE_LAUNCH': '0',
    }
    config = {'mcpServers': {'altium-local-developer-beta': {
        'command': str(interpreter), 'args': [str(server)], 'env': env,
    }}}
    with target.open('x', encoding='utf-8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return {'config': str(target), 'server': 'altium-local-developer-beta',
            'project_roots': env['ALTIUM_BETA_PROJECT_ROOTS'],
            'native_launch_enabled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', action='append', required=True)
    parser.add_argument('--altium-exe', default='')
    parser.add_argument('--run-root', default=str(ROOT / 'docs/validation/altium-beta'))
    parser.add_argument('--service-root', default=str(ROOT / 'data/altium-service'))
    parser.add_argument('--output', default=str(ROOT / 'altium-service.mcp.json'))
    args = parser.parse_args()
    print(json.dumps(generate(args.project_root, args.altium_exe, args.run_root,
                              args.service_root, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
