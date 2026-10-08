"""瀏覽器版的 NetCDF 檢視器(類 Panoply):瀏覽資料夾、看檔案結構、畫任一變數的切片。

用法:
    ncglobe ~/data                       # 開 http://127.0.0.1:8765
    ncglobe ~/data /Volumes/MyDrive --port 8800 --cache-mb 4000

只聽 127.0.0.1,而且只讀得到啟動時給的資料夾(roots)底下的檔案。

設計取捨:
- 後端只送「畫得出來的量」:二維切片抽稀到約 MAX_PX × MAX_PX。放大時前端送回目前的
  經緯度範圍(bbox),後端只取那一塊、重新抽稀 —— 全球看是粗的,放大就是原生解析度。
- 每個變數的二維切片**整塊讀一次**留在記憶體(依總量上限的 LRU)。壓縮過的 netCDF
  用 isel(step) 跳著讀非常慢:L2 每個變數 1 秒多;S5P 官方 L3 全球日檔
  (8192 × 16384 只有**一個**壓縮塊)跳著讀等於每一行都重新解壓 512 MB,會卡死。
- 規則網格(一維經緯度)畫 heatmap;二維經緯度(S5P L2 逐軌)由後端用 pcolormesh
  畫成影像,每個像素一個四邊形,鋪滿不留洞。
- 等距經緯以外的投影(Robinson、極地…)與匯出圖,由後端用 cartopy 畫整張圖。
- 讀取前先估算解壓後的大小,超過上限直接拒絕並說明,不讓伺服器陷進去;
  每個請求都在終端機印出耗時。
- 只用標準函式庫的 http.server,不需要網頁框架。
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time
import uuid
import warnings
import webbrowser
from collections import OrderedDict
from contextlib import contextmanager
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import netCDF4
import numpy as np
import xarray as xr
from xarray.backends.netCDF4_ import NETCDF4_PYTHON_LOCK

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / 'static' / 'index.html'
# 頁面用的圖示(白名單:只送這幾個檔,路徑不能拿來讀別的東西)
STATIC_FILES = {'favicon.ico': 'image/x-icon', 'logo.svg': 'image/svg+xml',
                'logo-180.png': 'image/png', 'logo-192.png': 'image/png', 'logo-512.png': 'image/png'}
# 內政部直轄市、縣市界線(TWD97,1090820 版)的邊界線,簡化到約 50 m 後隨套件附上

NC_SUFFIXES = {'.nc', '.nc4', '.netcdf', '.h5', '.hdf5', '.he5', '.cdf', '.hdf'}   # .hdf = HDF4(MODIS),netCDF4 讀得了
MAX_PX = 900            # heatmap 每軸最多幾格
MAX_POINTS = 40_000     # 非規則網格:給滑鼠讀值的取樣點數(畫面本身是後端畫的影像)
MAX_QUADS = 1_500_000   # 非規則網格畫影像時最多幾個像素四邊形
FIG_QUADS = 800_000     # 投影圖 / 匯出圖:cartopy 轉投影比較慢,點數少一些
MAX_READ_BYTES = 4 * 1024**3  # 單一個二維切片解壓後超過這個就拒絕讀
SERIES_MAX = 20_000     # 點選查值的時間序列最多幾點
CMAPS = {'Viridis': 'viridis', 'Turbo': 'turbo', 'Jet': 'jet', 'Plasma': 'plasma', 'Inferno': 'inferno',
         'RdBu': 'RdBu_r', 'YlOrRd': 'YlOrRd', 'Greys': 'gray'}
_SUPER = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁻', '0123456789-')


def _mpl_units(u: str) -> str:
    """單位字串給 matplotlib 畫:上標字元(⁻²)中文字型沒有字形會變方框,改成 mathtext 的上標。"""
    import re
    return re.sub(r'([⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+)', lambda m: f'$^{{{m.group(1).translate(_SUPER)}}}$', u or '')


def clean_units(u) -> str:
    """檔案寫 "None"、"1"、"-" 都是沒有單位:回空字串,不讓它出現在色階條、點選面板上。"""
    u = '' if u is None else str(u).strip()
    return '' if u.lower() in ('none', '1', '-', 'unitless', 'dimensionless') else u


def _cmap(name: str):
    """色表名稱 → matplotlib colormap;結尾加 _r 是反轉(RdBu 本身已是 RdBu_r,反轉後是 RdBu)。"""
    import matplotlib
    rev = name.endswith('_r')
    base = CMAPS.get(name[:-2] if rev else name, 'viridis')
    if rev:
        base = base[:-2] if base.endswith('_r') else base + '_r'
    return matplotlib.colormaps[base]


LAT_NAMES = ('latitude', 'lat', 'LAT', 'Latitude', 'nav_lat', 'y_lat')
LON_NAMES = ('longitude', 'lon', 'LON', 'Longitude', 'nav_lon', 'x_lon')

ROOTS: list[Path] = []

# 前端用的兩個大套件:第一次從 CDN 下載、核對雜湊後存在本機,之後頁面直接從本機讀
# (Plotly 從 cdnjs 下載實測要 5 秒,每次開頁都等;離線也能用)。雜湊跟 HTML 的 integrity 同一組。
VENDOR_DIR = Path(os.environ.get('NCGLOBE_CACHE', Path.home() / '.cache' / 'ncglobe'))
# 打包成單一執行檔(PyInstaller)時,前端套件與地圖資料跟著執行檔走,第一次開也不必上網
BUNDLE = Path(getattr(sys, '_MEIPASS', '.')) / 'ncglobe_bundle' if getattr(sys, 'frozen', False) else None
# 從 macOS 的 ncglobe.app 雙擊開啟:沒有終端機給參數,也沒有 Ctrl+C(用網頁上的「結束」)
IN_APP = bool(BUNDLE) and '.app/Contents/MacOS/' in sys.executable
VENDOR = {
    'plotly.min.js': ('https://cdnjs.cloudflare.com/ajax/libs/plotly.js/3.1.1/plotly.min.js',
                      'sha512', 'QaIUFweb9pyRnQZEYIujSDTHndibSwqsTSb69aedsfAAUpJRUE5yyYfaNAJIbTDuNECyHR/Cc0N7lQJrp0IEpA=='),
    'globe.gl.min.js': ('https://cdn.jsdelivr.net/npm/globe.gl@2.46.2/dist/globe.gl.min.js',
                        'sha384', '1uolMBZ25k3zJcNwCLEv49+L+m2dZudqAzsoSAJfQTzDCSBxJzrMuZ2dkp/5JKiT'),
}
_vendor_lock = threading.Lock()


def vendor(name: str) -> bytes:
    import hashlib
    import urllib.request
    url, algo, digest = VENDOR[name]
    f = VENDOR_DIR / name
    if BUNDLE and (BUNDLE / 'vendor' / name).exists():
        return (BUNDLE / 'vendor' / name).read_bytes()
    with _vendor_lock:
        if f.exists():
            return f.read_bytes()
        data = urllib.request.urlopen(url, timeout=60).read()
        got = base64.b64encode(hashlib.new(algo, data).digest()).decode()
        if got != digest:
            raise ValueError(f'{name} 的雜湊對不上,不使用(CDN 內容被改過?)')
        VENDOR_DIR.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return data
CACHE_BYTES = 3 * 1024**3

warnings.filterwarnings('ignore', message='The input coordinates to pcolormesh')

# 匯出圖的標題、色階條單位會有中文;matplotlib 預設的 DejaVu Sans 沒有中文字形(會變方框)
import logging  # noqa: E402
logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)   # PingFang 沒有 bold 字重,會一直警告
CJK_FONTS = ['PingFang TC', 'Heiti TC', 'Microsoft JhengHei', 'Arial Unicode MS', 'Noto Sans CJK TC', 'Noto Sans TC', 'DejaVu Sans']   # macOS / Windows / Linux


class Forbidden(Exception):
    pass


class TooBig(Exception):
    pass


# ------------------------------------------------------------------ 路徑安全

def resolve(path: str) -> Path:
    """只允許 roots 底下的路徑(擋 ../ 與符號連結跳出去)。"""
    p = Path(path).expanduser().resolve()
    if not any(p == r or p.is_relative_to(r) for r in ROOTS):
        raise Forbidden(path)
    return p


# ------------------------------------------------------------------ 開檔與陣列快取

# 每個檔只開一個 netCDF4 handle,所有群組的 xarray Dataset、檔案結構、壓縮塊資訊都用它。
# netCDF-C 4.9 的雷:同一個檔同時開兩個 handle(例如 PRODUCT 與 GEOLOCATIONS 兩個群組各開一次)、
# 關掉其中一個後再開這個檔,會直接 segmentation fault —— 整個伺服器沒有任何訊息就消失。
_open_lock = threading.RLock()
_roots: OrderedDict[str, netCDF4.Dataset] = OrderedDict()
_open: OrderedDict[tuple[str, str], xr.Dataset] = OrderedDict()
MAX_ROOTS = 64   # 同時開著幾個檔;碰到時關最久沒用的(連同它所有群組的 Dataset)


def stale_handle(e: Exception) -> bool:
    """讀到一半,檔被關掉了(另一個請求開太多檔,把它擠出去):重試一次就會重新開。"""
    return isinstance(e, RuntimeError) and 'Not a valid ID' in str(e)


def nc_root(file: str) -> netCDF4.Dataset:
    """這個檔唯一的 netCDF4 handle。"""
    with _open_lock:
        h = _roots.get(file)
        if h is not None:
            _roots.move_to_end(file)
            return h
        try:
            with nc_lock():
                h = netCDF4.Dataset(file)
        except OSError as e:
            raise OSError(_unreadable_reason(file) or str(e)) from e
        _roots[file] = h
        while len(_roots) > MAX_ROOTS:
            old_path, old = _roots.popitem(last=False)
            for k in [k for k in _open if k[0] == old_path]:
                del _open[k]
            with nc_lock():
                old.close()
        return h


def close_all() -> None:
    """關掉所有開著的檔(拿著讀檔鎖)。只把字典清空的話,handle 會在之後某個時間點被垃圾回收,
    在隨便哪條執行緒、沒拿鎖的情況下呼叫 HDF5 關檔 —— 剛好別的執行緒在讀檔時就 segfault(CI 的 Linux 上實際遇到)。"""
    with _open_lock:
        _open.clear()
        while _roots:
            _, h = _roots.popitem()
            try:
                with nc_lock():
                    h.close()
            except RuntimeError:
                pass   # 已經關過


def _unreadable_reason(file: str) -> str | None:
    """打不開的檔多半不是壞掉,而是下載失敗存成了網頁(例如 Earthdata 登入頁):講清楚。"""
    try:
        with open(file, 'rb') as f:
            head = f.read(512).lstrip().lower()
    except OSError:
        return None
    if head.startswith((b'<!doctype html', b'<html', b'<?xml', b'<')):
        size = os.path.getsize(file)
        login = b'login' in head or b'earthdata' in head
        return (f'這不是 NetCDF/HDF 檔,內容是 HTML 網頁({size:,} bytes)'
                + (',看起來是登入頁:下載時登入失效,請重新下載這個檔' if login else ',可能是下載失敗存下的錯誤頁,請重新下載'))
    if not head:
        return '檔案是空的(0 bytes),請重新下載'
    return None


def nc_group(file: str, group: str):
    g = nc_root(file)
    for part in [x for x in group.split('/') if x]:
        g = g.groups[part]
    return g


def dataset(file: str, group: str) -> xr.Dataset:
    key = (file, group)
    with _open_lock:
        if key in _open:
            _open.move_to_end(key)
            return _open[key]
        # 用同一個 handle 的群組建 Dataset(不另開檔);不呼叫 ds.close(),那會把整個檔關掉
        store = xr.backends.NetCDF4DataStore(nc_group(file, group))
        ds = xr.open_dataset(store, decode_times=True, mask_and_scale=True, cache=False)
        ds = _borrow_geolocation(file, group, ds)
        ds = _hdfeos_grid_coords(file, ds)
        _open[key] = ds
        while len(_open) > 64:
            _open.popitem(last=False)
        return ds


class ArrayCache:
    """依總位元組數淘汰的 LRU。一個 512 MB 的全球場跟一個 7 MB 的 L2 變數不能算同一格。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self.bytes = 0

    def get(self, k):
        with self.lock:
            a = self.items.get(k)
            if a is not None:
                self.items.move_to_end(k)
            return a

    def put(self, k, a: np.ndarray) -> None:
        with self.lock:
            if k in self.items:
                return
            self.items[k] = a
            self.bytes += a.nbytes
            while self.bytes > CACHE_BYTES and len(self.items) > 1:
                _, old = self.items.popitem(last=False)
                self.bytes -= old.nbytes


ARRAYS = ArrayCache()


def full2d(ds: xr.Dataset, key: tuple, name: str, sel: dict, ydim: str, xdim: str) -> np.ndarray:
    """一個變數在 sel 下的完整二維陣列(y, x),讀一次就留在記憶體。"""
    a = ds[name]
    s = {d: v for d, v in sel.items() if d in a.dims}
    k = (*key, name, tuple(sorted(s.items())))
    hit = ARRAYS.get(k)
    if hit is not None:
        return hit
    need = int(np.prod([ds.sizes[d] for d in a.dims if d not in s])) * max(a.dtype.itemsize, 4)
    if need > MAX_READ_BYTES:
        raise TooBig(f'{name} 的一個切片解壓後約 {need / 1024**3:.1f} GB,超過上限 '
                     f'{MAX_READ_BYTES / 1024**3:.0f} GB;請改用較小的檔或先裁切')
    arr = a.isel(s).transpose(ydim, xdim).values
    ARRAYS.put(k, arr)
    return arr


@contextmanager
def nc_lock():
    """直接用 netCDF4 開檔時拿 xarray 讀檔用的同一把鎖。
    HDF5 不是執行緒安全的:伺服器每個請求一條執行緒,xarray 自己讀檔有上鎖,
    這裡沒上鎖的話,同時開檔會偶發讀取錯誤,嚴重時整個行程直接結束。
    要用 xarray 的組合鎖物件本身:它依物件 id 排序後才拿,自己照寫的順序拿兩把會跟它相反而卡死。"""
    with NETCDF4_PYTHON_LOCK:
        yield


def chunk_note(file: str, group: str, var: str) -> str | None:
    """壓縮塊很大時提醒:第一次讀要整塊解壓(例如 S5P 官方 L3 全球日檔只有一塊)。"""
    try:
        g = nc_group(file, group)   # 先拿 handle(開檔自己會上鎖);nc_lock 不能重入
        with nc_lock():
            v = g.variables[var]
            ch = v.chunking()
            if ch == 'contiguous' or not v.filters() or not any(v.filters().get(k) for k in ('zlib', 'szip', 'zstd', 'blosc', 'bzip2')):
                return None
            mb = int(np.prod(ch)) * v.dtype.itemsize / 1024**2
            if mb >= 128:
                return f'這個變數的壓縮塊有 {mb:.0f} MB,第一次讀要整塊解壓,可能要十幾秒;之後縮放就快了'
    except Exception:
        return None
    return None


# ------------------------------------------------------------------ JSON 工具

def jsonable(v):
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, np.ndarray):
        return [jsonable(x) for x in v.tolist()] if v.size <= 64 else f'array{v.shape}'
    if isinstance(v, bytes):
        return v.decode(errors='replace')
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return v if isinstance(v, (str, int, bool, type(None))) else str(v)


def arr_to_list(a, digits: int | None = None, sig: int = 5) -> list:
    """NaN → null,截短讓 JSON 小很多。

    給 digits:四捨五入到小數第 digits 位(經緯度用);沒給:保留 sig 位有效數字(數值用)。
    數值不能用小數位數:NO₂ 是 1e-5 mol m⁻² 量級,round 到第 6 位只剩一位有效數字。
    """
    a = np.asarray(a, dtype='float64')
    finite = np.isfinite(a)
    if not a.size:
        return []
    if digits is not None:
        r = np.round(a, digits)
    else:
        with np.errstate(divide='ignore', invalid='ignore'):
            mag = np.floor(np.log10(np.abs(a)))
        mag = np.where(finite & (a != 0), mag, sig - 1)     # 0、NaN:scale = 1
        scale = 10.0 ** (sig - 1 - mag)
        r = np.round(a * scale) / scale
    out = r.astype(object)
    out[~finite] = None
    return out.tolist()


def coord_labels(values: np.ndarray) -> list[str]:
    if np.issubdtype(values.dtype, np.datetime64):
        return [str(v)[:19].replace('T', ' ') for v in values.astype('datetime64[s]')]
    return [f'{v:.6g}' if isinstance(v, (float, np.floating)) else str(v) for v in values.tolist()]


