"""Portable private-state paths and command prefixes; never shell commands."""
import json
import os
from pathlib import Path
import platform
import shutil
import sys


def default_state_dir(system=None, environment=None):
    env = os.environ if environment is None else environment
    if env.get('QUOTA_SENTINEL_STATE_DIR'):
        return Path(env['QUOTA_SENTINEL_STATE_DIR']).expanduser()
    system = platform.system() if system is None else system
    home = Path(env.get('USERPROFILE' if system == 'Windows' else 'HOME') or Path.home())
    if system == 'Darwin':
        return home / 'Library/Application Support/Quota-Sentinel'
    if system == 'Windows':
        return Path(env.get('LOCALAPPDATA') or home / 'AppData/Local') / 'Quota-Sentinel'
    if system == 'Linux':
        return Path(env.get('XDG_STATE_HOME') or home / '.local/state') / 'quota-sentinel'
    raise ValueError('unsupported operating system')


NPM_PACKAGES = {'codex': '@openai/codex', 'pi': '@earendil-works/pi-coding-agent'}


def _node():
    executable = shutil.which('node')
    if not executable:
        raise ValueError('Node.js is required for the selected launcher')
    return executable


def resolve_launcher(name, explicit=None):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('invalid launcher name')
    found = str(explicit) if explicit is not None else shutil.which(name)
    if not found or '\x00' in found:
        raise ValueError('selected executable is unavailable: ' + name)
    # Preserve executable symlinks: resolving a virtualenv Python would select
    # its base interpreter and lose the installed environment.
    path = Path(found).expanduser().absolute()
    if not path.is_file():
        raise ValueError('selected executable is unavailable: ' + name)
    suffix = path.suffix.lower()
    if suffix == '.py':
        return sys.executable, str(path.resolve())
    if suffix in ('.mjs', '.js'):
        return _node(), str(path)
    if suffix in ('.cmd', '.bat', '.ps1'):
        package_name = NPM_PACKAGES.get(name)
        if not package_name:
            raise ValueError('unsupported shell launcher; configure an executable or Python helper')
        package = path.parent / 'node_modules' / package_name
        try:
            document = json.loads((package / 'package.json').read_text(encoding='utf-8'))
            entry = document['bin']
            entry = entry.get(name) if isinstance(entry, dict) else entry
            if document['name'] != package_name or not isinstance(entry, str):
                raise ValueError()
            target = (package / entry).resolve()
            target.relative_to(package.resolve())
            if not target.is_file() or target.suffix not in ('.js', '.mjs'):
                raise ValueError()
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError('invalid selected npm launcher manifest') from exc
        return _node(), str(target)
    return (str(path),)
