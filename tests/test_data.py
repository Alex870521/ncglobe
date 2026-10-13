"""Reading, drawing inputs and point queries on synthetic swath / grid files."""
import numpy as np
import pytest

from ncglobe import server as s

VAR = 'nitrogendioxide_tropospheric_column'


def test_swath_slice_is_geo_and_respects_qa(data):
    f = data['swaths'][0]
    a = s.slice_data(f, 'PRODUCT', VAR, {}, None, None)
    b = s.slice_data(f, 'PRODUCT', VAR, {}, None, 0.5)
    assert a['kind'] == 'swath' and a['geo']
    assert b['stats']['count'] < a['stats']['count']            # ~10 % of pixels have qa 0.3


def test_point_flags_qa_rejected_and_off_swath(data):
    f = data['swaths'][0]
    ds = s.dataset(f, 'PRODUCT')
    qa = ds['qa_value'].values[0]
    lat, lon = ds['latitude'].values[0], ds['longitude'].values[0]
    r, c = np.argwhere(qa < 0.5)[0]
    bad = s.point(f, 'PRODUCT', VAR, {}, float(lon[r, c]), float(lat[r, c]), 0.5)
    assert 'qa_value' in bad['note']
    far = s.point(f, 'PRODUCT', VAR, {}, 0.0, -60.0, 0.5)
    assert '不在軌道上' in far['note']


def test_grid_point_has_series_with_units_and_marker(data):
    p = s.point(data['grid'], '', 't2m', {'valid_time': 5}, 120.0, 23.0)
    assert p['series']['units'] == 'K'
    assert len(p['series']['y']) == 24 and p['series']['current'] == p['series']['x'][5]


def test_html_page_named_hdf_gives_a_clear_error(data):
    with pytest.raises(OSError, match='登入頁'):
        s.file_info(data['page'])


def test_passes_marks_only_files_over_the_box(data):
    got = s.passes(data['swaths'], 'PRODUCT', VAR, [118.5, 20.0, 120.0, 22.0])
    assert all(v is True for v in got.values())
    got = s.passes(data['swaths'], 'PRODUCT', VAR, [0.0, 40.0, 5.0, 45.0])
    assert all(v is False for v in got.values())


def test_paths_outside_roots_are_refused(data, tmp_path):
    with pytest.raises(s.Forbidden):
        s.resolve('/etc/hosts')


def test_series_across_files_point_and_radius(data):
    files = data['swaths'] + [data['page']]
    pt = s.series_at(files, 'PRODUCT', VAR, {}, 120.0, 21.0, None)
    assert [r['n'] for r in pt[:3]] == [1, 1, 1] and all(r['time'].startswith('2026-01-02') for r in pt[:3])
    assert 'error' in pt[3]                                       # the saved web page
    area = s.series_at(data['swaths'], 'PRODUCT', VAR, {}, 120.0, 21.0, 0.5, radius_km=25)
    assert all(r['n'] > 1 and r['std'] is not None for r in area)
    off = s.series_at(data['swaths'], 'PRODUCT', VAR, {}, 0.0, -60.0, None)
    assert all(r['value'] is None for r in off)


def test_series_on_a_regular_grid(data):
    r = s.series_at([data['grid']], '', 't2m', {'valid_time': 3}, 120.0, 23.0, None)[0]
    assert r['n'] == 1 and abs(r['value'] - (290 + 0.3 + 120.0 * 0.01)) < 1e-3


def test_search_finds_files_in_subfolders_and_caches_the_index(tmp_path, monkeypatch):
    import time
    from ncglobe import server as srv
    monkeypatch.setattr(srv, 'INDEX_DIR', tmp_path / 'index')
    monkeypatch.setattr(srv, '_indexes', {})
    root = tmp_path / 'NO2___' / '2026'
    for month, day in (('03', '04'), ('03', '05'), ('04', '01')):
        d = root / month
        d.mkdir(parents=True, exist_ok=True)
        (d / f'S5P_OFFL_L2__NO2____2026{month}{day}T051213_2026{month}{day}T065342_43467_03_020901_2026{month}{day}T213009.nc').write_bytes(b'x')
    (root / 'notes.txt').write_text('not data')
    monkeypatch.setattr(srv, 'ROOTS', [tmp_path.resolve()])

    def wait(q, path=root):
        for _ in range(100):
            r = srv.search_files(str(path), q)
            if r['status'] == 'ready':
                return r
            time.sleep(0.02)
        raise AssertionError('index never finished')

    r = wait('2026-03-04')                       # 觀測時間(從檔名解析)也搜得到
    assert r['total'] == 1 and r['results'][0]['rel'] == '03'
    assert wait('20260305 43467')['total'] == 1   # 多個關鍵字 = 每個都要有
    assert wait('03')['total'] >= 2               # 子資料夾名稱也算
    assert wait('2026', root / '04')['total'] == 1  # 在子資料夾裡搜:沿用上層索引,只看自己底下
    cached = list((tmp_path / 'index').glob('*.json.gz'))
    assert len(cached) == 1                      # 掃完存成暫存檔
    monkeypatch.setattr(srv, '_indexes', {})     # 模擬重開:直接讀暫存檔,不用等掃描
    r = srv.search_files(str(root), '2026-04-01')
    assert r['total'] == 1 and r['age'] is not None