def stats(a) -> dict:
    a = np.asarray(a, dtype='float64')
    a = a[np.isfinite(a)]
    if not a.size:
        return {'min': None, 'max': None, 'p2': None, 'p98': None, 'count': 0}
    if a.size > 2_000_000:   # 百分位數用抽樣算,全球場上億點時省幾秒
        a2 = a[:: a.size // 1_000_000]
    else:
        a2 = a
    p2, p98 = np.percentile(a2, [2, 98])
    return {'min': float(a.min()), 'max': float(a.max()), 'p2': float(p2), 'p98': float(p98), 'count': int(a.size)}


# ------------------------------------------------------------------ 資料夾 / 檔案結構

_has_cache: dict[tuple[str, float], bool] = {}
SCAN_LIMIT = 50_000  # 掃這麼多項目還沒找到就當作「有」,寧可多列也不要卡住或漏掉


def has_data_files(d: Path) -> bool:
    """這個資料夾(含所有子資料夾)底下有沒有任何 NetCDF/HDF 檔。

    找到第一個就停;結果依 (路徑, 修改時間) 快取 —— 硬碟上加了檔案,資料夾的
    mtime 會變,下次自然重掃。
    """
    try:
        key = (str(d), d.stat().st_mtime)
    except OSError:
        return False
    if key in _has_cache:
        return _has_cache[key]
    stack, seen, found = [d], 0, False
    while stack and not found:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    seen += 1
                    if e.name.startswith('.'):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif os.path.splitext(e.name)[1].lower() in NC_SUFFIXES:
                        found = True
                        break
        except OSError:
            continue
        if seen > SCAN_LIMIT:
            found = True
    _has_cache[key] = found
    return found


# 衛星資料常見 年/月/日 一層層的資料夾:列表時把「年底下只有月」合成 2026-03 一項,
# 一路只有一個子資料夾的也合成一項(2026-01-02),免得點三次才看到檔;「..」也跳過這些中間層。
_YEAR_DIR = re.compile(r'^(19|20)\d{2}$')
_MD_DIR = re.compile(r'^\d{2}$')
COLLAPSE_DEPTH = 4


def _subdirs_only(d: Path) -> list[Path] | None:
    """d 底下只有子資料夾(沒有資料檔)時回傳它們,否則 None。"""
    try:
        kids = [c for c in d.iterdir() if not c.name.startswith('.')]
    except OSError:
        return None
    if any(not c.is_dir() and c.suffix.lower() in NC_SUFFIXES for c in kids):
        return None
    return sorted((c for c in kids if c.is_dir()), key=lambda c: c.name.lower())


def _month_dirs(d: Path) -> list[Path] | None:
    """年資料夾(2026)底下只有月資料夾(01–12)時回傳那些月資料夾。"""
    if not _YEAR_DIR.match(d.name):
        return None
    subs = _subdirs_only(d)
    if not subs or not all(_MD_DIR.match(c.name) and 1 <= int(c.name) <= 12 for c in subs):
        return None
    return subs


def _follow_single(d: Path, label: str) -> tuple[str, Path]:
    """一路只有一個子資料夾時往下走,名稱接起來(數字層用 -,例如 2026-01-02)。"""
    for _ in range(COLLAPSE_DEPTH):
        subs = _subdirs_only(d)
        if not subs or len(subs) != 1 or _month_dirs(subs[0]):   # 年/月那層留給下一層合併:產品名稱不能被吞掉
            break
        d = subs[0]
        label += ('-' if _MD_DIR.match(d.name) and label[-2:].isdigit() else '/') + d.name
    return label, d


def _is_passthrough(d: Path) -> bool:
    """列表裡被合併掉的中間層(年底下只有月、或只有一個子資料夾):「..」不要停在這裡。"""
    if any(d == r for r in ROOTS):
        return False
    if _month_dirs(d) is not None:
        return True
    subs = _subdirs_only(d)
    return bool(subs) and len(subs) == 1 and _month_dirs(subs[0]) is None   # 跟 _follow_single 同一個停止條件


def _listed_dirs(c: Path) -> list[dict]:
    if not has_data_files(c):   # 底下一個資料檔都沒有的資料夾不列
        return []
    months = _month_dirs(c)
    if months:
        out = []
        for m in months:
            if has_data_files(m):
                name, target = _follow_single(m, f'{c.name}-{m.name}')
                out.append({'name': name, 'path': str(target)})
        return out
    name, target = _follow_single(c, c.name)
    return [{'name': name, 'path': str(target)}]


def list_dir(path: str | None) -> dict:
    if not path:
        return {'path': None, 'parent': None,
                'dirs': [{'name': str(r), 'path': str(r)} for r in ROOTS], 'files': []}
    p = resolve(path)
    dirs, files = [], []
    for c in sorted(p.iterdir(), key=lambda c: c.name.lower()):
        if c.name.startswith('.'):
            continue
        if c.is_dir():
            dirs += _listed_dirs(c)
        elif c.suffix.lower() in NC_SUFFIXES:
            files.append({'name': c.name, 'path': str(c), 'size': c.stat().st_size, **file_meta(c.name)})
    parent = None
    if p not in ROOTS:
        q = p.parent
        while q not in ROOTS and q != q.parent and _is_passthrough(q):
            q = q.parent
        parent = str(q)
    return {'path': str(p), 'parent': parent, 'dirs': dirs, 'files': files}


TREE_MAX = 20_000


# ------------------------------------------------------------------ 子資料夾搜尋(檔名索引)

# 搜尋框要找到子資料夾裡的檔(例如年資料夾底下各月):背景逐層掃描,掃到哪裡就能先搜到哪裡;
# 掃完把檔名清單存成暫存檔(~/.cache/ncglobe/index/),下次同一個資料夾馬上就有結果,
# 太舊時在背景重掃再換上新的清單。上層資料夾的索引也拿來給它底下的子資料夾用。
INDEX_DIR = VENDOR_DIR / 'index'
INDEX_TTL = 600        # 秒:暫存檔超過這個時間就在背景重掃
SEARCH_MAX = 300       # 一次最多回傳幾筆


def _search_key(rel: str, name: str) -> str:
    """可以被搜到的字:檔名、觀測時間(2026-03-04 05:12)、日期、所在子資料夾。"""
    m = file_meta(name)
    return f"{name} {m.get('start') or ''} {m.get('date') or ''} {'' if rel == '.' else rel}".lower()


class FolderIndex:
    def __init__(self, root: Path):
        self.root = root
        self.entries: list[tuple[str, str, int, str]] = []   # (相對資料夾, 檔名, 大小, 搜尋字串)
        self.lock = threading.Lock()
        self.building = False
        self.scanned_dirs = 0
        self.built_at = 0.0

    def cache_file(self) -> Path:
        return INDEX_DIR / (hashlib.sha1(str(self.root).encode()).hexdigest()[:16] + '.json.gz')

    def load(self) -> bool:
        try:
            d = json.loads(gzip.decompress(self.cache_file().read_bytes()))
        except (OSError, ValueError, EOFError):
            return False
        if d.get('v') != 2 or d.get('root') != str(self.root):
            return False
        self.entries = [tuple(e) for e in d['files']]
        self.scanned_dirs, self.built_at = d.get('dirs', 0), d['built_at']
        return True

    def refresh(self) -> None:
        with self.lock:
            if self.building:
                return
            self.building = True
        threading.Thread(target=self._walk, daemon=True).start()

    def _walk(self) -> None:
        progressive = not self.built_at          # 第一次建:直接長在 entries 上,邊掃邊能搜
        out = self.entries if progressive else []
        ndirs = 0
        try:
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
                rel = os.path.relpath(dirpath, self.root)
                batch = []
                for n in sorted(filenames):
                    if n.startswith('.') or os.path.splitext(n)[1].lower() not in NC_SUFFIXES:
                        continue
                    try:
                        size = os.stat(os.path.join(dirpath, n)).st_size
                    except OSError:
                        continue
                    batch.append((rel, n, size, _search_key(rel, n)))
                ndirs += 1
                with self.lock:
                    out.extend(batch)
                    if progressive:
                        self.scanned_dirs = ndirs
        finally:
            with self.lock:
                self.entries, self.scanned_dirs = out, ndirs
                self.built_at = time.time()
                self.building = False
        try:
            INDEX_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file().with_suffix('.tmp')
            tmp.write_bytes(gzip.compress(json.dumps(
                {'v': 2, 'root': str(self.root), 'built_at': self.built_at, 'dirs': ndirs,
                 'files': out}, ensure_ascii=False).encode(), 5))
            tmp.replace(self.cache_file())
        except OSError:
            pass   # 寫不了暫存檔就只留在記憶體


_indexes: dict[Path, FolderIndex] = {}
_index_lock = threading.Lock()


def _index_for(p: Path) -> FolderIndex:
    with _index_lock:
        # 已有上層資料夾(或自己)的索引就用它;取最近的那個
        owners = [r for r in _indexes if p == r or p.is_relative_to(r)]
        if owners:
            idx = _indexes[max(owners, key=lambda r: len(r.parts))]
        else:
            idx = _indexes[p] = FolderIndex(p)
            idx.load()
    if not idx.building and time.time() - idx.built_at > INDEX_TTL:
        idx.refresh()
    return idx


def search_files(path: str, q: str) -> dict:
    """在 path 與它所有子資料夾裡找檔名 / 觀測時間 / 日期 / 子資料夾名稱都含有每個關鍵字的檔。"""
    p = resolve(path)
    idx = _index_for(p)
    prefix = '' if idx.root == p else str(p.relative_to(idx.root))
    terms = q.lower().split()
    with idx.lock:
        entries, building, dirs = list(idx.entries), idx.building, idx.scanned_dirs
    hits, total = [], 0
    for rel, name, size, key in entries:
        if prefix and not (rel == prefix or rel.startswith(prefix + os.sep)):
            continue
        if terms and all(t in key for t in terms):
            total += 1
            if len(hits) < SEARCH_MAX:
                f = idx.root / rel / name
                hits.append({'name': name, 'path': str(f), 'size': size,
                             'rel': os.path.relpath(f.parent, p), **file_meta(name)})
    return {'status': 'building' if building else 'ready', 'dirs': dirs, 'files': len(entries),
            'total': total, 'results': hits, 'age': round(time.time() - idx.built_at) if idx.built_at else None}


def list_tree(path: str) -> dict:
    """資料夾底下(含所有子資料夾)的資料檔,給合成視窗跨資料夾選檔(例如年資料夾底下的各月)。"""
    root = resolve(path)
    files, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for n in sorted(filenames):
            if n.startswith('.') or os.path.splitext(n)[1].lower() not in NC_SUFFIXES:
                continue
            f = Path(dirpath) / n
            try:
                size = f.stat().st_size
            except OSError:
                continue
            files.append({'name': n, 'path': str(f), 'size': size, 'rel': str(f.parent.relative_to(root)), **file_meta(n)})
            if len(files) >= TREE_MAX:
                truncated = True
                break
        if truncated:
            break
    return {'path': str(root), 'files': files, 'truncated': truncated}


def file_info(file: str) -> dict:
    """群組樹 + 每個變數的維度、形狀、單位(用 netCDF4 走群組,不載入資料)。"""
    p = resolve(file)
    if not p.is_file():
        raise FileNotFoundError(2, '找不到', str(p))
    groups = []
    nc = nc_root(str(p))
    with nc_lock():
        def walk(g, prefix):
            vars_ = []
            for name, v in g.variables.items():
                attrs = {k: jsonable(v.getncattr(k)) for k in v.ncattrs()}
                try:
                    numeric = np.issubdtype(np.dtype(v.dtype), np.number)
                except TypeError:
                    numeric = False
                vars_.append({
                    'name': name, 'dims': list(v.dimensions), 'shape': list(v.shape),
                    'dtype': str(v.dtype), 'units': clean_units(attrs.get('units') or attrs.get('unit', '')),
                    'long_name': attrs.get('long_name') or attrs.get('standard_name') or '',
                    'plottable': len(v.shape) >= 1 and numeric,
                })
            groups.append({'group': prefix, 'variables': vars_,
                           'attrs': {k: jsonable(g.getncattr(k)) for k in g.ncattrs()}})
            for name, sub in g.groups.items():
                walk(sub, f'{prefix}/{name}' if prefix else name)
        walk(nc, '')
    return {'file': str(p), 'size': p.stat().st_size, 'groups': groups}


# ------------------------------------------------------------------ 座標判斷

def _borrow_geolocation(file: str, group: str, ds: xr.Dataset) -> xr.Dataset:
    """經緯度放在別的群組時(GEMS:資料在 Data Fields、經緯度在 Geolocation Fields)借過來。

    只借維度名稱與大小都對得上的,否則寧可當成沒有經緯度,也不要畫到錯的位置。
    """
    if _find(ds, LAT_NAMES) and _find(ds, LON_NAMES):
        return ds
    found = None
    try:
        root = nc_root(file)
        with nc_lock():
            groups, stack = [], [('', root)]
            while stack:
                path, g = stack.pop()
                groups.append((path, g))
                stack.extend((f'{path}/{n}'.lstrip('/'), sub) for n, sub in g.groups.items())
            for path, g in groups:
                if path == group.strip('/'):
                    continue
                la = next((n for n in LAT_NAMES if n in g.variables), None)
                lo = next((n for n in LON_NAMES if n in g.variables), None)
                if not (la and lo):
                    continue
                dims = g.variables[la].dimensions
                if all(d in ds.sizes and ds.sizes[d] == n for d, n in zip(dims, g.variables[la].shape)):
                    found = (path, la, lo)
                    break
    except OSError:
        pass
    if found is None:
        return ds
    # 在 nc_lock 外建:xarray 讀屬性時會自己拿同一把鎖,鎖內再拿會卡死
    path, la, lo = found
    geo = dataset(file, path)
    return ds.assign_coords({la: geo[la], lo: geo[lo]})


_EOS_GRID = __import__('re').compile(r'GridName="([^"]+)"(.*?)END_GROUP=GRID_\d+', __import__('re').S)


def _hdfeos_grid_coords(file: str, ds: xr.Dataset) -> xr.Dataset:
    """HDF-EOS2 正弦投影格點(MODIS MCD19A2、MOD13、MOD11…)沒有經緯度變數,只有格點編號;
    網格的角點(公尺)與地球半徑寫在 StructMetadata.0。這裡算出每一格中心的經緯度,
    以 Latitude_<網格>/Longitude_<網格> 加進去(每個網格一組,1 km 與 5 km 分開)。"""
    if _find(ds, LAT_NAMES) and _find(ds, LON_NAMES):
        return ds
    root = nc_root(file)
    with nc_lock():
        meta = root.getncattr('StructMetadata.0') if 'StructMetadata.0' in root.ncattrs() else ''
    if 'GCTP_SNSOID' not in meta:
        return ds
    num = lambda key, text: [float(v) for v in __import__('re').search(key + r'=\(([^)]*)\)', text).group(1).split(',')]
    coords = {}
    for name, body in _EOS_GRID.findall(meta):
        if 'GCTP_SNSOID' not in body:
            continue
        ydim, xdim = f'YDim:{name}', f'XDim:{name}'
        if ydim not in ds.sizes or xdim not in ds.sizes:
            continue
        (ulx, uly), (lrx, lry) = num('UpperLeftPointMtrs', body), num('LowerRightMtrs', body)
        radius = num('ProjParams', body)[0] or 6371007.181
        ny, nx = ds.sizes[ydim], ds.sizes[xdim]
        x = ulx + (np.arange(nx) + 0.5) * (lrx - ulx) / nx
        y = uly - (np.arange(ny) + 0.5) * (uly - lry) / ny
        lat = np.degrees(y / radius)
        with np.errstate(invalid='ignore', divide='ignore'):
            lon = np.degrees(x[None, :] / (radius * np.cos(np.radians(lat))[:, None]))
        lon = np.where(np.abs(lon) <= 180, lon, np.nan)   # 正弦投影外框以外的格(地圖邊緣)沒有經緯度
        lat2 = np.broadcast_to(lat[:, None], lon.shape).astype('float32')
        coords[f'Latitude_{name}'] = ((ydim, xdim), lat2, {'standard_name': 'latitude', 'units': 'degrees_north'})
        coords[f'Longitude_{name}'] = ((ydim, xdim), lon.astype('float32'), {'standard_name': 'longitude', 'units': 'degrees_east'})
    return ds.assign_coords(coords) if coords else ds


def _find(ds: xr.Dataset, names) -> str | None:
    for n in names:
        if n in ds.variables:
            return n
    return None


def plot_axes(ds: xr.Dataset, var: str):
    """決定要畫哪兩個維度、經緯度怎麼來。

    回傳 (ydim, xdim, kind, lat_name, lon_name):
      kind = 'regular'(一維經緯度,或任意一維座標/索引)| 'curvilinear'(二維經緯度)
    """
    da = ds[var]
    dims = list(da.dims)
    ydim, xdim = dims[-2], dims[-1]
    lat = _find(ds, LAT_NAMES)
    lon = _find(ds, LON_NAMES)
    # 一個檔有好幾組經緯度(例如 HDF-EOS 的 1 km 與 5 km 網格):用維度跟這個變數對得上的那組
    std = lambda n: [k for k, v in ds.coords.items() if v.attrs.get('standard_name') == n and v.ndim == 2 and set(v.dims) <= set(dims)]
    if std('latitude') and std('longitude'):
        lat, lon = std('latitude')[0], std('longitude')[0]
    if lat and lon:
        la, lo = ds[lat], ds[lon]
        if la.ndim == 1 and lo.ndim == 1 and la.dims[0] in dims and lo.dims[0] in dims:
            return la.dims[0], lo.dims[0], 'regular', lat, lon
        if la.ndim >= 2 and la.dims[-2:] == lo.dims[-2:] and set(la.dims[-2:]) <= set(dims):
            return la.dims[-2], la.dims[-1], 'curvilinear', lat, lon
    return ydim, xdim, 'regular', None, None


def _orbit_stamps(path: str) -> list[str]:
    """MCD19A2 的 Orbit_time_stamp「20250010210T 20250010530A …」→ ['02:10 UTC Terra', '05:30 UTC Aqua', …]。"""
    try:
        nc = nc_root(path)
        with nc_lock():
            raw = str(nc.getncattr('Orbit_time_stamp')) if 'Orbit_time_stamp' in nc.ncattrs() else ''
    except Exception:
        return []
    sat = {'T': 'Terra', 'A': 'Aqua'}
    return [f'{t[7:9]}:{t[9:11]} UTC {sat.get(t[11:], t[11:])}' for t in raw.split() if len(t) >= 12]


def var_detail(file: str, group: str, var: str) -> dict:
    path = str(resolve(file))
    ds = dataset(path, group)
    da = ds[var]
    out = {'name': var, 'dims': list(da.dims), 'shape': list(da.shape),
           'attrs': {k: jsonable(v) for k, v in da.attrs.items()},
           'units': clean_units(da.attrs.get('units') or da.attrs.get('unit', '')),
           'long_name': str(da.attrs.get('long_name') or da.attrs.get('standard_name') or var),
           'note': chunk_note(path, group, var)}
    if da.ndim == 0:
        out.update(kind='scalar', value=jsonable(da.values[()]))
        return out
    if da.ndim == 1:
        out.update(kind='line', extra_dims=[])
        return out
    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    extra = []
    for d in da.dims:
        if d in (ydim, xdim):
            continue
        n = ds.sizes[d]
        labels = coord_labels(ds[d].values) if d in ds.coords and n <= 20000 else [str(i) for i in range(n)]
        title = '時間' if re.search(r'time', d, re.I) else d
        if re.fullmatch(r'(pressure_level|level|plev|isobaricInhPa|lev)', d, re.I):   # ERA5 等:氣壓層
            title = '氣壓層'
            u = str(ds[d].attrs.get('units', '')).lower() if d in ds.coords else ''
            if u in ('hpa', 'millibars', 'mbar', 'mb'):
                labels = [f'{v} hPa' for v in labels]
        if d.lower().startswith('orbits'):          # MCD19A2:同日多次過境,時間與衛星在 Orbit_time_stamp
            stamps = _orbit_stamps(path)
            if len(stamps) == n:
                labels, title = stamps, '過境'
        extra.append({'dim': d, 'size': n, 'labels': labels, 'title': title})
    out.update(kind=kind, ydim=ydim, xdim=xdim, extra_dims=extra, geo=bool(lat and lon),
               has_qa='qa_value' in ds.variables and var != 'qa_value')
    return out


def _sel(da, idx: dict, ydim: str, xdim: str) -> dict:
    sel = {}
    for d in da.dims:
        if d in (ydim, xdim):
            continue
        sel[d] = min(max(int(idx.get(d, 0)), 0), da.sizes[d] - 1)
    return sel


# ------------------------------------------------------------------ 規則網格

def _index_range(coord: np.ndarray, lo: float, hi: float) -> tuple[int, int]:
    """一維單調座標上,落在 [lo, hi] 的 index 範圍(含邊界多一格)。"""
    asc = coord[0] <= coord[-1]
    c = coord if asc else coord[::-1]
    i0 = max(int(np.searchsorted(c, lo, 'left')) - 1, 0)
    i1 = min(int(np.searchsorted(c, hi, 'right')) + 1, len(c))
    if not asc:
        i0, i1 = len(c) - i1, len(c) - i0
    return i0, max(i1, i0 + 1)


def _regular_axes(ds, da, ydim, xdim, lat, lon):
    """回傳 (xc, yc, order):經度 0–360 時 xc 已轉成 −180–180 並排序,order 是原本的欄序。"""
    xc = ds[lon].values if lon else (ds[xdim].values if xdim in ds.coords else np.arange(da.sizes[xdim]))
    yc = ds[lat].values if lat else (ds[ydim].values if ydim in ds.coords else np.arange(da.sizes[ydim]))
    xc = np.asarray(xc, dtype='float64')
    yc = np.asarray(yc, dtype='float64')
    order = None
    if lon is not None and np.nanmax(xc) > 180:
        xc = np.where(xc > 180, xc - 360, xc)
        order = np.argsort(xc, kind='stable')
        xc = xc[order]
    return xc, yc, order


def _regular_window(ds, key, var, ydim, xdim, lat, lon, sel, bbox, max_px):
    da = ds[var]
    xc, yc, order = _regular_axes(ds, da, ydim, xdim, lat, lon)
    iy0, iy1, ix0, ix1 = 0, len(yc), 0, len(xc)
    if bbox and lat and lon:
        iy0, iy1 = _index_range(yc, bbox[1], bbox[3])
        ix0, ix1 = _index_range(xc, bbox[0], bbox[2])
    sy = max(1, math.ceil((iy1 - iy0) / max_px))
    sx = max(1, math.ceil((ix1 - ix0) / max_px))
    z = full2d(ds, key, var, sel, ydim, xdim)
    cols = (order[ix0:ix1:sx] if order is not None else np.arange(ix0, ix1, sx))
    zz = z[iy0:iy1:sy][:, cols]
    return xc[ix0:ix1:sx], yc[iy0:iy1:sy], zz, (sy, sx), (iy1 - iy0, ix1 - ix0)


# ------------------------------------------------------------------ 切片(前端互動圖)

def _expand(bbox: list[float], f: float) -> list[float]:
    """畫面範圍往四周各延伸 f 倍(以經緯度為界)。"""
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return [max(-180.0, bbox[0] - f * w), max(-90.0, bbox[1] - f * h),
            min(180.0, bbox[2] + f * w), min(90.0, bbox[3] + f * h)]


def slice_data(file: str, group: str, var: str, idx: dict, bbox: list[float] | None,
               qa_min: float | None = None, pad: float = 0.0) -> dict:
    """pad > 0:資料多抓畫面外一圈(平移時周圍已經有東西),統計值(自動色階)仍只算畫面內。"""
    path = str(resolve(file))
    ds = dataset(path, group)
    da = ds[var]
    key = (path, group)

    if da.ndim == 1:
        d = da.dims[0]
        n = da.shape[0]
        step = max(1, math.ceil(n / 20000))
        y = da.values[::step]
        x = coord_labels(ds[d].values[::step]) if d in ds.coords else list(range(0, n, step))
        return {'kind': 'line', 'x': x, 'y': arr_to_list(y), 'xlabel': d, 'step': step}

    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    sel = _sel(da, idx, ydim, xdim)

    big = _expand(bbox, pad) if bbox and pad > 0 else bbox
    if kind == 'curvilinear':
        return _curvilinear(ds, key, var, ydim, xdim, lat, lon, sel, bbox, qa_min, big)

    # 延伸後格數變多:每軸上限跟著放寬一些,畫面內的解析度才不會掉一半
    x, y, z, step, native = _regular_window(ds, key, var, ydim, xdim, lat, lon, sel, big,
                                            int(MAX_PX * 1.5) if big is not bbox else MAX_PX)
    # 統計值照沒延伸時的畫面視窗算(陣列已在快取,再切一次很便宜),自動色階才跟原本一樣
    zin = z if big is bbox else _regular_window(ds, key, var, ydim, xdim, lat, lon, sel, bbox, MAX_PX)[2]
    return {'kind': 'heatmap', 'x': arr_to_list(x, 5), 'y': arr_to_list(y, 5),
            'z': arr_to_list(z), 'stats': stats(zin), 'step': list(step),
            'native': list(native), 'geo': bool(lat and lon),
            'xlabel': lon or xdim, 'ylabel': lat or ydim}


# ------------------------------------------------------------------ 非規則網格(L2 逐軌)

_no_bounds: set = set()


def edge_grid(path: str, group: str, sel: dict, ydim: str, xdim: str):
    """真實像素角點組成的 (2, ny+1, nx+1) 角點網格(經度、緯度),沒有就回 None。

    S5P L2 在 PRODUCT/SUPPORT_DATA/GEOLOCATIONS 有 latitude_bounds / longitude_bounds
    (每個像素 4 個角)。相鄰像素共用角點:角 1 = 右邊像素的角 0、角 3 = 下一列的角 0、
    角 2 = 右下像素的角 0 —— 所以可以拼成一張完整網格,pcolormesh 直接畫真實形狀。
    拼之前先檢查共用關係對不對得上,對不上(別的產品排法不同)就退回用中心點推。
    """
    k = ('edges', path, group, tuple(sorted(sel.items())))
    if k in _no_bounds:
        return None
    hit = ARRAYS.get(k)
    if hit is not None:
        return hit
    cands = [group, f'{group}/SUPPORT_DATA/GEOLOCATIONS' if group else 'SUPPORT_DATA/GEOLOCATIONS']
    for g in cands:
        try:
            d = dataset(path, g)
        except Exception:
            continue
        if 'latitude_bounds' not in d.variables or 'longitude_bounds' not in d.variables:
            continue
        try:
            out = []
            for name in ('longitude_bounds', 'latitude_bounds'):
                a = d[name]
                cdim = [x for x in a.dims if x not in (ydim, xdim) and x not in sel][0]
                c = a.isel({x: v for x, v in sel.items() if x in a.dims}).transpose(ydim, xdim, cdim).values
                e = np.empty((c.shape[0] + 1, c.shape[1] + 1), dtype='float64')
                e[:-1, :-1] = c[..., 0]
                e[:-1, -1] = c[:, -1, 1]
                e[-1, :-1] = c[-1, :, 3]
                e[-1, -1] = c[-1, -1, 2]
                # 檢查共用關係:角 1 == 右邊像素的角 0(經度允許 ±360 的換日線差)
                diff = np.abs(c[:, :-1, 1] - c[:, 1:, 0])
                diff = np.minimum(diff, np.abs(diff - 360))
                if np.nanmedian(diff) > 1e-3:
                    raise ValueError('corner order')
                out.append(e)
            arr = np.stack(out)
            ARRAYS.put(k, arr)
            return arr
        except Exception:
            break
    _no_bounds.add(k)
    return None


def _swath(ds, key, var, ydim, xdim, lat, lon, sel, bbox, qa_min, max_n):
    """在原生解析度找出畫面內的像素,再依那個數量抽稀到 max_n。

    回傳 (lon, lat, value, ok_mask, step, edges),都是二維(保留像素鄰接關係,畫四邊形要用);
    edges 是對應的真實角點網格 (2, n+1, m+1),檔案沒有角點時是 None。
    """
    def pick(name):
        return full2d(ds, key, name, sel, ydim, xdim)

    la, lo = pick(lat), pick(lon)
    mask = np.isfinite(la) & np.isfinite(lo)
    if bbox:
        # 多抓一圈:畫四邊形時邊緣像素要有鄰居,不然畫面邊上會缺一條
        pad = 0.5
        mask &= (lo >= bbox[0] - pad) & (lo <= bbox[2] + pad) & (la >= bbox[1] - pad) & (la <= bbox[3] + pad)
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if not rows.size:
        return None
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    n = (r1 - r0) * (c1 - c0)
    step = max(1, math.ceil(math.sqrt(n / max_n)))
    win = (slice(r0, r1, step), slice(c0, c1, step))
    v = pick(var)[win].astype('float64')
    ok = np.isfinite(v)
    if qa_min is not None and 'qa_value' in ds.variables:
        ok &= pick('qa_value')[win] >= qa_min
    edges = None
    e = edge_grid(key[0], key[1], sel, ydim, xdim)
    if e is not None:
        ri = np.minimum(r0 + step * np.arange(v.shape[0] + 1), e.shape[1] - 1)
        ci = np.minimum(c0 + step * np.arange(v.shape[1] + 1), e.shape[2] - 1)
        edges = e[:, ri][:, :, ci]
    return lo[win], la[win], v, ok, step, edges


def _curvilinear(ds, key, var, ydim, xdim, lat, lon, sel, bbox, qa_min, big=None) -> dict:
    """給前端的:統計值(畫面內)+ 滑鼠讀值用的稀疏取樣點(含延伸範圍 big)。畫面本身走 render()。"""
    big = big or bbox
    sw = _swath(ds, key, var, ydim, xdim, lat, lon, sel, big, qa_min, MAX_POINTS)
    if sw is None:
        return {'kind': 'swath', 'x': [], 'y': [], 'z': [], 'stats': stats([]), 'step': 1, 'geo': True}
    lo, la, v, ok, step, _ = sw
    inside = ok if not big else ok & (lo >= big[0]) & (lo <= big[2]) & (la >= big[1]) & (la <= big[3])
    full = _swath(ds, key, var, ydim, xdim, lat, lon, sel, bbox, qa_min, MAX_QUADS)
    st = stats(full[2][full[3]]) if full else stats([])
    return {'kind': 'swath', 'x': arr_to_list(lo[inside], 4), 'y': arr_to_list(la[inside], 4),
            'z': arr_to_list(v[inside]), 'stats': st, 'step': full[4] if full else step, 'geo': True}


# ------------------------------------------------------------------ 畫圖(影像 / 投影 / 匯出)

_draw_lock = threading.Lock()  # matplotlib 的繪圖狀態不保證執行緒安全;同時也避免多張大圖搶 CPU


def colorscale(cmap: str, n: int = 11) -> list:
    """matplotlib 色表取樣成 Plotly colorscale —— 色階條跟影像用同一份顏色。"""
    import matplotlib
    cm = _cmap(cmap)
    return [[i / (n - 1), matplotlib.colors.to_hex(cm(i / (n - 1)))] for i in range(n)]


def _log(v, log):
    if not log:
        return v
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(v > 0, np.log10(v), np.nan)


def _draw_swath(ax, lo, la, v, ok, kw, transform=None, edges=None):
    """有真實角點就用角點網格(shading='flat',像素形狀精確);沒有就由中心點推(nearest)。"""
    c = np.ma.masked_where(~ok | ~np.isfinite(v), v)
    if transform is not None:
        kw = {**kw, 'transform': transform}
    if edges is not None:
        x, y = edges[0], edges[1]
        kw = {**kw, 'shading': 'flat'}
    else:
        x, y = lo, la
        kw = {**kw, 'shading': 'nearest'}
    crosses = x.shape[1] > 1 and np.nanmax(np.abs(np.diff(x, axis=1))) > 180
    if crosses:   # 跨換日線:+360 畫一次、整體 −360 再畫一次,四邊形不會橫跨整張圖
        x360 = np.where(x < 0, x + 360, x)
        ax.pcolormesh(x360, y, c, **kw)
        ax.pcolormesh(x360 - 360, y, c, **kw)
    else:
        ax.pcolormesh(x, y, c, **kw)


def render(file: str, group: str, var: str, idx: dict, bbox: list[float], qa_min: float | None,
           vmin: float, vmax: float, cmap: str, log: bool, w: int, h: int) -> dict:
    """非規則網格畫成透明 PNG,範圍就是 bbox(等距經緯度),前端貼在座標軸上。"""
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    path = str(resolve(file))
    ds = dataset(path, group)
    da = ds[var]
    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    sel = _sel(da, idx, ydim, xdim)
    sw = _swath(ds, (path, group), var, ydim, xdim, lat, lon, sel, bbox, qa_min, MAX_QUADS)
    w, h = max(64, min(int(w), 6000)), max(64, min(int(h), 6000))   # 前端會多畫畫面外一圈,尺寸約兩倍
    with _draw_lock:
        fig = Figure(figsize=(w / 100, h / 100), dpi=100)
        FigureCanvasAgg(fig)
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_axis_off()
        ax.set_xlim(bbox[0], bbox[2])
        ax.set_ylim(bbox[1], bbox[3])
        step = 1
        if sw is not None:
            lo, la, v, ok, step, edges = sw
            _draw_swath(ax, lo, la, _log(v, log), ok,
                        dict(cmap=_cmap(cmap), vmin=vmin, vmax=vmax,
                             antialiased=False, linewidth=0, rasterized=True), edges=edges)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', transparent=True, dpi=100)
    return {'png': base64.b64encode(buf.getvalue()).decode(), 'bbox': bbox, 'step': step,
            'corners': bool(sw is not None and sw[5] is not None)}


PROJECTIONS = {
    'plate': '等距經緯', 'robinson': 'Robinson', 'mollweide': 'Mollweide',
    'north': '北極', 'south': '南極', 'ortho': '正射(地球儀)',
}


def _projection(name: str, clon: float = 0.0, clat: float = 0.0):
    import cartopy.crs as ccrs
    return {
        'plate': lambda: ccrs.PlateCarree(),
        'robinson': lambda: ccrs.Robinson(central_longitude=clon),
        'mollweide': lambda: ccrs.Mollweide(central_longitude=clon),
        'north': lambda: ccrs.NorthPolarStereo(central_longitude=clon),
        'south': lambda: ccrs.SouthPolarStereo(central_longitude=clon),
        'ortho': lambda: ccrs.Orthographic(central_longitude=clon, central_latitude=max(-89, min(89, clat))),
    }[name if name in PROJECTIONS else 'plate']()


def _extent_lonlat(prj, extent) -> list[float] | None:
    """投影座標的視野 → 涵蓋它的經緯度範圍(取樣邊框與內部的點反投影),好只讀那一塊。"""
    import cartopy.crs as ccrs
    x0, x1, y0, y1 = extent
    xs, ys = np.meshgrid(np.linspace(x0, x1, 41), np.linspace(y0, y1, 41))
    pts = ccrs.PlateCarree().transform_points(prj, xs.ravel(), ys.ravel())
    lo, la = pts[:, 0], pts[:, 1]
    ok = np.isfinite(lo) & np.isfinite(la)
    if not ok.any():
        return None
    lo, la = lo[ok], la[ok]
    if lo.max() - lo.min() > 300:     # 視野跨過換日線或包住極點:經度不裁
        return [-180.0, float(max(-90, la.min() - 1)), 180.0, float(min(90, la.max() + 1))]
    return [float(max(-180, lo.min() - 1)), float(max(-90, la.min() - 1)),
            float(min(180, lo.max() + 1)), float(min(90, la.max() + 1))]


def _clamp_extent(prj, extent) -> list[float] | None:
    """投影座標範圍夾進投影的有效邊界(prj.x_limits / y_limits);整個落在外面就回 None(畫全圖)。"""
    if not all(math.isfinite(v) for v in extent):
        return None
    (xl0, xl1), (yl0, yl1) = prj.x_limits, prj.y_limits
    x0, x1 = sorted(extent[:2])
    y0, y1 = sorted(extent[2:])
    x0, x1, y0, y1 = max(x0, xl0), min(x1, xl1), max(y0, yl0), min(y1, yl1)
    if x1 - x0 <= (xl1 - xl0) * 1e-6 or y1 - y0 <= (yl1 - yl0) * 1e-6:
        return None
    return [x0, x1, y0, y1]


def unproject(proj: str, clon: float, clat: float, x: float, y: float) -> dict:
    import cartopy.crs as ccrs
    p = ccrs.PlateCarree().transform_point(x, y, _projection(proj, clon, clat))
    if not all(math.isfinite(v) for v in p):
        raise ValueError('這個位置不在地球上')
    return {'lon': float(p[0]), 'lat': float(p[1])}


def figure(file: str, group: str, var: str, idx: dict, bbox: list[float] | None, qa_min: float | None,
           vmin: float, vmax: float, cmap: str, log: bool, proj: str, w: int, h: int, dpi: int,
           title: str, subtitle: str, job: dict | None = None, clon: float = 0.0, clat: float = 0.0,
           extent: list[float] | None = None, bare: bool = False, texture: bool = False,
           unit: tuple[float, float, str] | None = None, layers: frozenset = frozenset({'coast'}), credit: bool = False):
    """整張地圖(cartopy)。

    bare=True:互動畫面用,只有資料+海岸線、鋪滿整張、透明以外的部分留白,回傳
    (png, 實際的投影座標範圍) —— 前端把它貼在以投影座標為軸的畫布上,框選放大時
    送回新的 extent。bare=False:匯出圖,含標題、色階條、經緯線標籤。
    texture=True:給瀏覽器 WebGL 地球儀貼在球面上的全球等距經緯貼圖(2:1、陸地底色、
    海岸線、不畫經緯線),旋轉與縮放都在瀏覽器裡做,不用回伺服器重畫。
    """
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    path = str(resolve(file))
    ds = dataset(path, group)
    da = ds[var]
    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    if not (lat and lon):
        raise ValueError('這個變數沒有經緯度,不能畫地圖投影')
    sel = _sel(da, idx, ydim, xdim)
    prj = _projection(proj, clon, clat)
    pc = ccrs.PlateCarree()
    if extent:
        extent = _clamp_extent(prj, extent)
    if proj == 'plate':
        region = bbox
    elif extent:
        region = _extent_lonlat(prj, extent)
    else:
        region = None   # 其他投影的全圖
    cm = _cmap(cmap)
    w, h = max(200, min(int(w), 8192)), max(150, min(int(h), 8192))
    dpi = max(72, min(int(dpi), 300))

    if job is not None:   # 多軌/多日合成:直接用分格後的規則網格
        g = composite_grid(job['id'], region, job.get('mode', 'mean'), job.get('res'), max_px=1600)
        data = ('grid', (np.asarray(g['_x']), np.asarray(g['_y']), g['_z']))
    elif kind == 'curvilinear':
        data = ('swath', _swath(ds, (path, group), var, ydim, xdim, lat, lon, sel, region, qa_min,
                                MAX_QUADS * 2 if texture else FIG_QUADS))
    else:
        x, y, z, _, _ = _regular_window(ds, (path, group), var, ydim, xdim, lat, lon, sel, region, 1600)
        data = ('grid', (x, y, z))

    with _draw_lock, matplotlib.rc_context({'font.sans-serif': CJK_FONTS, 'font.family': 'sans-serif'}):
        fig = Figure(figsize=(w / dpi, h / dpi), dpi=dpi)
        FigureCanvasAgg(fig)
        fig.patch.set_facecolor('white')
        ax = fig.add_axes((0, 0, 1, 1) if bare else (0.04, 0.16, 0.92, 0.72), projection=prj)
        if extent:
            # 直接設投影座標,不經過 set_extent(crs=prj) —— 它會把範圍換算一次,
            # 範圍超出地球(地球儀縮小、拖到外面)時換出 NaN
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        elif proj == 'plate' and bbox:
            ax.set_extent([bbox[0], bbox[2], bbox[1], bbox[3]], crs=pc)   # cartopy 要 x0, x1, y0, y1
        elif proj == 'north':
            ax.set_extent([-180, 180, 45, 90], crs=pc)
        elif proj == 'south':
            ax.set_extent([-180, 180, -90, -45], crs=pc)
        else:
            ax.set_global()
        if bare:
            # 固定成畫布的長寬比:前端以 1:1 的投影座標軸貼這張圖,兩邊必須同比例
            ax.set_aspect('auto')
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
            cx, cy, sx, sy = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
            if sx / sy > w / h:
                sy = sx * h / w
            else:
                sx = sy * w / h
            ax.set_xlim(cx - sx / 2, cx + sx / 2)
            ax.set_ylim(cy - sy / 2, cy + sy / 2)
        if texture:
            ax.set_facecolor('#f4f6f8')
            ax.add_feature(cfeature.LAND.with_scale('110m'), facecolor='#e2e5e9', edgecolor='none', zorder=0)
        kw = dict(cmap=cm, vmin=vmin, vmax=vmax, rasterized=True)
        mappable = None
        if data[0] == 'swath' and data[1] is not None:
            lo, la, v, ok, _, edges = data[1]
            _draw_swath(ax, lo, la, _log(v, log), ok, kw, transform=pc, edges=edges)
            mappable = ax.collections[-1] if ax.collections else None
        elif data[0] == 'grid':
            x, y, z = data[1]
            mappable = ax.pcolormesh(x, y, np.ma.masked_invalid(_log(z.astype('float64'), log)),
                                     transform=pc, shading='nearest', **kw)
        span = (region[2] - region[0]) if region else 360
        cbox = list(region) if region else [-180, -90, 180, 90]
        geoms = coast_geoms(cbox)[0] if 'coast' in layers else []
        if geoms:
            ax.add_feature(cfeature.ShapelyFeature(geoms, pc), facecolor='none', edgecolor='#222',
                           linewidth=0.8 if texture else 0.6)
        if 'borders' in layers:
            ax.add_feature(cfeature.ShapelyFeature(_border_geoms(_border_res(span)), pc),
                           facecolor='none', edgecolor='#555', linewidth=0.45, linestyle=(0, (4, 2)))
        # 經緯線沒勾:匯出圖仍有經緯度刻度標籤,只是不畫線
        gl = ax.gridlines(draw_labels=(proj == 'plate' and not bare), linewidth=0.3, color='#999',
                          alpha=0.6 if 'grid' in layers and not texture else 0.0)
        if proj == 'plate' and not bare:
            gl.top_labels = gl.right_labels = False
            gl.xlabel_style = gl.ylabel_style = {'size': 8}
        if not bare:
            if mappable is not None:
                cax = fig.add_axes((0.2, 0.07, 0.6, 0.025))
                # 有資料超出色階範圍時,色階條兩端畫成箭頭:一看就知道顏色已經飽和
                arr = mappable.get_array()
                arr = np.asarray(arr.compressed() if hasattr(arr, 'compressed') else arr, dtype='float64')
                arr = arr[np.isfinite(arr)]
                lo_out = bool(arr.size and arr.min() < vmin)
                hi_out = bool(arr.size and arr.max() > vmax)
                extend = 'both' if lo_out and hi_out else 'min' if lo_out else 'max' if hi_out else 'neither'
                cb = fig.colorbar(mappable, cax=cax, orientation='horizontal', extend=extend)
                # 覆蓋數是檔數/天數,沒有資料的單位
                counting = bool(job and job.get('mode') in ('count', 'bcount'))
                units = '' if counting else clean_units(da.attrs.get('units') or da.attrs.get('unit', ''))
                if unit and not counting:   # 使用者選的顯示單位:資料仍是原始單位,只換刻度數字與標籤
                    ua, ub, units = unit
                else:
                    ua, ub = 1.0, 0.0
                cb.set_label(('log10 ' if log else '') + _mpl_units(units), fontsize=8)
                cb.ax.tick_params(labelsize=8)
                # 刻度寫法跟畫面一致:很小或很大的數用 ×10ⁿ,不要 1e−5 或 0.00005
                from matplotlib.ticker import ScalarFormatter
                if (ua, ub) == (1.0, 0.0):
                    fmt = ScalarFormatter(useMathText=True)
                    fmt.set_powerlimits((-3, 4))
                    cb.formatter = fmt
                else:
                    from matplotlib.ticker import FixedLocator, FuncFormatter
                    # 刻度放在換算後的「整數」位置:先在顯示單位裡挑好刻度,再換回原始單位擺放
                    conv = (lambda x: x + math.log10(ua)) if log else (lambda x: x * ua + ub)
                    back = (lambda y: y - math.log10(ua)) if log else (lambda y: (y - ub) / ua)
                    lo_d, hi_d = sorted((conv(vmin), conv(vmax)))
                    from matplotlib.ticker import MaxNLocator
                    ticks = [t for t in MaxNLocator(6).tick_values(lo_d, hi_d) if lo_d <= t <= hi_d]
                    cb.locator = FixedLocator([back(t) for t in ticks])   # 設在 colorbar 上:update_ticks 會用它
                    # 很大或很小的數:刻度寫成係數,共同的 ×10ⁿ 併到標籤(跟畫面的寫法一樣)
                    big = max((abs(t) for t in ticks), default=0)
                    e = int(math.floor(math.log10(big))) if big and (big >= 1e4 or big < 1e-3) and not log else 0
                    cb.formatter = FuncFormatter(lambda x, _: f'{conv(x) / 10 ** e:.3g}')
                    if e:
                        cb.set_label(f'$\\times10^{{{e}}}$ ' + _mpl_units(units), fontsize=8)
                cb.update_ticks()
            fig.text(0.5, 0.955, title, ha='center', va='center', fontsize=10, fontweight='bold', wrap=True)
            if subtitle:
                fig.text(0.5, 0.915, subtitle, ha='center', va='center', fontsize=8, color='#555')
            if credit:   # 設定 › 匯出 PNG 角落標上 ncglobe
                fig.text(0.995, 0.005, 'ncglobe', ha='right', va='bottom', fontsize=6, color='#999')
        got = [*ax.get_xlim(), *ax.get_ylim()]
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=dpi, facecolor='white')
    return buf.getvalue(), [float(v) for v in got]


