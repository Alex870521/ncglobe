"""產生 docs/使用手冊.md 用的截圖(docs/img/*.png)。

UI 改了就重跑一次,所有圖會照目前的介面重拍:

    .venv/bin/python scripts/doc_screenshots.py                     # --root 指到放範例資料的資料夾
    .venv/bin/python scripts/doc_screenshots.py --root ~/data --only globe composite_day
    .venv/bin/python scripts/doc_screenshots.py --list              # 列出所有場景

前提:
- ncglobe 伺服器已經在跑,而且讀得到 --root(例如 `ncglobe ~/data`)。腳本不會啟動或關掉它。
- 裝了 docs extra(`uv pip install -e '.[docs]'`),Playwright 的 Chromium 已下載。
- --root 底下要有手冊用的範例資料(路徑見下面 DATA);缺檔的場景會跳過並印出原因。

紅框編號是截圖前用 JS 疊上去的,截完就移除,不影響介面本身。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Page, sync_playwright

REPO = Path(__file__).resolve().parents[1]

# 手冊用的範例資料(相對於 --root)
DATA = {
    'l2_day': 'Sentinel-5P/raw/L2_global/NO2___/2026/01/02',      # 一天 14 軌全球 NO2
    'l2_tw': 'Sentinel-5P/raw/L2_global/NO2___/2026/01/02/'
             'S5P_OFFL_L2__NO2____20260102T040222_20260102T054352_42602_03_020901_20260103T203051.nc',   # 經過台灣的那軌
    'l2_month': 'Sentinel-5P/raw/L2/NO2___/2026/01',              # 每天經過台灣的軌道
    'era5': 'ERA5/raw/single_level/2025/era5_sfc_d2m_t2m_20250101_20251231.nc',
    'gems': 'GEMS/raw/NO2/2026/06/GK2_GEMS_L2_20260601_0345_NO2_FC_DPRO_ORI.nc',
    'modis': 'MODIS/raw/MYD04_L2/2025/01/MYD04_L2.A2025024.0550.061.2025027200208.hdf',
}
NO2 = 'nitrogendioxide_tropospheric_column'
TAIWAN = (118, 21.5, 122.6, 26.5)
TAIPEI = (121.52, 25.05)


# ---------------------------------------------------------------- 等待與操作
def wait_idle(page: Page, timeout: float = 240, quiet_ms: int = 900) -> None:
    """等到有資料、「讀取中」消失,而且連續 quiet_ms 都沒再出現(縮放後會再發一次請求)。"""
    page.wait_for_function("typeof S !== 'undefined' && S.data && document.getElementById('busy').hidden", timeout=timeout * 1000)
    t_end = time.time() + timeout
    calm = 0
    while time.time() < t_end:
        busy = page.evaluate("!document.getElementById('busy').hidden || (S.data && S.data.kind === 'swath' && !S.image && !S.pv)")
        calm = 0 if busy else calm + 150
        if calm >= quiet_ms:
            return
        page.wait_for_timeout(150)
    raise TimeoutError('畫面一直在讀取中')


class TileWatch:
    """地球儀的圖磚是 <img> 直接請求,不經過「讀取中」;自己數還沒回來的 /api/tile。"""

    def __init__(self, page: Page):
        self.pending = set()
        page.on('request', lambda r: self.pending.add(r) if '/api/tile' in r.url else None)
        page.on('requestfinished', lambda r: self.pending.discard(r))
        page.on('requestfailed', lambda r: self.pending.discard(r))

    def wait(self, page: Page, timeout: float = 120) -> None:
        t_end = time.time() + timeout
        calm = 0
        page.wait_for_timeout(800)
        while time.time() < t_end:
            calm = 0 if self.pending else calm + 200
            if calm >= 1500:
                return
            page.wait_for_timeout(200)
        raise TimeoutError(f'還有 {len(self.pending)} 張圖磚沒回來')


def open_hash(page: Page, base: str, path: Path, group: str = '', var: str = '', extra: str = '') -> None:
    h = f'f={quote(str(path))}&g={quote(group)}&v={quote(var)}' + (f'&{extra}' if extra else '')
    page.goto(f'{base}/#{h}')
    wait_idle(page)


def lonlat_to_screen(page: Page, lon: float, lat: float) -> tuple[float, float]:
    """平面地圖上某個經緯度在螢幕上的位置(用 Plotly 的座標軸換算)。"""
    return tuple(page.evaluate("""([lon, lat]) => {
        const p = document.getElementById('plot'), fl = p._fullLayout, r = p.getBoundingClientRect();
        return [r.left + fl.xaxis._offset + fl.xaxis.l2p(lon), r.top + fl.yaxis._offset + fl.yaxis.l2p(lat)];
    }""", [lon, lat]))


def click_map(page: Page, lon: float, lat: float) -> None:
    x, y = lonlat_to_screen(page, lon, lat)
    page.mouse.click(x, y)
    page.wait_for_function("!document.getElementById('insp').hidden && !document.getElementById('inspBody').textContent.startsWith('讀取中')",
                           timeout=60000)
    page.mouse.move(2, 890)   # 滑鼠移開,不留下讀值提示框
    page.wait_for_timeout(600)


def zoom_to(page: Page, box: tuple) -> None:
    """跟「跳到」選單一樣:設定視野再重讀。"""
    page.evaluate("b => { S.bbox = b; S.lastRange = b.slice(); loadSlice(false); }", list(box))
    page.wait_for_timeout(300)
    wait_idle(page)


def run_composite(page: Page) -> None:
    """按「開始合成」,等它做完並回到地圖;印出伺服器算了幾秒(手冊裡的實測數字)。"""
    page.click('#cmStart')
    page.wait_for_function("S.composite && document.getElementById('compModal').hidden", timeout=900000)
    wait_idle(page)
    st = page.evaluate("api('/api/composite/status', { id: S.composite.id }, { quiet: true })")
    print(f"  合成 {st['used']}/{st['total']} 個檔、{st['n_points']:,} 個有效像素、伺服器 {st['seconds']} 秒")


def mark(page: Page, items: list) -> None:
    """疊上紅框 + 編號。items:(CSS 選擇器 或 [x, y, w, h], 編號文字, 標籤位置 'tl'/'tr'/'bl'/'br')"""
    page.evaluate("""items => {
        for (const [target, label, where] of items) {
            let r;
            if (typeof target === 'string') {
                const el = document.querySelector(target);
                if (!el) continue;
                const b = el.getBoundingClientRect();
                r = [b.left, b.top, b.width, b.height];
            } else r = target;
            const box = document.createElement('div');
            box.className = 'docmark';
            Object.assign(box.style, { position: 'fixed', left: r[0] + 'px', top: r[1] + 'px', width: r[2] + 'px', height: r[3] + 'px',
                border: '2.5px solid #e0262b', borderRadius: '6px', zIndex: 9998, pointerEvents: 'none', boxSizing: 'border-box' });
            const tag = document.createElement('div');
            tag.className = 'docmark';
            tag.textContent = label;
            // 編號放在框的角上(一半在框外),不蓋住框裡的字
            const w = where || 'tl';
            Object.assign(tag.style, { position: 'fixed', zIndex: 9999, pointerEvents: 'none', background: '#e0262b', color: '#fff',
                font: '700 12px/20px -apple-system, sans-serif', minWidth: '20px', height: '20px', padding: '0 5px', borderRadius: '10px',
                textAlign: 'center', boxSizing: 'border-box', boxShadow: '0 0 0 2px #fff',
                left: Math.max(2, Math.min(innerWidth - 22, (w.includes('r') ? r[0] + r[2] - 10 : r[0] - 10))) + 'px',
                top: Math.max(2, Math.min(innerHeight - 22, (w.includes('b') ? r[1] + r[3] - 10 : r[1] - 10))) + 'px' });
            document.body.append(box, tag);
        }
    }""", [[t, l, w] for t, l, *rest in items for w in [rest[0] if rest else 'tl']])


def unmark(page: Page) -> None:
    page.evaluate("document.querySelectorAll('.docmark').forEach(e => e.remove())")


def rect(page: Page, selector: str) -> list:
    return page.evaluate("s => { const b = document.querySelector(s).getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; }", selector)


class Shooter:
    def __init__(self, out: Path):
        self.out = out
        self.made: list[str] = []

    def __call__(self, page: Page, name: str, clip: list | dict | None = None, selector: str | None = None) -> None:
        path = self.out / f'{name}.png'
        if selector:
            page.locator(selector).screenshot(path=path)
        else:
            if isinstance(clip, list):
                clip = {'x': clip[0], 'y': clip[1], 'width': clip[2], 'height': clip[3]}
            page.screenshot(path=path, clip=clip)
        unmark(page)
        self.made.append(path.name)
        print(f'  ✓ {path.relative_to(REPO) if path.is_relative_to(REPO) else path}')


# ---------------------------------------------------------------- 場景
def scene_overview(page, base, root, shot):
    """總覽:L2 放大到台灣 + 點選面板,標出各區。"""
    open_hash(page, base, root / DATA['l2_tw'], 'PRODUCT', NO2)
    zoom_to(page, TAIWAN)
    click_map(page, *TAIPEI)
    wait_idle(page)
    mark(page, [('aside', '1', 'tr'), ('#bar1', '2', 'tr'), ('#bar2', '3', 'tr'), ('#plotwrap', '4', 'tr'), ('#insp', '5', 'tr'), ('#status', '6', 'tr')])
    shot(page, '01_overview')


def scene_browse(page, base, root, shot):
    """側欄:從根目錄點進資料夾,檔案用觀測時間列出。"""
    page.goto(f'{base}/')
    page.wait_for_selector('#side .item')
    page.wait_for_timeout(400)
    page.evaluate("p => openDir(p)", str(root / DATA['l2_day']))
    page.wait_for_function("S.dir && S.dir.files.length > 0")
    page.wait_for_timeout(500)
    shot(page, '02b_folder', clip=[0, 0, 320, 620])
    # 看著台灣回到資料夾:經過目前畫面的檔標 ●
    open_hash(page, base, root / DATA['l2_tw'], 'PRODUCT', NO2)
    zoom_to(page, TAIWAN)
    page.click('#side .item:has-text("回資料夾")')
    page.wait_for_function("S.dir && S.passes && S.dir.files.every(f => f.path in S.passes)", timeout=180000)
    page.wait_for_timeout(300)
    shot(page, '02c_passes', clip=[0, 0, 320, 620])


def scene_l2(page, base, root, shot):
    """打開一個 L2 檔、跳到台灣。"""
    open_hash(page, base, root / DATA['l2_tw'], 'PRODUCT', NO2)
    mark(page, [('#side .item.sel', '1', 'tr'), ('#fileName', '2'), ('#quickBox', '3', 'tr')])
    shot(page, '03_l2_orbit')
    page.select_option('#quick', 'taiwan')
    page.wait_for_timeout(300)
    wait_idle(page)
    mark(page, [('#status', '1')])
    shot(page, '04_l2_taiwan')


def scene_inspect(page, base, root, shot):
    """在台灣點一下看值。"""
    open_hash(page, base, root / DATA['l2_tw'], 'PRODUCT', NO2)
    zoom_to(page, TAIWAN)
    click_map(page, *TAIPEI)
    wait_idle(page)
    shot(page, '05_inspect', clip=[320, 0, 1120, 900])
    # qa 被篩掉的像素也點得到,面板會說明
    b = rect(page, '#insp')
    h = page.evaluate("document.querySelector('#inspBody').scrollHeight + document.querySelector('.insp-head').offsetHeight")
    shot(page, '05b_inspect_panel', clip=[b[0], b[1], b[2], min(b[3], h + 8)])


def scene_era5(page, base, root, shot):
    """規則網格 + 時間維度:時間軸、播放、點選後的時間序列。"""
    open_hash(page, base, root / DATA['era5'], '', 't2m', 'i=' + quote('{"valid_time":4500}'))
    click_map(page, 121.0, 23.5)
    wait_idle(page)
    mark(page, [('#dims', '1'), ('#series', '2')])
    shot(page, '06_era5_series')


def scene_globe(page, base, root, shot, tiles):
    open_hash(page, base, root / DATA['l2_day'] / Path(DATA['l2_tw']).name, 'PRODUCT', NO2)
    zoom_to(page, (95, 5, 150, 50))
    page.select_option('#proj', 'ortho')
    page.wait_for_function("!document.getElementById('earth').hidden")
    tiles.wait(page)
    page.wait_for_timeout(800)
    shot(page, '07_globe', clip=[320, 0, 1120, 900])


def scene_colour(page, base, root, shot):
    """色階工具列。"""
    open_hash(page, base, root / DATA['l2_tw'], 'PRODUCT', NO2)
    zoom_to(page, TAIWAN)
    mark(page, [('#cmap', '1'), ('#crev', '2'), ('#vmin', '3'), ('#autoP', '4'), ('#autoM', '5'), ('#lockR', '6'), ('#log', '7'), ('#qaBox', '8')])
    b = rect(page, '#bar2')
    shot(page, '08_colour_bar', clip=[b[0], b[1] - 2, b[2], b[3] + 4])
    # 鎖定範圍:0 – 1e-4,換到下一軌顏色可比
    page.fill('#vmin', '0')
    page.fill('#vmax', '1e-4')
    page.press('#vmax', 'Enter')
    page.wait_for_timeout(300)
    wait_idle(page)
    shot(page, '09_manual_range', clip=[320, 0, 1120, 900])


def scene_composite_day(page, base, root, shot):
    """同一天 14 軌拼全球。"""
    open_hash(page, base, root / DATA['l2_day'] / Path(DATA['l2_tw']).name, 'PRODUCT', NO2)
    page.click('#compBtn')
    page.wait_for_selector('#compModal:not([hidden])')
    page.click('#cmAll')
    page.select_option('#cmRegion', 'global')
    page.wait_for_timeout(300)
    shot(page, '10_comp_dialog', selector='#compModal .modal')
    run_composite(page)
    mark(page, [('#compBar', '1'), ('#compMode', '2', 'tr'), ('#compRes', '3', 'tr'), ('#compFilesBtn', '4', 'tr'), ('#compNcBtn', '5', 'tr')])
    shot(page, '11_comp_global')
    page.click('#compFilesBtn')
    page.wait_for_timeout(400)
    shot(page, '12_comp_files', clip=[320, 0, 1120, 520])


def scene_composite_days(page, base, root, shot):
    """多日平均(台灣):觀測日期篩選 + 每天等權。"""
    f = sorted((root / DATA['l2_month']).glob('S5P_*____20260102T*.nc'))[0]
    open_hash(page, base, f, 'PRODUCT', NO2)
    zoom_to(page, TAIWAN)
    page.click('#compBtn')
    page.wait_for_selector('#compModal:not([hidden])')
    page.select_option('#cmWeight', 'day')
    page.fill('#cmFrom', '2026-01-01')
    page.fill('#cmTo', '2026-01-10')
    page.dispatch_event('#cmTo', 'input')
    page.click('#cmAll')
    page.wait_for_timeout(300)
    shot(page, '13_comp_days_dialog', selector='#compModal .modal')
    run_composite(page)
    shot(page, '14_comp_days_mean')
    page.select_option('#compMode', 'count')
    page.wait_for_timeout(300)
    wait_idle(page)
    page.click('#autoM')   # 覆蓋數是整數,用「全距」才看得到最少的格子
    page.wait_for_timeout(300)
    wait_idle(page)
    shot(page, '15_comp_days_count', clip=[320, 0, 1120, 900])


def scene_diff(page, base, root, shot):
    """相減:1/2 那軌 − 1/3–1/10 平均。"""
    f = sorted((root / DATA['l2_month']).glob('S5P_*____20260102T*.nc'))[0]
    open_hash(page, base, f, 'PRODUCT', NO2)
    zoom_to(page, TAIWAN)
    page.click('#compBtn')
    page.wait_for_selector('#compModal:not([hidden])')
    page.check('input[name=cmKind][value=diff]')
    page.fill('#cmFrom', '2026-01-03')
    page.fill('#cmTo', '2026-01-10')
    page.dispatch_event('#cmTo', 'input')
    page.click('#cmAll')
    run_composite(page)
    shot(page, '16_comp_diff')


def scene_gems(page, base, root, shot):
    open_hash(page, base, root / DATA['gems'], 'Data Fields', 'ColumnAmountNO2Trop')
    shot(page, '17_gems')


def scene_modis(page, base, root, shot):
    open_hash(page, base, root / DATA['modis'], '', '')
    shot(page, '18_modis')


SCENES = {
    'overview': scene_overview, 'browse': scene_browse, 'l2': scene_l2, 'inspect': scene_inspect, 'era5': scene_era5,
    'globe': scene_globe, 'colour': scene_colour, 'composite_day': scene_composite_day,
    'composite_days': scene_composite_days, 'diff': scene_diff, 'gems': scene_gems, 'modis': scene_modis,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', required=True, help='範例資料的根目錄(伺服器要讀得到)')
    ap.add_argument('--url', default='http://127.0.0.1:8765', help='ncglobe 伺服器網址')
    ap.add_argument('--out', default=str(REPO / 'docs' / 'img'))
    ap.add_argument('--only', nargs='*', help='只拍這幾個場景')
    ap.add_argument('--list', action='store_true', help='列出場景後結束')
    args = ap.parse_args()
    if args.list:
        for k, f in SCENES.items():
            print(f'{k:16} {f.__doc__.strip() if f.__doc__ else ""}')
        return
    root, out = Path(args.root), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    missing = [k for k, rel in DATA.items() if not (root / rel).exists()]
    if missing:
        print(f'⚠ {root} 底下缺少範例資料:{", ".join(missing)};用到的場景會失敗', file=sys.stderr)
    shot = Shooter(out)
    failed = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--use-gl=angle', '--enable-webgl', '--ignore-gpu-blocklist'])
        for name in args.only or SCENES:
            fn = SCENES[name]
            # 每個場景一個新頁面:狀態(合成、鎖定範圍、檢視)不會帶到下一個
            page = browser.new_page(viewport={'width': 1440, 'height': 900}, device_scale_factor=1)
            tiles = TileWatch(page)
            t0 = time.time()
            print(f'{name} …')
            try:
                fn(page, args.url, root, shot, tiles) if name == 'globe' else fn(page, args.url, root, shot)
                print(f'  {time.time() - t0:.1f} 秒')
            except Exception as e:   # 一個場景失敗不影響其他場景
                failed.append(name)
                print(f'  ✗ {type(e).__name__}: {e}', file=sys.stderr)
            page.close()
        browser.close()
    print(f'完成 {len(shot.made)} 張 → {out}' + (f';失敗:{", ".join(failed)}' if failed else ''))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
