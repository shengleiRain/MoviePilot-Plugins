"""Build the exact MoviePilot Release ZIP from the checked-out plugin sources."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / 'plugins.v2' / 'p123sync'
SOURCE_FILES = ('__init__.py', 'p123_client.py', 'sync_engine.py', 'sync_journal.py', 'auto_monitor.py', 'README.md', 'requirements.txt')


def build(output: Path) -> str:
    meta = json.loads((ROOT / 'package.v2.json').read_text(encoding='utf-8'))['P123Sync']
    version = meta['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not meta.get('release'):
        raise ValueError('invalid plugin version or Release is not enabled')
    tree = ast.parse((PLUGIN / '__init__.py').read_text(encoding='utf-8'))
    plugin_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'P123Sync')
    declared = next(ast.literal_eval(node.value) for node in plugin_class.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'plugin_version' for t in node.targets))
    if declared != version:
        raise ValueError('market and plugin versions differ')
    dist_rel = Path('dist') / ('v' + version.replace('.', '_'))
    dist = PLUGIN / dist_rel
    files = [(PLUGIN / name, name) for name in SOURCE_FILES]
    files.extend((file, (dist_rel / file.name).as_posix()) for file in sorted(dist.iterdir()) if file.is_file())
    if not (dist / 'remoteEntry.js').is_file():
        raise ValueError('missing federated entry')
    for source, _ in files:
        if not source.is_file():
            raise ValueError(f'missing required file: {source.name}')
        if source.suffix == '.js':
            content = source.read_text(encoding='utf-8')
            refs = re.findall(r'["\'](?:\./)?((?:__federation_|_plugin-vue_|utils-)[^"\']+\.(?:js|css))["\']', content)
            for name in refs:
                if not (dist / name).is_file():
                    raise ValueError(f'{source.name} refers to missing {name}')
    if output.exists():
        raise FileExistsError(f'output already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in files:
            archive.write(source, name)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('release archive is corrupt')
        if not {name for _, name in files}.issubset(archive.namelist()):
            raise ValueError('release archive is incomplete')
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(f'{args.output} SHA256 {build(args.output)}')
