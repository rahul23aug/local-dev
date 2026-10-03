#!/usr/bin/env python3
"""Install project-private optional backends; no global changes or services."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
import tarfile
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]
PWSH_VERSION = '7.6.6'
PWSH_SHA256 = '924829e54c983648f6f1419a2dc7f9433c861b2fb5bd57736ff096c24f133729'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--powershell', action='store_true', help='Install verified Linux ARM64 PowerShell')
    args = parser.parse_args()
    environment = ROOT / '.venv'
    if not (environment / 'bin/python').exists(): venv.create(environment, with_pip=True)
    subprocess.run([str(environment/'bin/python'), '-m', 'pip', 'install', '-r',
                    str(ROOT/'requirements-tools.txt')], check=True)
    if args.powershell:
        if platform.system() != 'Linux' or platform.machine() not in {'aarch64', 'arm64'}:
            parser.error('This pinned PowerShell archive is Linux ARM64 only; install native pwsh separately')
        directory = ROOT / '.tools'
        directory.mkdir(exist_ok=True, mode=0o700)
        archive = directory / 'powershell.tar.gz'
        url = ('https://github.com/PowerShell/PowerShell/releases/download/v' + PWSH_VERSION
               + '/powershell-' + PWSH_VERSION + '-linux-arm64.tar.gz')
        with urllib.request.urlopen(url, timeout=60) as remote, archive.open('wb') as output:
            total = 0
            while chunk := remote.read(1024*1024):
                total += len(chunk)
                if total > 100*1024*1024: raise ValueError('Archive exceeds download budget')
                output.write(chunk)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != PWSH_SHA256:
            raise ValueError('PowerShell checksum mismatch; not extracting')
        destination = directory / 'pwsh'
        if destination.exists(): raise ValueError('Private PowerShell directory already exists; not overwriting')
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            if any(PurePosixPath(m.name).is_absolute() or '..' in PurePosixPath(m.name).parts
                   or not (m.isfile() or m.isdir()) for m in members):
                raise ValueError('Unsafe archive member')
            bundle.extractall(destination)
        (destination/'pwsh').chmod(0o755)
    print('Private backends installed. Run ./local-coder tools to inspect availability.')


if __name__ == '__main__': main()