# ------------------------------------------------------------------ 地球儀圖磚(Web Mercator z/x/y)

TILE_PX = 512
_tile_lock = threading.Lock()
_tiles: OrderedDict[str, bytes] = OrderedDict()
MERC_LAT = 85.0511


def _merc(lat):
    la = np.clip(np.asarray(lat, dtype='float64'), -MERC_LAT, MERC_LAT)
    return np.log(np.tan(np.pi / 4 + np.radians(la) / 2))


def tile(key: str, file: str, group: str, var: str, idx: dict, qa_min, vmin: float, vmax: float, cmap: str,
         log: bool, z: int, x: int, y: int, job: dict | None, layers: frozenset = frozenset({'coast'})) -> bytes:
    """一張地球儀圖磚:這一格(Web Mercator)範圍內的資料 + 勾選的圖層(海岸線、國界、經緯線)。

    整個地球一張貼圖放大就糊;改成圖磚後,globe.gl 依縮放層級只要畫面上那幾張,
    放大多少就細多少。資料走同一套快取(L2 的陣列、角點都已在記憶體),一張約幾十毫秒。
    """
    with _tile_lock:
        hit = _tiles.get(key)
        if hit is not None:
            _tiles.move_to_end(key)
            return hit
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    n = 2 ** z
    lon0, lon1 = x / n * 360 - 180, (x + 1) / n * 360 - 180
    ym1 = math.pi * (1 - 2 * y / n)          # 上緣(Mercator y)
    ym0 = math.pi * (1 - 2 * (y + 1) / n)    # 下緣
    lat0, lat1 = math.degrees(math.atan(math.sinh(ym0))), math.degrees(math.atan(math.sinh(ym1)))
    bbox = [lon0, lat0, lon1, lat1]
    span = max(lon1 - lon0, lat1 - lat0)

    path = str(resolve(file))
    ds = dataset(path, group)
    da = ds[var]
    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    sel = _sel(da, idx, ydim, xdim)
    data = None
    if job is not None:
        g = composite_grid(job['id'], bbox, job.get('mode', 'mean'), job.get('res'), max_px=TILE_PX)
        data = ('grid', (np.asarray(g['_x']), np.asarray(g['_y']), g['_z']))
    elif kind == 'curvilinear':
        sw = _swath(ds, (path, group), var, ydim, xdim, lat, lon, sel, bbox, qa_min, TILE_PX * TILE_PX)
        data = ('swath', sw) if sw is not None else None
    elif lat and lon:
        gx, gy, gz, _, _ = _regular_window(ds, (path, group), var, ydim, xdim, lat, lon, sel, bbox, TILE_PX)
        data = ('grid', (gx, gy, gz))

    with _draw_lock:
        fig = Figure(figsize=(TILE_PX / 100, TILE_PX / 100), dpi=100)
        FigureCanvasAgg(fig)
        fig.patch.set_facecolor('#e9ecf0')
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_axis_off()
        ax.set_xlim(lon0, lon1)
        ax.set_ylim(ym0, ym1)
        cm = _cmap(cmap)
        if data and data[0] == 'swath':
            lo, la, v, ok, _, edges = data[1]
            e = None if edges is None else np.stack([edges[0], _merc(edges[1])])
            _draw_swath(ax, lo, _merc(la), _log(v, log), ok,
                        dict(cmap=cm, vmin=vmin, vmax=vmax, antialiased=False, linewidth=0), edges=e)
        elif data:
            gx, gy, gz = data[1]
            if gz.size:
                ax.pcolormesh(gx, _merc(gy), np.ma.masked_invalid(_log(gz.astype('float64'), log)),
                              cmap=cm, vmin=vmin, vmax=vmax, shading='nearest')
        o = outlines(bbox, layers) if layers & {'coast', 'borders'} else {}
        if 'grid' in layers:   # 經緯線:同一縮放層級每張圖磚用同一個間隔,接起來才連續
            step = _nice_step(span / 3)
            gl = [(np.array([v, v]), np.array([bbox[1], bbox[3]]))
                  for v in np.arange(math.ceil(bbox[0] / step) * step, bbox[2] + 1e-9, step)]
            gl += [(np.array([bbox[0], bbox[2]]), np.array([v, v]))
                   for v in np.arange(math.ceil(bbox[1] / step) * step, bbox[3] + 1e-9, step) if abs(v) < 85]
            for gx, gy in gl:
                ax.plot(gx, _merc(gy), color='#888', linewidth=0.5, alpha=0.7)
        for name, color, width in (('borders', '#555', 0.6), ('coast', '#222', 0.9)):
            if name in o and o[name]['x']:
                xs = np.array([np.nan if v is None else v for v in o[name]['x']], dtype='float64')
                ys = np.array([np.nan if v is None else v for v in o[name]['y']], dtype='float64')
                ax.plot(xs, _merc(np.nan_to_num(ys, nan=0.0)) * np.where(np.isnan(ys), np.nan, 1),
                        color=color, linewidth=width * (1.4 if span < 5 else 1))
        buf = io.BytesIO()
        # 自己存 PNG:壓縮等級 1 比 matplotlib 預設(6)快好幾倍,圖磚只差幾 KB
        from PIL import Image
        canvas = fig.canvas
        canvas.draw()
        Image.frombuffer('RGBA', canvas.get_width_height(), canvas.buffer_rgba()).save(buf, 'PNG', compress_level=1)
    png = buf.getvalue()
    with _tile_lock:
        _tiles[key] = png
        while len(_tiles) > 3000:
            _tiles.popitem(last=False)
    return png


