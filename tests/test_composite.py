"""Composites: map cell == click == hover, weighting labels, NetCDF export."""
import io
import time

import numpy as np
import xarray as xr

from ncglobe import server as s

VAR = 'nitrogendioxide_tropospheric_column'


def _run(files, **kw):
    st = s.composite_start(files, 'PRODUCT', VAR, {}, 0.5, None, **kw)
    for _ in range(300):
        if s.composite_status(st['id'])['status'] != 'running':
            break
        time.sleep(0.05)
    return st['id']


def test_cell_click_and_hover_agree(data):
    job = _run(data['swaths'])
    view, res = [118.0, 19.0, 126.0, 27.0], 0.05
    g = s.composite_grid(job, view, 'mean', res)
    z, xs, ys = g['_z'], g['_x'], g['_y']
    cells = np.argwhere(np.isfinite(z))
    assert len(cells) > 100
    for iy, ix in cells[np.random.default_rng(1).choice(len(cells), 8, replace=False)]:
        x, y = xs[ix], ys[iy]
        click = s.composite_point(job, [x - 1, y - 1, x + 1, y + 1], res, x, y)['values'][0]['value']
        hover = s.composite_point(job, [x - .3, y - .3, x + .3, y + .3], res, x, y)['values'][0]['value']
        assert np.isclose(click, z[iy, ix]) and np.isclose(hover, z[iy, ix])


def test_per_day_labels_and_pixel_weighting_has_no_count(data):
    day = _run(data['swaths'], weight='day')
    names = [v['name'] for v in s.composite_point(day, [118, 19, 126, 27], 0.1, 120.0, 21.0)['values']]
    assert names == ['平均', '覆蓋天數', '天間標準差']
    pix = _run(data['swaths'], weight='pixel')
    assert [v['name'] for v in s.composite_point(pix, [118, 19, 126, 27], 0.1, 120.0, 21.0)['values']] == ['平均']


def test_netcdf_export_is_cf_and_matches_grid(data, tmp_path):
    job = _run(data['swaths'])
    view, res = [118.0, 19.0, 126.0, 27.0], 0.1
    nc = s.composite_netcdf(job, view, res)
    (tmp_path / 'c.nc').write_bytes(nc)
    ds = xr.open_dataset(tmp_path / 'c.nc')
    assert ds.attrs['Conventions'] == 'CF-1.8' and ds.attrs['time_coverage_start'].startswith('2026-01-02T04')
    g = s.composite_grid(job, view, 'mean', res)
    assert np.allclose(ds['mean'].values, g['_z'], equal_nan=True, rtol=1e-6)


def test_auto_grid_is_not_much_finer_than_a_pixel(data):
    # 「自動」格距至少半個像素寬:比像素細很多只會把回應撐到幾 MB(台灣曾算出 0.0056°、8.9 MB)
    job = _run(data['swaths'])
    pix = float(np.median(s._job(job)['pix']['a']))
    g = s.composite_grid(job, [119.0, 21.0, 123.0, 26.0], 'mean', None)
    assert g['res'] >= 0.5 * pix - 1e-9
