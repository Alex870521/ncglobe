"""Build a single-file ncglobe executable for the current platform (PyInstaller).

    .venv/bin/python scripts/build_exe.py            # → dist/ncglobe (macOS) / dist/ncglobe.exe (Windows)
    .venv/bin/python scripts/build_exe.py --onedir   # folder build: bigger on disk, but starts faster
    .venv/bin/python scripts/build_exe.py --app      # macOS: dist/ncglobe.app (double-click; Finder "Open With")

Bundled so the executable works offline on first launch: Plotly and globe.gl, the Natural Earth
coastlines/land used by the maps, and GSHHG levels l/i/h. GSHHG full (f, ~190 MB) is left out;
`ncglobe --install-gshhg` still downloads it into the user's cartopy data folder.
"""
import argparse
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from ncglobe import server  # noqa: E402

NE = ([('physical', f'ne_{r}_coastline') for r in ('10m', '50m', '110m')] + [('physical', 'ne_110m_land')]
      + [('cultural', f'ne_{r}_admin_0_boundary_lines_land') for r in ('10m', '50m', '110m')])   # 國界圖層
GSHHG_LEVELS = ('l', 'i', 'h')


def collect(bundle: Path) -> None:
    import cartopy
    from cartopy.io import shapereader
    if bundle.exists():
        shutil.rmtree(bundle)
    (bundle / 'vendor').mkdir(parents=True)
    for name in server.VENDOR:
        (bundle / 'vendor' / name).write_bytes(server.vendor(name))   # cached copy, hash-checked on download
    data = Path(cartopy.config['data_dir'])
    for category, name in NE:
        shp = Path(shapereader.natural_earth(name.split('_', 2)[1], category, name.split('_', 2)[2]))
        dest = bundle / 'cartopy' / shp.relative_to(data).parent
        dest.mkdir(parents=True, exist_ok=True)
        for f in shp.parent.glob(shp.stem + '.*'):
            shutil.copy2(f, dest / f.name)
    gshhs = data / 'shapefiles' / 'gshhs'
    if not (gshhs / 'l' / 'GSHHS_l_L1.shp').exists():
        sys.exit('GSHHG is not installed here; run `ncglobe --install-gshhg` first')
    for lv in GSHHG_LEVELS:
        shutil.copytree(gshhs / lv, bundle / 'cartopy' / 'shapefiles' / 'gshhs' / lv)
    shutil.copy2(gshhs / 'LICENSE.TXT', bundle / 'cartopy' / 'shapefiles' / 'gshhs' / 'LICENSE.TXT')


DOC_EXTS = ['nc', 'nc4', 'cdf', 'h5', 'hdf5', 'he5', 'hdf', 'h4', 'he4']


def make_icns(png: Path, out: Path, fill: float = 0.96) -> Path:
    """logo PNG → macOS .icns(sips 縮圖 + iconutil)。

    logo 的圓只佔畫布約 78%,四周透明留白在 Dock 裡看起來偏小:先裁到不透明的範圍,
    再置中放進 1024 方塊、佔 fill 的比例。
    """
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        im = Image.open(png).convert('RGBA')
        im = im.crop(im.getchannel('A').point(lambda a: 255 if a > 8 else 0).getbbox())
        side = round(1024 * fill)
        k = side / max(im.size)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.Resampling.LANCZOS)
        big = Image.new('RGBA', (1024, 1024))
        big.paste(im, ((1024 - im.width) // 2, (1024 - im.height) // 2), im)
        png = Path(tmp) / 'icon-1024.png'
        big.save(png)
        iconset = Path(tmp) / 'ncglobe.iconset'
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
                subprocess.run(['sips', '-z', str(px), str(px), str(png), '--out', str(iconset / name)],
                               check=True, capture_output=True)
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(out)], check=True)
    return out


def finish_app(app: Path) -> None:
    """Info.plist:版本、可用來打開的檔案類型、允許連本機 http;改完重新簽章。"""
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    plist = app / 'Contents' / 'Info.plist'
    info = plistlib.loads(plist.read_bytes())
    info.update({
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},   # WKWebView 連 http://127.0.0.1
        'NSHumanReadableCopyright': 'ncglobe',
        'CFBundleShortVersionString': version, 'CFBundleVersion': version,
        'CFBundleDisplayName': 'ncglobe',
        'CFBundleDocumentTypes': [{
            'CFBundleTypeName': 'NetCDF / HDF data', 'CFBundleTypeRole': 'Viewer',
            'LSHandlerRank': 'Alternate',              # 出現在「打開方式」,不搶預設程式
            'CFBundleTypeExtensions': DOC_EXTS}],
    })
    plist.write_bytes(plistlib.dumps(info))
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=True, capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--onedir', action='store_true', help='folder build instead of a single file')
    ap.add_argument('--app', action='store_true', help='macOS .app bundle (implies a folder build)')
    a = ap.parse_args()
    if a.app and sys.platform != 'darwin':
        sys.exit('--app is macOS only')
    build = ROOT / 'build'
    bundle = build / 'ncglobe_bundle'
    collect(bundle)
    sep = ';' if sys.platform == 'win32' else ':'
    pkg = ROOT / 'src' / 'ncglobe'
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--name', 'ncglobe',
           '--onedir' if a.onedir or a.app else '--onefile',
           # .app 先建在 build/ 裡再搬進 dist/:Finder 開著 dist/ 時一直重寫 .DS_Store,PyInstaller 會刪不掉舊資料夾
           '--distpath', str(build / 'app-dist' if a.app else ROOT / 'dist'),
           '--workpath', str(build / 'pyinstaller'), '--specpath', str(build),
           '--paths', str(ROOT / 'src'),
           '--add-data', f'{pkg / "static"}{sep}ncglobe/static',
           '--add-data', f'{bundle}{sep}ncglobe_bundle',
           '--collect-data', 'pyproj', '--collect-data', 'cartopy',
           '--exclude-module', 'tkinter', '--exclude-module', 'pytest', '--exclude-module', 'playwright',
           str(pkg / '__main__.py')]
    if a.app:
        icns = make_icns(pkg / 'static' / 'logo-1024.png', build / 'ncglobe.icns')
        cmd[cmd.index('--name'):cmd.index('--name')] = [
            '--windowed', '--icon', str(icns), '--osx-bundle-identifier', 'org.ncglobe.viewer']
    elif (pkg / 'static' / 'favicon.ico').exists():
        cmd[cmd.index('--name'):cmd.index('--name')] = ['--icon', str(pkg / 'static' / 'favicon.ico')]
    subprocess.run(cmd, check=True)
    if a.app:
        app = ROOT / 'dist' / 'ncglobe.app'
        if app.exists():
            shutil.rmtree(app)
        app.parent.mkdir(exist_ok=True)
        shutil.move(str(build / 'app-dist' / 'ncglobe.app'), app)
        finish_app(app)
        size = sum(f.stat().st_size for f in app.rglob('*') if f.is_file() and not f.is_symlink())
        print(f'\n→ {app}  ({size / 1e6:.0f} MB)')
        return
    out = ROOT / 'dist' / 'ncglobe'
    size = sum(f.stat().st_size for f in out.rglob('*') if f.is_file()) if out.is_dir() else \
        (out.with_suffix('.exe') if sys.platform == 'win32' else out).stat().st_size
    print(f'\n→ {out}  ({size / 1e6:.0f} MB)')


if __name__ == '__main__':
    main()