# ------------------------------------------------------------------ 哪些檔經過目前視野

_FOOT: OrderedDict = OrderedDict()
_foot_lock = threading.Lock()


def footprint(path: str, group: str, var: str):
    """檔案的大致涵蓋範圍:逐軌資料取每 8×8 個像素一點的經緯度,規則網格取外框。
    只為了回答「這個檔經不經過畫面」,所以很粗、很小,快取起來重複用。"""
    key = (path, group, var)
    with _foot_lock:
        if key in _FOOT:
            return _FOOT[key]
    ds = dataset(path, group)
    if var not in ds.variables:
        fp = None
    else:
        ydim, xdim, kind, lat, lon = plot_axes(ds, var)
        if not (lat and lon):
            fp = None
        elif kind == 'curvilinear':
            la, lo = ds[lat], ds[lon]
            first = {d: 0 for d in la.dims if d not in (ydim, xdim)}
            a = np.asarray(la.isel(first).values, 'float32')[::8, ::8]
            o = np.asarray(lo.isel(first).values, 'float32')[::8, ::8]
            ok = np.isfinite(a) & np.isfinite(o)
            fp = ('pts', o[ok], a[ok])
        else:
            a, o = np.asarray(ds[lat].values, 'float64'), np.asarray(ds[lon].values, 'float64')
            o = np.where(o > 180, o - 360, o)
            fp = ('box', [float(np.nanmin(o)), float(np.nanmin(a)), float(np.nanmax(o)), float(np.nanmax(a))])
    with _foot_lock:
        _FOOT[key] = fp
        while len(_FOOT) > 5000:
            _FOOT.popitem(last=False)
    return fp


def passes(files: list[str], group: str, var: str, bbox: list[float]) -> dict:
    """每個檔經不經過 bbox:True / False;讀不到或沒有這個變數是 None。"""
    x0, y0, x1, y1 = bbox
    out = {}
    for f in files[:400]:
        try:
            fp = footprint(str(resolve(f)), group, var)
        except Exception:
            fp = None
        if fp is None:
            out[f] = None
        elif fp[0] == 'pts':
            o, a = fp[1], fp[2]
            pad = 0.5   # 取樣點間隔約 0.4°(S5P 8 個像素),邊上的像素才不會漏掉
            out[f] = bool(((o >= x0 - pad) & (o <= x1 + pad) & (a >= y0 - pad) & (a <= y1 + pad)).any())
        else:
            b = fp[1]
            out[f] = not (b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1)
    return out


# ------------------------------------------------------------------ 點選查值

