"""Kerit entry point: install pinned dependencies in a private, rootless environment."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('Select the Python 3.12 image in the hosting panel, or build the repository Dockerfile. Then start python main.py.')
    environment = root / '.venv'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        try:
            venv.EnvBuilder(with_pip=True).create(environment)
        except Exception:
            raise SystemExit('The host Python image must provide venv/ensurepip and a writable repository folder. Select a complete Python 3.12 image; root access is not needed.') from None
    manifest = root / 'requirements.txt'
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    marker = environment / '.offhours-requirements'
    if not marker.exists() or marker.read_text().strip() != digest:
        subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                        '--no-cache-dir', '--only-binary=:all:', '-r', str(manifest)], check=True)
        marker.write_text(digest)
    if '--install-only' in sys.argv:
        return
    env = os.environ.copy()
    env['PATH'] = str(python.parent) + os.pathsep + env.get('PATH', '')
    env['PYTHONUNBUFFERED'] = '1'
    os.execve(str(python), [str(python), str(root / 'bot.py')], env)


if __name__ == '__main__':
    main()