def test_padded_slice_reaches_past_the_view_but_stats_stay_on_the_view(data):
    # 平面地圖多抓畫面外一圈:資料涵蓋更大,自動色階的統計值卻只算畫面內
    view = [120.0, 22.0, 121.0, 23.0]
    plain = s.slice_data(data['grid'], '', 't2m', {'valid_time': 0}, view, None)
    padded = s.slice_data(data['grid'], '', 't2m', {'valid_time': 0}, view, None, pad=0.5)
    assert min(padded['x']) < min(plain['x']) and max(padded['x']) > max(plain['x'])
    assert min(padded['y']) < min(plain['y']) and max(padded['y']) > max(plain['y'])
    assert padded['stats'] == plain['stats']
    sw = s.slice_data(data['swaths'][0], 'PRODUCT', 'nitrogendioxide_tropospheric_column', {}, view, None)
    swp = s.slice_data(data['swaths'][0], 'PRODUCT', 'nitrogendioxide_tropospheric_column', {}, view, None, pad=0.5)
    assert len(swp['x']) >= len(sw['x']) and swp['stats'] == sw['stats']


def test_folder_list_merges_year_month_and_single_child_chains(tmp_path, monkeypatch):
    root = tmp_path / 'sat'
    for prod, ym in (('NO2', [('2026', '01'), ('2026', '02')]), ('HCHO', [('2025', '12'), ('2026', '01')])):
        for y, m in ym:
            d = root / 'L2' / prod / y / m
            d.mkdir(parents=True)
            (d / f'S5P_OFFL_L2__{prod}_{y}{m}01T040000_{y}{m}01T050000_00001_03_020901_{y}{m}02T000000.nc').write_bytes(b'x')
    day = root / 'global' / 'NO2' / '2026' / '01' / '02'
    day.mkdir(parents=True)
    (day / 'a.nc').write_bytes(b'x')
    monkeypatch.setattr(s, 'ROOTS', [root.resolve()])
    r = root.resolve()
    names = lambda p: [d['name'] for d in s.list_dir(str(p))['dirs']]
    assert names(r / 'L2') == ['HCHO', 'NO2']                     # 產品名稱不被吞掉
    assert names(r / 'L2' / 'NO2') == ['2026-01', '2026-02']      # 年/月合成一層
    assert names(r / 'L2' / 'HCHO') == ['2025-12', '2026-01']
    assert names(r) == ['global/NO2', 'L2']                       # 一路只有一個子資料夾:合成一項
    assert names(r / 'global' / 'NO2') == ['2026-01-02']          # 只有一個月、一天:年月日一項
    assert s.list_dir(str(r / 'L2' / 'NO2' / '2026' / '02'))['parent'] == str(r / 'L2' / 'NO2')   # .. 跳過年
    assert s.list_dir(str(day))['parent'] == str(r / 'global' / 'NO2')
    assert s.list_dir(str(r / 'global' / 'NO2'))['parent'] == str(r)


def test_tiles_outside_the_data_are_shared_and_kept_on_disk(data, tmp_path, monkeypatch):
    import math
    monkeypatch.setattr(s, 'BLANK_TILE_DIR', tmp_path / 'tiles')
    monkeypatch.setattr(s, '_tiles', s.OrderedDict())
    f, var = data['swaths'][0], VAR
    _, lo, la = s.footprint(f, 'PRODUCT', var)
    n, cx, cy = 8, float(lo.mean()), float(la.mean())
    x = int((cx + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(cy))) / math.pi) / 2 * n)
    far = ((x + n // 2) % n, y)

    def tile(xy, cmap='Jet'):
        return s.tile(f'{xy}{cmap}', f, 'PRODUCT', var, {}, 0.5, 0.0, 1e-4, cmap, False, 3, *xy, None)

    with_data, blank = tile((x, y)), tile(far)
    assert with_data != blank
    assert not list((tmp_path / 'tiles').rglob(f'3/{x}_{y}.png'))           # 有資料的不存
    assert list((tmp_path / 'tiles').rglob(f'3/{far[0]}_{far[1]}.png'))     # 空白的存起來
    assert tile(far, 'Viridis') == blank                                    # 換色表、換檔都共用
    monkeypatch.setattr(s, '_tiles', s.OrderedDict())                    # 重新啟動:從硬碟讀
    assert tile(far, 'Turbo') == blank


def test_prewarm_draws_the_wide_levels_once(tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'BLANK_TILE_DIR', tmp_path / 'tiles')
    monkeypatch.setattr(s, '_tiles', s.OrderedDict())
    assert s.prewarm_blank_tiles(frozenset(), max_z=1) == 5          # z0 一張 + z1 四張
    assert len(list((tmp_path / 'tiles').rglob('*.png'))) == 5
    assert s.prewarm_blank_tiles(frozenset(), max_z=1) == 0          # 已經有了:不重畫


@pytest.mark.skipif(not s.gshhg_installed(), reason='GSHHG not installed')
def test_coastline_pieces_are_cached_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'COAST_CACHE_DIR', tmp_path / 'coast')
    coords, offs = s._coast_arrays('l')
    assert s._coast_cache_file('l').exists() and offs[-1] == len(coords)
    c2, o2 = s._coast_arrays('l')                                     # 第二次從快取讀
    assert np.array_equal(coords, c2) and np.array_equal(offs, o2)