def point(file: str, group: str, var: str, idx: dict, x: float, y: float, qa_min=None) -> dict:
    """點一個位置:最近像素/格點的所有同維度變數值,規則網格再加時間序列。

    note:點到的像素在地圖上其實沒畫(被 qa 篩掉、或離軌道太遠)時說明原因,
    不讓使用者把一個沒畫出來的值當成那一點的值。
    """
    path = str(resolve(file))
    ds = dataset(path, group)
    key = (path, group)
    da = ds[var]
    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    sel = _sel(da, idx, ydim, xdim)
    note = None

    if kind == 'curvilinear':
        la, lo = full2d(ds, key, lat, sel, ydim, xdim), full2d(ds, key, lon, sel, ydim, xdim)
        dlon = (lo - x + 180) % 360 - 180
        d2 = (la - y) ** 2 + (dlon * math.cos(math.radians(y))) ** 2
        r, c = np.unravel_index(int(np.nanargmin(d2)), d2.shape)
        at = {ydim: int(r), xdim: int(c)}
        dist = float(math.sqrt(d2[r, c]) * 111.2)
        where = {'經度': float(lo[r, c]), '緯度': float(la[r, c]), '離點選處(km)': round(dist, 1),
                 f'{ydim} 索引': int(r), f'{xdim} 索引': int(c)}
        if dist > 15:
            note = f'點選處不在軌道上:最近的像素在 {dist:,.0f} km 外,下面是那個像素的值'
        elif qa_min is not None and 'qa_value' in ds.variables:
            q = float(full2d(ds, key, 'qa_value', sel, ydim, xdim)[r, c])
            if not q >= qa_min:
                note = f'這個像素 qa_value = {q:.2f} < {qa_min:g},地圖上已被篩掉;下面是它的原始值'
    else:
        xc, yc, order = _regular_axes(ds, da, ydim, xdim, lat, lon)
        ix = int(np.nanargmin(np.abs(xc - x)))
        iy = int(np.nanargmin(np.abs(yc - y)))
        ixo = int(order[ix]) if order is not None else ix
        at = {ydim: iy, xdim: ixo}
        where = {'經度' if lon else xdim: float(xc[ix]), '緯度' if lat else ydim: float(yc[iy]),
                 f'{ydim} 索引': iy, f'{xdim} 索引': ixo}

    values = []
    for name, v in ds.data_vars.items():
        if not {ydim, xdim} <= set(v.dims):
            continue
        rest = [d for d in v.dims if d not in (ydim, xdim) and d not in sel]
        if rest:   # 還有別的維度(例如 layer)的變數,單點值是一串,略過
            continue
        try:
            val = v.isel({**{d: s for d, s in sel.items() if d in v.dims}, **at}).values
            values.append({'name': name, 'value': jsonable(np.asarray(val).item()),
                           'units': clean_units(v.attrs.get('units') or v.attrs.get('unit', '')), 'current': name == var})
        except Exception:
            continue
        if len(values) >= 80:
            break

    series = None
    extra = [d for d in da.dims if d not in (ydim, xdim) and ds.sizes[d] > 1]
    if extra:
        d = extra[0]
        n = ds.sizes[d]
        step = max(1, math.ceil(n / SERIES_MAX))
        other = {k: s for k, s in sel.items() if k != d}
        ys = da.isel({**other, **at}).isel({d: slice(None, None, step)}).values
        xs = coord_labels(ds[d].values[::step]) if d in ds.coords else list(range(0, n, step))
        k = sel.get(d, 0) // step
        series = {'dim': d, 'x': xs, 'y': arr_to_list(ys), 'step': step,
                  'units': clean_units(da.attrs.get('units') or da.attrs.get('unit', '')), 'current': xs[k] if k < len(xs) else None}
    return {'where': where, 'values': values, 'series': series, 'note': note}


# ------------------------------------------------------------------ 多軌 / 多日合成

COMPOSITE_MAX_POINTS = 60_000_000   # 所有檔的有效點加起來的上限(float32 × 3 ≈ 720 MB)
COMPOSITE_REGULAR_MAX = 5_000_000   # 規則網格的檔:每個檔最多取這麼多格(太大請先放大)
_jobs_lock = threading.Lock()
JOBS: OrderedDict[str, dict] = OrderedDict()


class OutsideRange(ValueError):
    """這個檔在合成範圍裡沒有有效像素:軌道沒經過、或全被 qa 篩掉。不算錯誤,只計數。"""


def _job_points(path: str, group: str, var: str, sel_idx: dict, qa_min, bbox):
    """一個檔在 bbox 內的二維視窗 (lon, lat, value),無效值是 NaN。

    保留二維(像素鄰接關係),分格時才能從相鄰像素中心推出每個像素的四個角。
    """
    ds = dataset(path, group)
    if var not in ds.variables:
        raise KeyError(f'沒有變數 {var}')
    da = ds[var]
    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    if not (lat and lon):
        raise ValueError('沒有經緯度')
    sel = _sel(da, sel_idx, ydim, xdim)
    key = (path, group)
    if kind == 'curvilinear':
        lo, la = full2d(ds, key, lon, sel, ydim, xdim), full2d(ds, key, lat, sel, ydim, xdim)
        v = full2d(ds, key, var, sel, ydim, xdim).astype('float32')
        ok = np.isfinite(v)
        if qa_min is not None and 'qa_value' in ds.variables:
            ok &= full2d(ds, key, 'qa_value', sel, ydim, xdim) >= qa_min
    else:
        x, y, z, _, native = _regular_window(ds, key, var, ydim, xdim, lat, lon, sel, bbox, 10**9)
        if native[0] * native[1] > COMPOSITE_REGULAR_MAX:
            raise ValueError(f'範圍內有 {native[0] * native[1]:,} 格,太大;請先放大到要合成的區域')
        lo, la = np.meshgrid(x, y)
        v = z.astype('float32')
        ok = np.isfinite(v)
    edges = edge_grid(path, group, sel, ydim, xdim) if kind == 'curvilinear' else None
    inside = np.isfinite(lo) & np.isfinite(la)
    if bbox:
        pad = 0.5
        inside &= (lo >= bbox[0] - pad) & (lo <= bbox[2] + pad) & (la >= bbox[1] - pad) & (la <= bbox[3] + pad)
    rows, cols = np.flatnonzero(inside.any(axis=1)), np.flatnonzero(inside.any(axis=0))
    if not rows.size or not (ok & inside).any():
        raise OutsideRange('範圍內沒有有效資料')
    w = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
    vv = np.where(ok, v, np.nan)[w].astype('float32')
    ew = edges[:, rows[0]:rows[-1] + 2, cols[0]:cols[-1] + 2].astype('float32') if edges is not None else None
    return np.asarray(lo[w], 'float32'), np.asarray(la[w], 'float32'), vv, ew


def _corners(c: np.ndarray) -> np.ndarray:
    """像素中心 (ny, nx) → 像素角點 (ny+1, nx+1):先往外線性外插一圈,再取相鄰四點平均。"""
    c = c.astype('float64')
    e = np.empty((c.shape[0] + 2, c.shape[1] + 2))
    e[1:-1, 1:-1] = c
    e[0, 1:-1] = 2 * c[0] - c[1]
    e[-1, 1:-1] = 2 * c[-1] - c[-2]
    e[:, 0] = 2 * e[:, 1] - e[:, 2]
    e[:, -1] = 2 * e[:, -2] - e[:, -3]
    return (e[:-1, :-1] + e[1:, :-1] + e[:-1, 1:] + e[1:, 1:]) / 4


# 一個檔一次分格最多產生幾個取樣點。K 由「像素大小 ÷ 格距」決定,視野內的取樣點數大約是
# 4 × MAX_PX²(與縮放無關),所以平常碰不到這個上限;碰到時 K 會變小,同一格的值就會因
# 視野不同而略有差異 —— 點選與滑鼠讀值要跟畫面一致,所以上限設得很寬。
SUPERSAMPLE_BUDGET = 20_000_000


def _pixel_size(lo, la) -> float:
    """像素大約幾度(取相鄰像素中心距離的中位數)。"""
    lon = lo.astype('float64')
    d = np.abs(np.diff(lon, axis=1)) if lon.shape[1] > 1 else np.array([np.nan])
    d = np.where(d > 180, 360 - d, d)
    pix = float(np.nanmedian(d)) if np.isfinite(d).any() else 0.0
    if la.shape[0] > 1:
        pix = max(pix, float(np.nanmedian(np.abs(np.diff(la.astype('float64'), axis=0)))))
    return pix


def _row_boxes(lo, la) -> np.ndarray:
    """每一列(掃描線)的 [經度最小, 最大, 緯度最小, 最大]。跨換日線的列經度範圍會變成整圈,只是保守、不會漏。"""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)   # 整列都是 NaN
        out = np.stack([np.nanmin(lo, axis=1), np.nanmax(lo, axis=1), np.nanmin(la, axis=1), np.nanmax(la, axis=1)], axis=1)
    out[:, [0, 2]] = np.nan_to_num(out[:, [0, 2]], nan=np.inf)
    out[:, [1, 3]] = np.nan_to_num(out[:, [1, 3]], nan=-np.inf)
    return out


def _supersample(lo, la, v, res, edges=None, pix=None):
    """每個有效像素在自己的四邊形內取 K×K 個點(K 依網格解析度決定),回傳一維 (x, y, value)。

    只丟像素中心進網格,格子比像素細時會有一半的格分不到點(滿地洞);
    這跟 L3 管線的 footprint 超取樣是同一個想法。
    """
    ok = np.isfinite(v)
    if lo.shape[0] < 2 or lo.shape[1] < 2:
        return lo[ok], la[ok], v[ok]
    lon = lo.astype('float64')
    wrap = np.nanmax(np.abs(np.diff(lon, axis=1))) > 180 if lon.shape[1] > 1 else False
    if wrap:   # 跨換日線:先展開成連續經度再推角點
        lon = np.where(lon < 0, lon + 360, lon)
    if edges is not None:   # 檔案的真實角點
        ex = edges[0].astype('float64')
        ey = edges[1].astype('float64')
        if wrap:
            ex = np.where(ex < 0, ex + 360, ex)
    else:                   # 沒有角點:由相鄰像素中心推
        ex, ey = _corners(lon), _corners(la)
    if pix is None:   # 呼叫端最好用整個檔算好傳進來:裁切範圍不同,中位數就不同,K 跟著變、同一格的值也變
        pix = _pixel_size(lo, la) or res
    i, j = np.nonzero(ok)
    # 每個像素取 K×K 點,K 讓取樣間距 ≤ 半格(否則格子比取樣點密,鋪不滿 → 洞)。
    # 放大時格子很細但範圍內像素很少,K 可以拉高;總點數封頂 SUPERSAMPLE_BUDGET。
    k_need = math.ceil(2 * pix / max(res, 1e-6))
    k_cap = max(1, int(math.sqrt(SUPERSAMPLE_BUDGET / max(len(i), 1))))
    k = int(np.clip(k_need, 1, min(40, k_cap)))
    a = (ex[i, j], ey[i, j]); b = (ex[i, j + 1], ey[i, j + 1])
    c = (ex[i + 1, j], ey[i + 1, j]); d = (ex[i + 1, j + 1], ey[i + 1, j + 1])
    vals = v[i, j]
    xs, ys, zs = [], [], []
    for si in range(k):
        s_ = (si + 0.5) / k
        for ti in range(k):
            t_ = (ti + 0.5) / k
            w00, w01, w10, w11 = (1 - s_) * (1 - t_), s_ * (1 - t_), (1 - s_) * t_, s_ * t_
            xs.append(w00 * a[0] + w01 * b[0] + w10 * c[0] + w11 * d[0])
            ys.append(w00 * a[1] + w01 * b[1] + w10 * c[1] + w11 * d[1])
            zs.append(vals)
    x = np.concatenate(xs)
    if wrap:
        x = (x + 180) % 360 - 180
    return x, np.concatenate(ys), np.concatenate(zs)


_S5P_NAME = __import__('re').compile(r'_(\d{8}T\d{6})_(\d{8}T\d{6})_(\d{5})_')
_S5P_FULL = __import__('re').compile(r'S5P_(NRTI|OFFL|RPRO)_.*?_(\d{5})_(\d{2})_(\d{6})_(\d{8}T\d{6})')
_PROC_RANK = {'RPRO': 2, 'OFFL': 1, 'NRTI': 0}


def _dedupe_orbits(paths: list[str]) -> tuple[list[str], list[str]]:
    """同一軌出現好幾次(NRTI / OFFL / RPRO、或重新處理過)只留一套:RPRO > OFFL > NRTI。

    NRTI 一軌切成好幾個 5 分鐘的檔(同一個軌道號、起始時間不同),那是同一軌的不同段,
    不是重複:同一軌只用最好的處理器那一套,套內同一段(起始時間相同)取處理時間最新的。
    混用不同處理器或演算法版本也提醒,數字可能有系統差。"""
    by_orbit: dict[str, list] = {}
    other = []
    for p in paths:
        name = Path(p).name
        m, t = _S5P_FULL.search(name), _S5P_NAME.search(name)
        if not (m and t):
            other.append(p)
            continue
        proc, orbit, _coll, ver, made = m.groups()
        by_orbit.setdefault(orbit, []).append((_PROC_RANK[proc], t.group(1), made, p, proc, ver))
    best: dict[tuple, tuple] = {}
    for orbit, items in by_orbit.items():
        top = max(i[0] for i in items)
        for rank, start, made, p, proc, ver in items:
            if rank != top:
                continue
            k = (orbit, start)
            if k not in best or made > best[k][0]:
                best[k] = (made, p, proc, ver)
    kept = [v[1] for v in best.values()] + other
    notes = []
    dropped = len(paths) - len(kept)
    if dropped:
        notes.append(f'同一軌有重複的檔(不同處理器或重新處理),去掉 {dropped} 個,每軌只留 RPRO > OFFL > NRTI 中最新的一個')
    procs = sorted({v[2] for v in best.values()})
    vers = sorted({v[3] for v in best.values()})
    if len(procs) > 1:
        notes.append(f'混用了不同處理器:{"、".join(procs)}')
    if len(vers) > 1:
        notes.append(f'混用了不同演算法版本:{"、".join(vers)},數值可能有系統差')
    modes = sorted({m for p in kept if (m := file_meta(Path(p).name).get('mode'))})
    if len(modes) > 1:
        notes.append(f'混用了 GEMS 不同掃描模式:{"、".join(modes)}(涵蓋範圍不同)')
    order = {p: i for i, p in enumerate(paths)}
    return sorted(kept, key=order.get), notes
_GEMS_NAME = __import__('re').compile(r'GEMS_L2_(\d{8})_(\d{4})_[A-Z0-9]+_([A-Z]+(?:-[A-Z]+)?)_')
_MODIS_TILE = __import__('re').compile(r'\.A(\d{4})(\d{3})\.(h\d\dv\d\d)\.')
_MODIS_NAME = __import__('re').compile(r'\.A(\d{4})(\d{3})\.(\d{4})\.')
_DATE8 = __import__('re').compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)')


def file_meta(name: str) -> dict:
    """從檔名讀出時間:S5P 有起訖時間與軌道號;其他檔抓第一個 8 位數日期。"""
    m = _S5P_NAME.search(name)
    if m:
        f = lambda t: f'{t[:4]}-{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]}'
        out = {'start': f(m.group(1)), 'end': f(m.group(2)), 'orbit': int(m.group(3)), 'date': f(m.group(1))[:10]}
        full = _S5P_FULL.search(name)
        if full:
            out.update(proc=full.group(1), ver=full.group(4))
        return out
    m = _GEMS_NAME.search(name)          # GK2_GEMS_L2_20260614_0345_NO2_FC_…:掃描開始 UTC、掃描模式
    if m:
        d, t, mode = m.groups()
        return {'start': f'{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:]}', 'date': f'{d[:4]}-{d[4:6]}-{d[6:]}', 'mode': mode}
    m = _MODIS_TILE.search(name)         # MCD19A2.A2025244.h28v06.…:年 + 年中第幾天 + 正弦投影格點
    if m:
        y, j, tile = m.groups()
        return {'date': str(np.datetime64(f'{y}-01-01') + np.timedelta64(int(j) - 1, 'D')), 'tile': tile}
    m = _MODIS_NAME.search(name)         # MYD04_L2.A2025073.0650.…:年 + 年中第幾天 + UTC
    if m:
        y, j, t = m.groups()
        day = str(np.datetime64(f'{y}-01-01') + np.timedelta64(int(j) - 1, 'D'))
        return {'start': f'{day} {t[:2]}:{t[2:]}', 'date': day}
    d = _DATE8.search(name)
    return {'date': f'{d.group(1)}-{d.group(2)}-{d.group(3)}' if d else None}


def _run_job(job: dict) -> None:
    for i, (which, f) in enumerate(job['files']):
        if job['cancel']:
            job['status'] = 'cancelled'
            return
        job['current'] = Path(f).name
        try:
            try:
                lo, la, v, edges = _job_points(f, job['group'], job['var'], job['idx'], job['qa'], job['bbox'])
            except RuntimeError as e:
                if not stale_handle(e):
                    raise
                lo, la, v, edges = _job_points(f, job['group'], job['var'], job['idx'], job['qa'], job['bbox'])
            meta = file_meta(Path(f).name)
            job['pts'][which].append((lo, la, v, edges))
            job['pix'][which].append(_pixel_size(lo, la))   # 每個檔算一次:分格時每張圖磚都要用
            job['rows'][which].append(_row_boxes(lo, la))
            job['keys'][which].append(meta.get('date') or f)
            job['used_files'].append({'set': which, 'name': Path(f).name, 'path': f, **meta})
            job['n_points'] += int(np.isfinite(v).sum())
            job['used'] += 1
        except OutsideRange:
            job['outside'] += 1
        except Exception as e:
            job['errors'].append(f'{Path(f).name}: {type(e).__name__}: {e}')
        job['done'] = i + 1
        if job['n_points'] > COMPOSITE_MAX_POINTS:
            job['errors'].append(f'有效點超過 {COMPOSITE_MAX_POINTS:,},停在第 {i + 1} 個檔;請縮小範圍或少選幾個檔')
            break
    if job['diff'] and not (job['pts']['a'] and job['pts']['b']):
        job['errors'].append('A 或 B 沒有任何有效資料,無法相減')
    job['status'] = 'done'
    job['current'] = None


WEIGHTS = {'file': '每個檔等權', 'day': '每天等權(依 UTC 日期)', 'pixel': '每個像素等權'}


def composite_start(files: list[str], group: str, var: str, idx: dict, qa_min, bbox,
                    bfiles: list[str] | None = None, weight: str = 'file') -> dict:
    """合成工作。bfiles 給了就是相減:A = files 的平均、B = bfiles 的平均,兩邊先各自分格再相減
    (L2 兩軌的像素位置不同,只能在同一個網格上比)。

    weight:file = 每個檔先格平均再跨檔平均;day = 同一天的檔先合在一起(重疊處取像素平均)
    再跨天平均;pixel = 所有像素一起平均(觀測多的格子權重大)。
    """
    if not files:
        raise ValueError('沒有選檔')
    if weight not in WEIGHTS:
        raise ValueError(f'weight 只能是 {", ".join(WEIGHTS)}')
    a = [str(resolve(f)) for f in files]
    b = [str(resolve(f)) for f in (bfiles or [])]
    notes = []
    a, n_a = _dedupe_orbits(a)
    notes += n_a
    if b:
        overlap = set(a) & set(b)
        if overlap:   # A 也在 B 裡,差值會被拉向 0
            b = [p for p in b if p not in overlap]
            notes.append(f'B 裡有 {len(overlap)} 個檔跟 A 相同,已從 B 排除')
        if not b:
            raise ValueError('B 扣掉跟 A 相同的檔之後沒有檔了')
        b, n_b = _dedupe_orbits(b)
        notes += [f'B:{n}' for n in n_b]
    paths = [('a', p) for p in a] + [('b', p) for p in b]
    job = {'id': uuid.uuid4().hex[:12], 'files': paths, 'group': group, 'var': var, 'idx': idx, 'qa': qa_min,
           'bbox': bbox, 'pts': {'a': [], 'b': []}, 'pix': {'a': [], 'b': []}, 'rows': {'a': [], 'b': []}, 'keys': {'a': [], 'b': []}, 'diff': bool(b), 'weight': weight,
           'notes': notes, 'outside': 0, 'n_points': 0, 'done': 0, 'used': 0,
           'errors': [], 'status': 'running', 'cancel': False, 'current': None, 't0': time.monotonic(),
           'used_files': []}
    with _jobs_lock:
        JOBS[job['id']] = job
        while len(JOBS) > 3:   # 只留最近幾個,點資料可能幾百 MB
            _, old = JOBS.popitem(last=False)
            old['cancel'] = True
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return composite_status(job['id'])


