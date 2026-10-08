"""Pack the npm packages for the current platform (after scripts/build_exe.py --onedir).

    .venv/bin/python scripts/build_npm.py     # → dist/npm/ncglobe-<ver>.tgz + alex870521-ncglobe-<platform>-<ver>.tgz

Layout follows esbuild/Biome: `ncglobe` holds only bin/ncglobe.js; each platform package
(`@alex870521/ncglobe-darwin-arm64`, `@alex870521/ncglobe-win32-x64`, …) holds the PyInstaller folder build and declares
os/cpu, so npm installs just the one matching the machine. Run this on each platform; every run
lists all platforms it has packed so far as optionalDependencies of the main package.

CI (.github/workflows/release.yml) runs it in two steps: `--platform-only` on each OS, then
`--main-only darwin-arm64 darwin-x64 win32-x64` once, so the main package lists every platform.

Local install test (no publishing):
    npm i -g --prefix /tmp/ncg dist/npm/alex870521-ncglobe-<platform>-<ver>.tgz dist/npm/ncglobe-<ver>.tgz
"""
import argparse
import json
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 平台套件放在帳號範圍下:0.1.0 不帶範圍的 ncglobe-win32-x64 被 npm 的垃圾套件檢查擋下
SCOPE = '@alex870521'
ARCH = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'amd64': 'x64'}
META = {'license': 'MIT', 'author': 'Chih-Yu Chan',
        'repository': {'type': 'git', 'url': 'git+https://github.com/Alex870521/ncglobe.git'},
        'homepage': 'https://github.com/Alex870521/ncglobe#readme'}


def npm_platform() -> tuple[str, str]:
    os_ = {'darwin': 'darwin', 'win32': 'win32', 'linux': 'linux'}[sys.platform]
    return os_, ARCH[platform.machine().lower()]


def pack(src: Path, out: Path) -> Path:
    npm = shutil.which('npm') or sys.exit('npm not found')
    name = subprocess.run([npm, 'pack', '--pack-destination', str(out), '--json'], cwd=src, check=True,
                          capture_output=True, text=True).stdout
    return out / json.loads(name)[0]['filename']


def pack_platform(version: str, out: Path, stage: Path) -> None:
    build = ROOT / 'dist' / 'ncglobe'
    exe = build / ('ncglobe.exe' if sys.platform == 'win32' else 'ncglobe')
    if not exe.exists():
        sys.exit('no folder build; run scripts/build_exe.py --onedir first')
    os_, cpu = npm_platform()
    plat = stage / f'ncglobe-{os_}-{cpu}'
    if plat.exists():
        shutil.rmtree(plat)
    shutil.copytree(build, plat / 'ncglobe', symlinks=False)   # npm 打包會丟掉符號連結(PyInstaller 在 macOS 用它連 Python 與 .dylib)
    (plat / 'package.json').write_text(json.dumps({
        'name': f'{SCOPE}/ncglobe-{os_}-{cpu}', 'version': version,
        'description': f'ncglobe executable for {os_}-{cpu} (installed by the ncglobe package)', 'os': [os_], 'cpu': [cpu],
        'files': ['ncglobe'], **META}, indent=2))
    print('→', pack(plat, out))


def pack_main(version: str, out: Path, stage: Path, platforms: list[str]) -> None:
    main_pkg = stage / 'ncglobe'
    if main_pkg.exists():
        shutil.rmtree(main_pkg)
    shutil.copytree(ROOT / 'npm' / 'ncglobe', main_pkg)
    shutil.copy2(ROOT / 'README.md', main_pkg / 'README.md')
    meta = json.loads((main_pkg / 'package.json').read_text())
    meta['version'] = version
    meta['optionalDependencies'] = {f'{SCOPE}/ncglobe-{p}': version for p in platforms}
    (main_pkg / 'package.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print('→', pack(main_pkg, out))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--platform-only', action='store_true', help='pack only this machine\'s platform package')
    g.add_argument('--main-only', nargs='+', metavar='PLATFORM', help='pack only the main package listing these platforms')
    a = ap.parse_args()
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    out = ROOT / 'dist' / 'npm'
    out.mkdir(parents=True, exist_ok=True)
    stage = ROOT / 'build' / 'npm'
    stage.mkdir(parents=True, exist_ok=True)
    if not a.main_only:
        pack_platform(version, out, stage)
    if not a.platform_only:
        platforms = a.main_only or sorted(p.name.removeprefix('ncglobe-') for p in stage.glob('ncglobe-*-*') if p.is_dir())
        pack_main(version, out, stage, platforms)


if __name__ == '__main__':
    main()