def _job(job_id: str) -> dict:
    with _jobs_lock:
        job = JOBS.get(job_id)
    if job is None:
        raise KeyError('合成結果已過期,請重新合成')
    return job


def composite_status(job_id: str) -> dict:
    j = _job(job_id)
    return {'id': j['id'], 'status': j['status'], 'done': j['done'], 'total': len(j['files']), 'used': j['used'],
            'used_a': len(j['pts']['a']), 'used_b': len(j['pts']['b']), 'diff': j['diff'],
            'n_points': j['n_points'], 'errors': j['errors'][-20:], 'current': j['current'],
            'notes': j['notes'], 'outside': j['outside'], 'weight': j['weight'], 'weight_label': WEIGHTS[j['weight']], 'qa': j['qa'],
            'seconds': round(time.monotonic() - j['t0'], 1),
            'files': sorted(j['used_files'], key=lambda x: (x['set'], x.get('start') or x.get('date') or '', x['name']))
            if j['status'] != 'running' else None}


def composite_cancel(job_id: str) -> dict:
    _job(job_id)['cancel'] = True
    return {'ok': True}


def _grid_spec(job: dict, bbox, res, max_px):
    if bbox is None:
        pts = job['pts']['a'] + job['pts']['b']
        xs_all = [np.nanmin(p[0]) for p in pts] + [np.nanmax(p[0]) for p in pts]
        ys_all = [np.nanmin(p[1]) for p in pts] + [np.nanmax(p[1]) for p in pts]
        bbox = [float(min(xs_all)), float(min(ys_all)), float(max(xs_all)), float(max(ys_all))] if xs_all else [-180, -90, 180, 90]
    x0, y0, x1, y1 = bbox
    span = max(x1 - x0, y1 - y0, 1e-6)
    # 下限 0.002°:以前是 0.02°,放大到台灣還是 0.02° 的糊塊(單檔畫的是真實像素,合成只能分格)
    pix = [v for v in job['pix']['a'] + job['pix']['b'] if v and math.isfinite(v)]
    # 自動:至少半個像素寬(TROPOMI 約 0.03° → 0.015°);比像素細很多只是把同一個像素切成幾百格,回應幾 MB
    r = float(res) if res else max(0.002, span / max_px, 0.5 * float(np.median(pix)) if pix else 0)
    r = max(r, span / 4000)                       # 不要分出上億格
    # 格線對齊固定格點(0、r、2r…):視野、點選、滑鼠讀值給的範圍不同,格子還是同一批,值才對得上
    x0, y0 = math.floor(x0 / r) * r, math.floor(y0 / r) * r
    x1, y1 = math.ceil(x1 / r - 1e-9) * r, math.ceil(y1 / r - 1e-9) * r
    return x0, y0, x1, y1, r


def _bins(job: dict, bbox, res, max_px, which: str = 'a'):
    """依目前視野分格,依 job['weight'] 分組:每組先算格平均,再跨組平均。
    file:一個檔一組;day:同一天一組;pixel:全部一組(等於所有取樣點直接平均)。
    count = 這一格被幾組覆蓋(檔數 / 天數),std = 組與組之間的標準差。"""
    x0, y0, x1, y1, r = _grid_spec(job, bbox, res, max_px)
    nx = max(1, int(math.ceil((x1 - x0) / r)))
    ny = max(1, int(math.ceil((y1 - y0) / r)))
    nfile = np.zeros(ny * nx)
    s1 = np.zeros(ny * nx)
    s2 = np.zeros(ny * nx)
    pad = max(0.5, 2 * r)
    weight = job.get('weight', 'file')
    keys = job['keys'][which]
    group_of = (list(range(len(keys))) if weight == 'file' else keys if weight == 'day' else [0] * len(keys))
    gcnt: dict = {}
    gtot: dict = {}
    for (lo, la, v, edges), g, pix_file, rb in zip(job['pts'][which], group_of, job['pix'][which], job['rows'][which]):
        # 先用每列的經緯度外框挑出可能有關的列(很便宜),只在那幾列裡逐像素篩
        cand = np.flatnonzero((rb[:, 1] >= x0 - pad) & (rb[:, 0] <= x1 + pad) & (rb[:, 3] >= y0 - pad) & (rb[:, 2] <= y1 + pad))
        if not cand.size:
            continue
        q0, q1 = cand[0], cand[-1] + 1
        slo, sla = lo[q0:q1], la[q0:q1]
        # 先裁到視野附近再超取樣:放大或滑鼠讀值時只看一小塊,整個檔都超取樣再丟掉太浪費
        near = (slo >= x0 - pad) & (slo <= x1 + pad) & (sla >= y0 - pad) & (sla <= y1 + pad)
        rows, cols = np.flatnonzero(near.any(axis=1)), np.flatnonzero(near.any(axis=0))
        if not rows.size:
            continue
        r0, r1, c0, c1 = q0 + rows[0], q0 + rows[-1] + 1, cols[0], cols[-1] + 1
        pix = pix_file or r
        if (r1 - r0) * (c1 - c0) < lo.size:
            lo, la, v = lo[r0:r1, c0:c1], la[r0:r1, c0:c1], v[r0:r1, c0:c1]
            edges = edges[:, r0:r1 + 1, c0:c1 + 1] if edges is not None else None
        px, py, pv = _supersample(lo, la, v, r, edges, pix)
        m = (px >= x0) & (px < x1) & (py >= y0) & (py < y1)
        if not m.any():
            continue
        flat = np.clip(((py[m] - y0) / r).astype('int64'), 0, ny - 1) * nx + np.clip(((px[m] - x0) / r).astype('int64'), 0, nx - 1)
        cnt = np.bincount(flat, minlength=ny * nx)
        tot = np.bincount(flat, weights=pv[m].astype('float64'), minlength=ny * nx)
        if g in gcnt:
            gcnt[g] += cnt
            gtot[g] += tot
        else:
            gcnt[g], gtot[g] = cnt, tot
    for g, cnt in gcnt.items():
        tot = gtot[g]
        has = cnt > 0
        fm = np.zeros(ny * nx)
        fm[has] = tot[has] / cnt[has]
        nfile += has
        s1 += fm
        s2 += fm * fm
    xs = x0 + (np.arange(nx) + 0.5) * r
    ys = y0 + (np.arange(ny) + 0.5) * r
    nfile, s1, s2 = nfile.reshape(ny, nx), s1.reshape(ny, nx), s2.reshape(ny, nx)
    jb = job.get('bbox')
    if jb:   # 合成範圍外:讀檔時為了保留像素鄰接多讀了一圈,那圈只有部分軌道,不能當成合成結果
        out = (xs[None, :] < jb[0]) | (xs[None, :] > jb[2]) | (ys[:, None] < jb[1]) | (ys[:, None] > jb[3])
        nfile = np.where(out, 0, nfile)
    return xs, ys, nfile, s1, s2, r


def _mean(cnt, tot):
    with np.errstate(invalid='ignore', divide='ignore'):
        return tot / cnt


def composite_grid(job_id: str, bbox, mode: str, res, max_px: int = MAX_PX) -> dict:
    """依目前視野重新分格。合成:mean / count(覆蓋檔數)/ std(檔間標準差)。
    相減工作另有:diff(A − B)/ a / b / bcount。"""
    j = _job(job_id)
    xs, ys, cnt, tot, sq, r = _bins(j, bbox, res, max_px, 'a')
    mean = _mean(cnt, tot)
    with np.errstate(invalid='ignore', divide='ignore'):
        if mode in ('diff', 'b', 'bcount'):
            _, _, cb, tb, _, _ = _bins(j, bbox, res, max_px, 'b')
            mb = _mean(cb, tb)
            z = mean - mb if mode == 'diff' else mb if mode == 'b' else np.where(cb > 0, cb, np.nan)
        elif mode == 'count':
            z = np.where(cnt > 0, cnt, np.nan).astype('float64')
        elif mode == 'std':
            z = np.where(cnt > 1, np.sqrt(np.maximum(sq / cnt - mean * mean, 0)), np.nan)
        else:
            z = mean
    z = np.asarray(z, dtype='float64')
    return {'kind': 'heatmap', 'x': arr_to_list(xs, 5), 'y': arr_to_list(ys, 5), 'z': arr_to_list(z),
            '_x': xs, '_y': ys, '_z': z, 'stats': stats(z), 'step': [1, 1], 'native': [len(ys), len(xs)],
            'geo': True, 'res': r, 'files': j['used'], 'xlabel': 'longitude', 'ylabel': 'latitude'}


GROUP_WORD = {'file': '檔', 'day': '天', 'pixel': None}   # count / std 是「幾組」;像素等權只有一組,沒有意義


def composite_netcdf(job_id: str, bbox, res) -> bytes:
    """目前視野、目前格距的合成結果存成 NetCDF:平均、覆蓋數、標準差(相減另有 A、B),
    屬性記下所用的檔、qa 門檻、平均方式,拿到別處也知道它是怎麼來的。"""
    import tempfile
    j = _job(job_id)
    xs, ys, cnt, tot, sq, r = _bins(j, bbox, res, MAX_PX, 'a')
    word = GROUP_WORD[j.get('weight', 'file')]
    with np.errstate(invalid='ignore', divide='ignore'):
        mean = tot / cnt
        std = np.where(cnt > 1, np.sqrt(np.maximum(sq / cnt - mean * mean, 0)), np.nan)
    ds0 = dataset(j['files'][0][1], j['group'])
    attrs0 = ds0[j['var']].attrs
    units = clean_units(attrs0.get('units', ''))
    coords = {'latitude': ('latitude', ys, {'units': 'degrees_north'}), 'longitude': ('longitude', xs, {'units': 'degrees_east'})}
    dims = ('latitude', 'longitude')
    data = {}
    if j['diff']:
        _, _, cb, tb, _, _ = _bins(j, bbox, res, MAX_PX, 'b')
        with np.errstate(invalid='ignore', divide='ignore'):
            mb = tb / cb
        data['difference'] = (dims, (mean - mb).astype('float32'), {'units': units, 'long_name': 'A − B'})
        data['mean_a'] = (dims, mean.astype('float32'), {'units': units})
        data['mean_b'] = (dims, mb.astype('float32'), {'units': units})
        data['count_b'] = (dims, cb.astype('int32'))
    else:
        data['mean'] = (dims, mean.astype('float32'), {'units': units, 'long_name': str(attrs0.get('long_name', j['var']))})
    # 標籤用英文:Panoply、ncview 這類工具多半顯示不了中文
    en = {'檔': 'files', '天': 'days (UTC)'}.get(word or '', '')
    if word:
        data['count' if not j['diff'] else 'count_a'] = (dims, cnt.astype('int32'), {'long_name': f'number of {en} with valid data'})
        if not j['diff']:
            data['std'] = (dims, std.astype('float32'), {'units': units, 'long_name': f'standard deviation across {en}'})
    used = sorted(j['used_files'], key=lambda f: (f['set'], f.get('start') or f.get('date') or '', f['name']))
    names = [f"{f['set']}:{f['name']}" for f in used]
    starts = sorted(f.get('start') or f.get('date') for f in used if f.get('start') or f.get('date'))
    ends = sorted(f.get('end') or f.get('start') or f.get('date') for f in used if f.get('start') or f.get('date'))
    iso = lambda t: (t.replace(' ', 'T') + ':00Z') if len(t) > 10 else t
    attrs = {
        'Conventions': 'CF-1.8',
        'title': f"ncglobe composite of {j['group']}/{j['var']}",
        'source_variable': f"{j['group']}/{j['var']}", 'qa_min': 'none' if j['qa'] is None else float(j['qa']),
        'weighting': j['weight'], 'weighting_description': {
            'file': 'mean of per-file grid means (each file equal weight)',
            'day': 'files grouped by UTC date, mean of per-day grid means (each day equal weight)',
            'pixel': 'mean of all supersampled pixels'}[j['weight']],
        'grid_resolution_deg': float(r), 'files': '\n'.join(names),
        'notes': '\n'.join(j['notes']), 'created_by': 'ncglobe'}
    if starts:   # 觀測期間(依檔名的 UTC 時間);沒有時間維度,資料是這段期間的合成
        attrs['time_coverage_start'] = iso(starts[0])
        attrs['time_coverage_end'] = iso(ends[-1])
    out = xr.Dataset(data, coords=coords, attrs=attrs)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / 'composite.nc'
        # 座標不帶 _FillValue(CF 不允許座標有缺值;xarray 預設會給浮點座標 NaN 的 _FillValue)
        out.to_netcdf(f, engine='netcdf4', encoding={**{k: {'zlib': True, 'complevel': 4} for k in data},
                                                      'latitude': {'_FillValue': None}, 'longitude': {'_FillValue': None}})
        return f.read_bytes()


def composite_point(job_id: str, bbox, res, x: float, y: float) -> dict:
    j = _job(job_id)
    word = GROUP_WORD[j.get('weight', 'file')]
    xs, ys, cnt, tot, sq, r = _bins(j, bbox, res, MAX_PX, 'a')
    ix = int(np.clip(np.argmin(np.abs(xs - x)), 0, len(xs) - 1))
    iy = int(np.clip(np.argmin(np.abs(ys - y)), 0, len(ys) - 1))
    n = int(cnt[iy, ix])
    mean = tot[iy, ix] / n if n else None
    std = math.sqrt(max(sq[iy, ix] / n - mean * mean, 0)) if n else None
    where = {'lon': float(xs[ix]), 'lat': float(ys[iy]), '格距(度)': r}
    if j['diff']:
        _, _, cb, tb, _, _ = _bins(j, bbox, res, MAX_PX, 'b')
        nb = int(cb[iy, ix])
        mb = tb[iy, ix] / nb if nb else None
        diff = mean - mb if (mean is not None and mb is not None) else None
        return {'where': where, 'series': None, 'values': [
            {'name': 'A − B', 'value': jsonable(diff), 'units': '', 'current': True},
            {'name': 'A', 'value': jsonable(mean), 'units': '', 'current': False},
            {'name': 'B(平均)', 'value': jsonable(mb), 'units': '', 'current': False},
            *([{'name': f'A 覆蓋{word}數', 'value': n, 'units': '', 'current': False, 'count': True},
               {'name': f'B 覆蓋{word}數', 'value': nb, 'units': '', 'current': False, 'count': True}] if word else [])]}
    vals = [{'name': '平均', 'value': jsonable(mean), 'units': '', 'current': True}]
    if word:
        vals += [{'name': f'覆蓋{word}數', 'value': n, 'units': '', 'current': False, 'count': True},
                 {'name': f'{word}間標準差', 'value': jsonable(std) if n > 1 else None, 'units': '', 'current': False}]
    return {'where': where, 'series': None, 'values': vals}


def series_at(files: list[str], group: str, var: str, idx: dict, x: float, y: float, qa_min,
              radius_km: float = 0.0) -> list[dict]:
    """跨檔時序:每個檔在 (x, y) 的值。radius_km = 0 取最近的像素/格點(逐軌資料離軌道 15 km 以上算沒有),
    > 0 取半徑內所有有效像素的平均,並回傳像素數與標準差。時間取自檔名(觀測開始或日期)。"""
    out = []
    coslat = math.cos(math.radians(y))
    for f in files[:1000]:
        name = Path(f).name
        meta = file_meta(name)
        row = {'name': name, 'time': meta.get('start') or meta.get('date'), 'value': None, 'n': 0, 'std': None}
        try:
            path = str(resolve(f))
            ds = dataset(path, group)
            if var not in ds.variables:
                raise KeyError(f'沒有變數 {var}')
            da = ds[var]
            ydim, xdim, kind, lat, lon = plot_axes(ds, var)
            if not (lat and lon):
                raise ValueError('沒有經緯度')
            sel = _sel(da, idx, ydim, xdim)
            key = (path, group)
            z = full2d(ds, key, var, sel, ydim, xdim).astype('float64')
            if kind == 'curvilinear':
                la, lo = full2d(ds, key, lat, sel, ydim, xdim), full2d(ds, key, lon, sel, ydim, xdim)
            else:
                xc, yc, order = _regular_axes(ds, da, ydim, xdim, lat, lon)
                xo = xc if order is None else np.empty_like(xc)
                if order is not None:
                    xo[order] = xc
                lo, la = np.meshgrid(xo, yc)
            dkm = np.sqrt((la - y) ** 2 + (((lo - x + 180) % 360 - 180) * coslat) ** 2) * 111.2
            ok = np.isfinite(z)
            if qa_min is not None and kind == 'curvilinear' and 'qa_value' in ds.variables:
                ok &= full2d(ds, key, 'qa_value', sel, ydim, xdim) >= qa_min
            if radius_km > 0:
                m = ok & (dkm <= radius_km)
                if m.any():
                    v = z[m]
                    row.update(value=float(v.mean()), n=int(v.size), std=float(v.std()) if v.size > 1 else None)
            else:
                i = int(np.nanargmin(np.where(np.isfinite(dkm), dkm, np.inf)))
                limit = 15 if kind == 'curvilinear' else max(15, float(np.nanmedian(np.abs(np.diff(la[:, 0])))) * 111.2 if la.shape[0] > 1 else 15)
                if dkm.flat[i] <= limit and ok.flat[i]:
                    row.update(value=float(z.flat[i]), n=1)
        except Exception as e:
            row['error'] = f'{type(e).__name__}: {e}'
        out.append(row)
    return out


def value_at(file: str, group: str, var: str, idx: dict, x: float, y: float, qa_min) -> dict:
    """滑鼠讀值(地球儀):只回最近像素/格點的這一個值,比 point() 輕很多。"""
    path = str(resolve(file))
    ds = dataset(path, group)
    key = (path, group)
    da = ds[var]
    ydim, xdim, kind, lat, lon = plot_axes(ds, var)
    sel = _sel(da, idx, ydim, xdim)
    if kind == 'curvilinear':
        la, lo = full2d(ds, key, lat, sel, ydim, xdim), full2d(ds, key, lon, sel, ydim, xdim)
        dlon = (lo - x + 180) % 360 - 180
        d2 = (la - y) ** 2 + (dlon * math.cos(math.radians(y))) ** 2
        r, c = np.unravel_index(int(np.nanargmin(d2)), d2.shape)
        dist = math.sqrt(d2[r, c]) * 111.2
        v = full2d(ds, key, var, sel, ydim, xdim)[r, c]
        ok = dist < 15   # 離最近的像素太遠 = 不在軌道上
        if ok and qa_min is not None and 'qa_value' in ds.variables:
            ok = bool(full2d(ds, key, 'qa_value', sel, ydim, xdim)[r, c] >= qa_min)
        return {'lon': x, 'lat': y, 'value': jsonable(v) if ok else None}
    xc, yc, order = _regular_axes(ds, da, ydim, xdim, lat, lon)
    ix = int(np.nanargmin(np.abs(xc - x)))
    iy = int(np.nanargmin(np.abs(yc - y)))
    z = full2d(ds, key, var, sel, ydim, xdim)
    return {'lon': x, 'lat': y, 'value': jsonable(z[iy, int(order[ix]) if order is not None else ix])}


# ------------------------------------------------------------------ 海岸線 / 縣市界

@lru_cache(maxsize=4)
def _ne_geoms(res: str):
    from cartopy.io import shapereader
    return list(shapereader.Reader(shapereader.natural_earth(res, 'physical', 'coastline')).geometries())


# ---- GSHHG 海岸線(比 Natural Earth 細很多:full 約 40 m)
# 檔案放在 cartopy 的資料目錄 shapefiles/gshhs/<級>/GSHHS_<級>_L1.shp(`ncglobe --install-gshhg` 會下載);
# 沒裝就退回 Natural Earth。依畫面跨度選級:越放大越細。
GSHHG_URL = 'https://github.com/GenericMappingTools/gshhg-gmt/releases/download/2.3.7/gshhg-shp-2.3.7.zip'
GSHHG_SCALES = ((60, 'l', '5 km'), (15, 'i', '1 km'), (3, 'h', '200 m'), (0, 'f', '40 m'))   # (跨度下限°, 級, 約略精度)
_coast_lock = threading.Lock()


def _gshhg_file(scale: str) -> Path:
    """使用者下載的(cartopy 資料目錄)優先,其次是執行檔內建的;都沒有時回下載目的地。"""
    import cartopy
    rel = Path('shapefiles') / 'gshhs' / scale / f'GSHHS_{scale}_L1.shp'
    user = Path(cartopy.config['data_dir']) / rel
    if not user.exists() and BUNDLE and (BUNDLE / 'cartopy' / rel).exists():
        return BUNDLE / 'cartopy' / rel
    return user


def gshhg_installed() -> bool:
    """至少有最粗的一級。執行檔只內建 l/i/h(f 級 190 MB,要 --install-gshhg 下載)。"""
    return _gshhg_file('l').exists()


@lru_cache(maxsize=4)
def _gshhg_index(scale: str):
    """一級 GSHHG 海岸線切成每段最多 1000 點的線段,建 STRtree。

    大陸是一整個多邊形(full 級有上百萬個點),每次都拿整圈去裁會很慢;
    切段後查詢只拿到畫面附近的幾段。full 第一次載入約 10 秒、記憶體幾百 MB,之後重複用。
    """
    import shapely
    from cartopy.io import shapereader
    pieces = []
    for poly in shapereader.Reader(str(_gshhg_file(scale))).geometries():
        for ring in [poly.exterior, *poly.interiors] if poly.geom_type == 'Polygon' else \
                [r for p in poly.geoms for r in (p.exterior, *p.interiors)]:
            xy = shapely.get_coordinates(ring)
            for i in range(0, len(xy) - 1, 1000):
                pieces.append(shapely.linestrings(xy[i:i + 1001]))
    return pieces, shapely.STRtree(pieces)


def coast_scale(span: float) -> tuple[str, str]:
    """(來源名稱, 級):GSHHG 有裝就用 GSHHG,否則 Natural Earth。"""
    if gshhg_installed():
        best = 'l'
        for lo, sc, _ in GSHHG_SCALES:
            if _gshhg_file(sc).exists():
                best = sc          # 沒有更細的級時停在已有的最細一級
            if span > lo:
                break
        return 'gshhg', best
    return 'ne', '110m' if span > 90 else '50m' if span > 15 else '10m'


def coast_geoms(bbox: list[float]) -> tuple[list, str]:
    """畫面範圍內的海岸線(線段)與給人看的說明,例如 'GSHHG h(約 200 m)'。"""
    import shapely
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    src, sc = coast_scale(span)
    if src == 'ne':
        return _ne_geoms(sc), f'Natural Earth {sc}'
    with _coast_lock:   # full 級第一次載入要幾秒,別讓好幾個請求同時各載一份
        pieces, tree = _gshhg_index(sc)
    hits = tree.query(shapely.box(*bbox))
    label = dict((s_, a) for _, s_, a in GSHHG_SCALES)[sc]
    return [pieces[i] for i in hits], f'GSHHG {sc}(約 {label})'


def install_gshhg() -> None:
    """下載 GSHHG 2.3.7(約 150 MB),只解出五級的 L1 海岸線到 cartopy 資料目錄。"""
    import tempfile
    import urllib.request
    import zipfile
    with tempfile.TemporaryDirectory() as tmp:
        z = Path(tmp) / 'gshhg.zip'
        print(f'下載 {GSHHG_URL} …', flush=True)
        urllib.request.urlretrieve(GSHHG_URL, z)
        with zipfile.ZipFile(z) as zf:
            for _, sc, _ in GSHHG_SCALES:
                dest = _gshhg_file(sc).parent
                dest.mkdir(parents=True, exist_ok=True)
                for ext in ('shp', 'shx', 'dbf', 'prj'):
                    (dest / f'GSHHS_{sc}_L1.{ext}').write_bytes(zf.read(f'GSHHS_shp/{sc}/GSHHS_{sc}_L1.{ext}'))
            (_gshhg_file('f').parent.parent / 'LICENSE.TXT').write_bytes(zf.read('LICENSE.TXT'))
    print(f'完成 → {_gshhg_file("f").parent.parent}(GSHHG,LGPL v3)', flush=True)


def _lines(geoms, bbox, tol) -> tuple[list, list]:
    from shapely.geometry import box
    clip = box(*bbox)
    xs: list = []
    ys: list = []
    for g in geoms:
        if not g.intersects(clip):
            continue
        g = g.intersection(clip).simplify(tol, preserve_topology=False)
        for part in getattr(g, 'geoms', [g]):
            if part.geom_type != 'LineString' or part.is_empty:
                continue
            cx, cy = part.xy
            xs.extend(round(v, 4) for v in cx)
            ys.extend(round(v, 4) for v in cy)
            xs.append(None)
            ys.append(None)
    return xs, ys


LAYERS = frozenset({'coast', 'borders', 'grid'})
BORDER_SCALES = ((90, '110m'), (15, '50m'), (0, '10m'))   # (跨度下限°, Natural Earth 比例)


def _layers(q) -> frozenset:
    """請求要畫哪些圖層:ly=coast,borders,grid;舊網址的 coast=0 / 沒給 = 只有海岸線開或關。"""
    if 'ly' in q:
        return frozenset(q['ly'].split(',')) & LAYERS
    return frozenset() if q.get('coast') == '0' else frozenset({'coast'})


def _border_res(span: float) -> str:
    return next(r for lo, r in BORDER_SCALES if span > lo)


@lru_cache(maxsize=3)
def _border_geoms(res: str):
    """Natural Earth 陸地國界線(只有陸上邊界;海上沒有)。"""
    from cartopy.io import shapereader
    return list(shapereader.Reader(shapereader.natural_earth(res, 'cultural', 'admin_0_boundary_lines_land')).geometries())


def _nice_step(x: float) -> float:
    """經緯線間隔:取 0.1、0.2、0.5、1、2、5、10、15、30 裡最接近又不小於 x 的。"""
    for v in (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 15, 30):
        if v >= x:
            return v
    return 30


def outlines(bbox: list[float], layers: frozenset = frozenset({'coast'})) -> dict:
    lon0, lat0, lon1, lat1 = bbox
    span = max(lon1 - lon0, lat1 - lat0)
    tol = span / 3000
    out: dict = {}
    if 'coast' in layers:
        geoms, label = coast_geoms(bbox)
        cx, cy = _lines(geoms, bbox, tol)
        out.update(coast={'x': cx, 'y': cy}, res=label)
    if 'borders' in layers:
        bx, by = _lines(_border_geoms(_border_res(span)), bbox, tol)
        out['borders'] = {'x': bx, 'y': by}
    return out


# ------------------------------------------------------------------ HTTP

# ------------------------------------------------------------------ 使用者設定(存在伺服器端:App 的 WebKit 與瀏覽器共用)

CONFIG_DIR = Path(os.environ.get('NCGLOBE_CONFIG', Path.home() / '.config' / 'ncglobe'))
SETTINGS_FILE = CONFIG_DIR / 'settings.json'
SETTINGS_DEFAULTS = {
    'name': '', 'avatar_color': '#2f6fde', 'credit': False,             # 個人;credit = 匯出 PNG 角落標上 ncglobe
    'use_system': True,                                                  # 沒填名字時用這台電腦帳號的全名與照片
    'theme': 'system',                                                   # 外觀
    'cmap': 'Jet', 'coast': True, 'borders': False, 'grid': False,       # 地圖預設值:色表、圖層
    'qa': 0.5, 'speed': 700,
    'update_repo': 'Alex870521/ncglobe', 'auto_update': True,            # 更新(GitHub Releases;清空 = 不檢查、不連網)
}
_settings_lock = threading.Lock()


def _valid_setting(k: str, v):
    """只收認得的鍵、合理的值;其他丟掉(設定檔被手改壞也不會讓頁面出錯)。"""
    if k == 'name':
        return isinstance(v, str) and len(v.strip()) <= 40
    if k == 'avatar_color':
        return isinstance(v, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', v) is not None
    if k in ('credit', 'coast', 'borders', 'grid', 'use_system'):
        return isinstance(v, bool)
    if k == 'theme':
        return v in ('system', 'light', 'dark')
    if k == 'cmap':
        return v in CMAPS
    if k == 'qa':
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
    if k == 'speed':
        return v in (250, 700, 1500)
    if k == 'update_repo':
        return isinstance(v, str) and (v == '' or re.fullmatch(r'[\w.-]{1,39}/[\w.-]{1,100}', v) is not None)
    if k == 'auto_update':
        return isinstance(v, bool)
    return False


# ------------------------------------------------------------------ 檢查更新(GitHub Releases)

UPDATE_TTL = 6 * 3600
_update_cache: dict = {}


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r'\d+', v)[:3]) or (0,)


def update_check(force: bool = False) -> dict:
    """問設定的 GitHub 專案最新的 Release;比目前版本新就回傳版本與頁面網址。沒設定來源就不連網。"""
    import urllib.request
    from ncglobe import __version__
    st = load_settings()
    repo = st['update_repo']
    out = {'current': __version__, 'repo': repo, 'latest': None, 'newer': False, 'url': None, 'notes': '', 'error': None}
    if not repo:
        return out
    hit = _update_cache.get(repo)
    if hit and not force and time.time() - hit[0] < UPDATE_TTL:
        return {**out, **hit[1], 'newer': _version_tuple(hit[1]['latest'] or '0') > _version_tuple(__version__)}
    try:
        req = urllib.request.Request(f'https://api.github.com/repos/{repo}/releases/latest',
                                     headers={'Accept': 'application/vnd.github+json', 'User-Agent': f'ncglobe/{__version__}'})
        with urllib.request.urlopen(req, timeout=6) as r:
            rel = json.loads(r.read())
        info = {'latest': str(rel.get('tag_name', '')).lstrip('vV'), 'url': rel.get('html_url'),
                'notes': str(rel.get('body') or '')[:2000]}
    except Exception as e:   # 離線、專案不存在、沒有 Release:不影響使用,設定頁顯示原因
        return {**out, 'error': f'檢查更新失敗:{type(e).__name__}: {e}'[:200]}
    _update_cache[repo] = (time.time(), info)
    return {**out, **info, 'newer': _version_tuple(info['latest'] or '0') > _version_tuple(__version__)}


def load_settings() -> dict:
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        raw = {}
    out = dict(SETTINGS_DEFAULTS)
    out.update({k: v for k, v in (raw.items() if isinstance(raw, dict) else []) if k in SETTINGS_DEFAULTS and _valid_setting(k, v)})
    return out


def save_settings(changes: dict) -> dict:
    if not isinstance(changes, dict):
        raise ValueError('設定要是 JSON 物件')
    bad = [k for k, v in changes.items() if not _valid_setting(k, v)]
    if bad:
        raise ValueError(f'這些設定值不合理:{", ".join(bad)}')
    with _settings_lock:
        cur = load_settings()
        cur.update({k: (v.strip() if isinstance(v, str) and k == 'name' else v) for k, v in changes.items()})
        # 只存跟預設不同的:沒改過的項目跟著程式的預設走(預設值以後改了才會生效)
        diff = {k: v for k, v in cur.items() if v != SETTINGS_DEFAULTS.get(k)}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(diff, ensure_ascii=False, indent=1))
        tmp.replace(SETTINGS_FILE)
    return cur


_profile: dict = {}


def system_profile() -> dict:
    """這台電腦目前帳號的全名與大頭照(macOS 的 JPEGPhoto / Picture)。只在本機讀,不送到任何地方。"""
    if _profile:
        return _profile
    import pwd
    import subprocess
    try:
        full = pwd.getpwuid(os.getuid()).pw_gecos.split(',')[0].strip()
    except (KeyError, OSError):
        full = ''
    photo = None
    if sys.platform == 'darwin':
        user = os.environ.get('USER') or ''
        try:
            out = subprocess.run(['dscl', '.', '-read', f'/Users/{user}', 'JPEGPhoto'], capture_output=True, text=True, timeout=5).stdout
            hexs = ''.join(out.split()[1:]) if out.startswith('JPEGPhoto') else ''
            photo = bytes.fromhex(hexs) if hexs else None
        except (OSError, ValueError, subprocess.SubprocessError):
            photo = None
        if not photo:   # 沒有內嵌照片:帳號指定的圖檔(可能是 heic/tif),轉成 PNG
            try:
                out = subprocess.run(['dscl', '.', '-read', f'/Users/{user}', 'Picture'], capture_output=True, text=True, timeout=5).stdout
                pic = out.split(':', 1)[1].strip() if ':' in out else ''
                if pic and Path(pic).is_file():
                    dst = VENDOR_DIR / 'avatar.png'
                    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
                    subprocess.run(['sips', '-s', 'format', 'png', '-Z', '256', pic, '--out', str(dst)], capture_output=True, timeout=15)
                    photo = dst.read_bytes() if dst.exists() else None
            except (OSError, subprocess.SubprocessError):
                photo = None
    _profile.update(name=full, photo=photo,
                    photo_type='image/jpeg' if photo and photo[:2] == b'\xff\xd8' else 'image/png')
    return _profile


def clear_search_index() -> dict:
    """清掉檔名索引(記憶體與暫存檔):檔案搬來搬去後,搜尋結果對不上時用。"""
    with _index_lock:
        _indexes.clear()
    n = 0
    for f in INDEX_DIR.glob('*.json.gz') if INDEX_DIR.exists() else []:
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return {'removed': n}


def about() -> dict:
    from ncglobe import __version__
    size = sum(f.stat().st_size for f in INDEX_DIR.glob('*.json.gz')) if INDEX_DIR.exists() else 0
    return {'version': __version__, 'roots': [str(r) for r in ROOTS], 'app': IN_APP, 'settings_file': str(SETTINGS_FILE),
            'index_dir': str(INDEX_DIR), 'index_mb': round(size / 1e6, 1), 'cache_mb': CACHE_BYTES // 1024**2,
            'gshhg': gshhg_installed()}


def _bbox(raw: str) -> list[float]:
    """網址/請求裡的範圍:四個有限數字,排好大小、夾在經緯度範圍內(亂改網址、拖出地圖時不要 500)。"""
    try:
        b = [float(v) for v in json.loads(raw)]
    except (TypeError, ValueError):
        raise ValueError('範圍(bbox)格式不對,要四個數字') from None
    if len(b) != 4 or not all(math.isfinite(v) for v in b):
        raise ValueError('範圍(bbox)要四個有限數字')
    x0, x1 = sorted((max(-180.0, min(180.0, b[0])), max(-180.0, min(180.0, b[2]))))
    y0, y1 = sorted((max(-90.0, min(90.0, b[1])), max(-90.0, min(90.0, b[3]))))
    if x1 - x0 < 1e-6 or y1 - y0 < 1e-6:
        raise ValueError('範圍(bbox)太小或在地圖外')
    return [x0, y0, x1, y1]


def _vrange(q) -> tuple[float, float]:
    """色階上下限:有限數字、下限 < 上限(填反了就對調,相等就撐開一點)。"""
    try:
        lo, hi = float(q['vmin']), float(q['vmax'])
    except (KeyError, ValueError):
        raise ValueError('色階範圍要填數字') from None
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError('色階範圍要是有限的數字')
    lo, hi = min(lo, hi), max(lo, hi)
    if lo == hi:
        d = abs(lo) * 1e-6 or 1e-12
        lo, hi = lo - d, hi + d
    return lo, hi


def _qa(q) -> float | None:
    return _num(q, 'qa', 0.0, 1.0) if q.get('qa') else None


def _tile_xyz(q) -> tuple[int, int, int]:
    """地球儀圖磚 z/x/y:z 0–18(globe.gl 用到 9),x、y 在這一層的範圍內。"""
    z = _num(q, 'z', 0, 18, kind=int)
    n = 2 ** z
    return z, _num(q, 'x', 0, n - 1, kind=int), _num(q, 'y', 0, n - 1, kind=int)


def _num(q, key: str, lo: float, hi: float, default=None, kind=float):
    """請求裡的數字參數:要是有限數字、在合理範圍內(不然一個超大的 z、w、dpi 就能把電腦拖垮)。"""
    raw = q.get(key)
    if raw in (None, ''):
        if default is None:
            raise ValueError(f'缺少參數 {key}')
        return default
    try:
        v = kind(float(raw))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'參數 {key} 要是數字') from None
    if not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f'參數 {key} 要在 {lo}–{hi} 之間')
    return v


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 預設的存取紀錄太吵;自己印耗時(見 do_GET)
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            if 'Cache-Control' not in (extra or {}):
                self.send_header('Cache-Control', 'no-store')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # 前端已取消或逾時,結果丟掉就好

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')

    def _host_ok(self) -> bool:
        """只接受 Host 是本機名稱的請求。

        伺服器只聽 127.0.0.1,但惡意網站可以用 DNS rebinding 讓自己的網域解析到 127.0.0.1,
        瀏覽器就把它當同源,能讀檔、也能帶自訂標頭。這種請求的 Host 是對方的網域,在這裡擋掉。
        """
        host = (self.headers.get('Host') or '').strip().lower()
        name = host.rsplit(':', 1)[0] if not host.startswith('[') else host.split(']')[0] + ']'
        if name not in ('127.0.0.1', 'localhost', '[::1]'):
            self._json({'error': 'forbidden host'}, 403)
            return False
        # 跨站請求(CSRF):別的網站用 <img>、表單、fetch 打到 127.0.0.1 時,Host 是對的,
        # 但瀏覽器會標 Sec-Fetch-Site: cross-site / same-site,也會帶對方的 Origin。只收同源與直接開網址(none)。
        site = (self.headers.get('Sec-Fetch-Site') or '').lower()
        origin = (self.headers.get('Origin') or '').lower()
        if site in ('cross-site', 'same-site') or (origin and origin != f'http://{host}'):
            self._json({'error': 'cross-site request refused'}, 403)
            return False
        return True

    def do_POST(self):
        # 先把請求內容讀完再回應:不讀就回 4xx 時,對方還在送資料連線就被關掉(connection reset)
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        self._body = self.rfile.read(min(n, 64_000)) if n > 0 else b''
        if not self._host_ok():
            return
        # 結束伺服器(網頁上的按鈕)。要求自訂標頭:別的網站跨站送不出這個標頭,不能遠端把它關掉
        path = urlparse(self.path).path
        if path == '/api/quit' and self.headers.get('X-Ncglobe') == 'quit':
            self._json({'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        # 設定、清索引:同樣要自訂標頭(別的網站跨站送不出),body 是 JSON
        if self.headers.get('X-Ncglobe') == 'settings':
            try:
                body = json.loads(self._body or b'{}')
                if path == '/api/settings':
                    return self._json(save_settings(body))
                if path == '/api/index/clear':
                    return self._json(clear_search_index())
            except ValueError as e:
                return self._json({'error': str(e)}, 400)
        self._json({'error': 'not found'}, 404)

    # 會改變狀態或很吃資源的 GET:要 X-Ncglobe 標頭。跨站的 <img>/表單/fetch 帶不了自訂標頭
    # (要 CORS 預檢,這裡不允許),不靠瀏覽器有沒有送 Sec-Fetch-Site —— 舊瀏覽器沒有送時也擋得住。
    SIDE_EFFECTS = frozenset({'/api/composite/start', '/api/composite/cancel', '/api/update'})

    def do_GET(self):
        if not self._host_ok():
            return
        if urlparse(self.path).path in self.SIDE_EFFECTS and not self.headers.get('X-Ncglobe'):
            return self._json({'error': 'missing X-Ncglobe header'}, 403)
        t0 = time.monotonic()
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        code = 200
        try:
            if u.path == '/':
                return self._send(200, UI_FILE.read_bytes(), 'text/html; charset=utf-8')
            if u.path == '/favicon.ico' or u.path.startswith('/static/'):
                name = 'favicon.ico' if u.path == '/favicon.ico' else u.path[8:]
                if name in STATIC_FILES and (HERE / 'static' / name).exists():
                    return self._send(200, (HERE / 'static' / name).read_bytes(), STATIC_FILES[name],
                                      {'Cache-Control': 'max-age=86400'})
            if u.path.startswith('/vendor/') and u.path[8:] in VENDOR:
                return self._send(200, vendor(u.path[8:]), 'text/javascript; charset=utf-8',
                                  {'Cache-Control': 'max-age=86400'})
            if u.path == '/api/ls':
                return self._json(list_dir(q.get('path')))
            if u.path == '/api/series':
                return self._json(series_at(json.loads(q['files']), q.get('group', ''), q['var'],
                                            json.loads(q.get('idx') or '{}'), _num(q, 'x', -1e8, 1e8), _num(q, 'y', -1e8, 1e8), _qa(q),
                                            _num(q, 'radius', 0, 500, 0.0)))
            if u.path == '/api/passes':
                return self._json(passes(json.loads(q['files']), q.get('group', ''), q['var'], _bbox(q['bbox'])))
            if u.path == '/api/settings':
                return self._json(load_settings())
            if u.path == '/api/about':
                return self._json(about())
            if u.path == '/api/me':      # 電腦帳號的全名、有沒有照片(照片本身走 /api/me/photo)
                p = system_profile()
                return self._json({'name': p['name'], 'photo': bool(p['photo'])})
            if u.path == '/api/me/photo':
                p = system_profile()
                if not p['photo']:
                    raise FileNotFoundError(2, '這個帳號沒有大頭照', 'photo')
                return self._send(200, p['photo'], p['photo_type'], {'Cache-Control': 'max-age=3600'})
            if u.path == '/api/update':
                return self._json(update_check(q.get('force') == '1'))
            if u.path == '/api/search':
                return self._json(search_files(q['path'], q.get('q', '')))
            if u.path == '/api/tree':
                return self._json(list_tree(q['path']))
            if u.path == '/api/info':
                return self._json(file_info(q['file']))
            if u.path == '/api/var':
                return self._json(var_detail(q['file'], q.get('group', ''), q['var']))
            if u.path == '/api/slice':
                bbox = _bbox(q['bbox']) if q.get('bbox') else None
                return self._json(slice_data(q['file'], q.get('group', ''), q['var'],
                                             json.loads(q.get('idx') or '{}'), bbox, _qa(q),
                                             min(2.0, float(q.get('pad') or 0))))
            if u.path == '/api/render':
                return self._json(render(q['file'], q.get('group', ''), q['var'], json.loads(q.get('idx') or '{}'),
                                         _bbox(q['bbox']), _qa(q), *_vrange(q),
                                         q.get('cmap', 'Jet'), q.get('log') == '1', _num(q, 'w', 16, 6000, kind=int), _num(q, 'h', 16, 6000, kind=int)))
            if u.path in ('/api/figure', '/api/projrender', '/api/globetex'):
                texture = u.path == '/api/globetex'
                bbox = [-180, -90, 180, 90] if texture else (_bbox(q['bbox']) if q.get('bbox') else None)
                bare = u.path in ('/api/projrender', '/api/globetex')
                png, got = figure(q['file'], q.get('group', ''), q['var'], json.loads(q.get('idx') or '{}'), bbox, _qa(q),
                             *_vrange(q), q.get('cmap', 'Jet'), q.get('log') == '1',
                             'plate' if texture else q.get('proj', 'plate'), _num(q, 'w', 16, 6000, kind=int), _num(q, 'h', 16, 6000, kind=int), _num(q, 'dpi', 30, 600, 100, int),
                             q.get('title', ''), q.get('subtitle', ''),
                             {'id': q['job'], 'mode': q.get('mode', 'mean'),
                              'res': _num(q, 'res', 0.001, 10.0) if q.get('res') else None} if q.get('job') else None,
                             _num(q, 'clon', -360, 360, 0.0), _num(q, 'clat', -90, 90, 0.0),
                             None if texture else (json.loads(q['extent']) if q.get('extent') else None), bare, texture,
                             (_num(q, 'ua', -1e30, 1e30), _num(q, 'ub', -1e30, 1e30, 0.0), q.get('ul', '')) if q.get('ua') else None,
                             _layers(q), q.get('credit') == '1')
                if texture:
                    return self._send(200, png, 'image/png')
                if bare:
                    return self._json({'png': base64.b64encode(png).decode(), 'extent': got})
                extra = {}
                if q.get('download'):
                    extra['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(q['download'])}"
                return self._send(200, png, 'image/png', extra)
            if u.path == '/api/tile':
                job = ({'id': q['job'], 'mode': q.get('mode', 'mean'), 'res': _num(q, 'res', 0.001, 10.0) if q.get('res') else None}
                       if q.get('job') else None)
                png = tile(u.query, q['file'], q.get('group', ''), q['var'], json.loads(q.get('idx') or '{}'), _qa(q),
                           *_vrange(q), q.get('cmap', 'Jet'), q.get('log') == '1',
                           *_tile_xyz(q), job, _layers(q))
                return self._send(200, png, 'image/png', {'Cache-Control': 'max-age=3600'})
            if u.path == '/api/point':
                return self._json(point(q['file'], q.get('group', ''), q['var'], json.loads(q.get('idx') or '{}'),
                                        _num(q, 'x', -1e8, 1e8), _num(q, 'y', -1e8, 1e8), _qa(q)))
            if u.path == '/api/composite/start':
                bbox = _bbox(q['bbox']) if q.get('bbox') else None
                return self._json(composite_start(json.loads(q['files']), q.get('group', ''), q['var'],
                                                  json.loads(q.get('idx') or '{}'), _qa(q), bbox,
                                                  json.loads(q['bfiles']) if q.get('bfiles') else None,
                                                  q.get('weight', 'file')))
            if u.path == '/api/composite/status':
                return self._json(composite_status(q['id']))
            if u.path == '/api/composite/cancel':
                return self._json(composite_cancel(q['id']))
            if u.path == '/api/composite/slice':
                bbox = _bbox(q['bbox']) if q.get('bbox') else None
                g = composite_grid(q['id'], bbox, q.get('mode', 'mean'), _num(q, 'res', 0.001, 10.0) if q.get('res') else None)
                return self._json({k: v for k, v in g.items() if not k.startswith('_')})
            if u.path == '/api/composite/export':
                bbox = _bbox(q['bbox']) if q.get('bbox') else None
                body = composite_netcdf(q['id'], bbox, _num(q, 'res', 0.001, 10.0) if q.get('res') else None)
                # 檔名來自網址參數,只留安全字元:換行、引號進到標頭會被拿來插入別的標頭
                name = __import__('re').sub(r'[^A-Za-z0-9_.-]', '_', q.get('name') or 'composite')[:80] or 'composite'
                return self._send(200, body, 'application/x-netcdf',
                                  {'Content-Disposition': f'attachment; filename="{name}.nc"'})
            if u.path == '/api/composite/point':
                bbox = _bbox(q['bbox']) if q.get('bbox') else None
                return self._json(composite_point(q['id'], bbox, _num(q, 'res', 0.001, 10.0) if q.get('res') else None,
                                                  _num(q, 'x', -1e8, 1e8), _num(q, 'y', -1e8, 1e8)))
            if u.path == '/api/value':
                x, y = _num(q, 'x', -1e8, 1e8), _num(q, 'y', -1e8, 1e8)
                if q.get('job'):
                    # 只在滑鼠周圍一小塊分格:整個合成範圍每移一下就重算太慢
                    r = composite_point(q['job'], [x - 0.3, y - 0.3, x + 0.3, y + 0.3],
                                        _num(q, 'res', 0.001, 10.0, 0.05), x, y)
                    return self._json({'lon': x, 'lat': y, 'value': r['values'][0]['value']})
                return self._json(value_at(q['file'], q.get('group', ''), q['var'], json.loads(q.get('idx') or '{}'),
                                           x, y, _qa(q)))
            if u.path == '/api/unproject':
                return self._json(unproject(q.get('proj', 'plate'), _num(q, 'clon', -360, 360, 0.0), _num(q, 'clat', -90, 90, 0.0),
                                            _num(q, 'x', -1e8, 1e8), _num(q, 'y', -1e8, 1e8)))
            if u.path == '/api/colorscale':
                return self._json(colorscale(q.get('cmap', 'Jet')))
            if u.path == '/api/outlines':
                return self._json(outlines(_bbox(q['bbox']), _layers(q)))
            code = 404
            self._json({'error': 'not found'}, 404)
        except Forbidden as e:
            code = 403
            self._json({'error': f'「{Path(str(e)).name}」不在目前允許讀取的資料夾裡({Path(str(e)).parent})。'
                                 f'App 版:用選單「檔案 › 開啟…」(⌘O)選它所在的資料夾;指令版:啟動時把那個資料夾一起列出來。'}, 403)
        except TooBig as e:
            code = 413
            self._json({'error': str(e)}, 413)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as e:
            code = 404
            self._json({'error': f'找不到這個檔,或它不是資料檔:{Path(getattr(e, "filename", "") or q.get("file", "")).name}'}, 404)
        except KeyError as e:          # 合成過期、檔案裡沒有這個變數 / 群組
            code = 404
            self._json({'error': str(e.args[0]) if e.args else '找不到'}, 404)
        except ValueError as e:        # 參數不合理(範圍、色階、qa…):講清楚哪裡錯
            code = 400
            self._json({'error': str(e)}, 400)
        except Exception as e:  # 檢視器:把錯誤丟回頁面,不讓伺服器掛掉
            if stale_handle(e) and not getattr(self, '_retried', False):
                self._retried = True
                return self.do_GET()
            code = 500
            self._json({'error': f'{type(e).__name__}: {e}'}, 500)
        finally:
            dt = time.monotonic() - t0
            if u.path.startswith('/api/') and u.path not in ('/api/colorscale', '/api/outlines') and (u.path not in ('/api/tile', '/api/value') or dt > 1 or code >= 400):
                name = Path(q.get('file', '')).name
                print(f'{time.strftime("%H:%M:%S")} {u.path:<15} {dt:6.2f}s  {code}  {name} {q.get("var", "")}',
                      file=sys.stderr, flush=True)


class Server(ThreadingHTTPServer):
    # 預設只能排 5 個等候中的連線:瀏覽器一次抓一批地球儀圖磚、或好幾個人同時用時,
    # 多出來的會被 reset(Connection reset by peer)
    request_queue_size = 128
    daemon_threads = True


def main():
    global CACHE_BYTES
    if BUNDLE:   # cartopy 先找內建的 Natural Earth,沒有才下載到使用者的資料目錄
        import cartopy
        cartopy.config['pre_existing_data_dir'] = str(BUNDLE / 'cartopy')
    ap = argparse.ArgumentParser(description='Browser NetCDF viewer (Panoply-like).')
    ap.add_argument('paths', nargs='*', help='folders the viewer may read, or a file to open (default: current folder)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--cache-mb', type=int, default=3000, help='memory for decoded arrays (default 3000)')
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--install-gshhg', action='store_true',
                    help='download the GSHHG shoreline (~150 MB) for finer coastlines, then exit')
    args = ap.parse_args()
    if args.install_gshhg:
        install_gshhg()
        return
    CACHE_BYTES = args.cache_mb * 1024**2
    open_file = None
    paths = args.paths or [os.getcwd()]
    if IN_APP:   # App 版只讀使用者在「開啟」對話框選的地方(之後由 macapp 加入);不預設家目錄,
        paths = list(args.paths)   # 否則一掃描就碰到桌面、文件、外接碟,macOS 每個都要問一次權限
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_file():                      # 給檔案:它的資料夾當 root,啟動後直接打開
            open_file = open_file or p
            p = p.parent
        if not p.is_dir():
            raise SystemExit(f'not a folder or file: {raw}')
        if p not in ROOTS:
            ROOTS.append(p)
    url = f'http://127.0.0.1:{args.port}/'
    if open_file:
        url += '#' + 'f=' + quote(str(open_file))
    try:
        srv = Server(('127.0.0.1', args.port), Handler)
    except OSError:
        # 已經有一個在跑:直接用它開(那一個要能讀到這個檔,否則頁面會說不在允許的資料夾內)
        print(f'port {args.port} 已有 ncglobe 在跑,直接開 {url}')
        if IN_APP:   # App 照樣開自己的視窗,連到已在跑的那一個
            from ncglobe import macapp
            return macapp.run(None, f'http://127.0.0.1:{args.port}/', lambda d: None, open_file)
        if not args.no_browser:
            webbrowser.open(url)
        return
    srv.daemon_threads = True
    # HDF5 每個變數的解壓快取預設只有 4 MB;壓縮塊比它大時,每次讀取都得重新解壓整塊
    netCDF4.set_chunk_cache(size=256 * 1024**2, nelems=1009, preemption=0.75)
    if gshhg_installed():   # 常用的三級先在背景載好(h 約 3 秒);最細的 f 等真的放大到那裡再載
        def warm():
            for sc in ('l', 'i', 'h'):
                with _coast_lock:
                    _gshhg_index(sc)
        threading.Thread(target=warm, daemon=True).start()
    else:
        print('提示:海岸線用 Natural Earth;要更細的 GSHHG(約 40 m)請執行 ncglobe --install-gshhg', file=sys.stderr)
    print(f'ncglobe → {url}  (roots: {", ".join(map(str, ROOTS))}, cache {args.cache_mb} MB)  Ctrl+C 結束')
    if IN_APP:   # App 自己的視窗(WKWebView)+ Finder「打開方式」;伺服器改在背景執行緒
        from ncglobe import macapp

        def add_root(d: Path) -> None:
            if not any(d == r or d.is_relative_to(r) for r in ROOTS):
                ROOTS.append(d)
        return macapp.run(srv, f'http://127.0.0.1:{args.port}/', add_root, open_file)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
