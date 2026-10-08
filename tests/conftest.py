"""Synthetic data files and a running server for ncglobe tests (no external drive needed)."""
import threading
from pathlib import Path

import netCDF4
import numpy as np
import pytest

from ncglobe import server as s

S5P_NAME = 'S5P_OFFL_L2__NO2____20260102T040222_20260102T054352_42602_03_020901_20260103T203051.nc'


def make_swath(path: Path, lon0=118.0, seed=0, ny=80, nx=40):
    """S5P-like L2 swath: PRODUCT group, (time, scanline, ground_pixel), qa_value, mol m-2."""
    rng = np.random.default_rng(seed)
    j, i = np.meshgrid(np.arange(nx), np.arange(ny))
    lon = lon0 + 0.12 * j + 0.03 * i            # tilted swath
    lat = 19.0 + 0.1 * i - 0.01 * j
    val = 1e-5 * (1 + np.sin(np.radians(lon) * 20) + rng.random((ny, nx)) * 0.1)
    qa = np.where(rng.random((ny, nx)) < 0.1, 0.3, 0.9)
    with netCDF4.Dataset(path, 'w') as d:
        g = d.createGroup('PRODUCT')
        g.createDimension('time', 1); g.createDimension('scanline', ny); g.createDimension('ground_pixel', nx)
        for name, a, units in (('latitude', lat, 'degrees_north'), ('longitude', lon, 'degrees_east'),
                               ('qa_value', qa, '1'), ('nitrogendioxide_tropospheric_column', val, 'mol m-2')):
            v = g.createVariable(name, 'f4', ('time', 'scanline', 'ground_pixel'), fill_value=9.96921e36)
            v[0] = a
            v.units = units
    return path


def make_grid(path: Path, nt=24):
    """ERA5-like regular grid with a time axis, units K."""
    lat = np.arange(26.0, 20.9, -0.25)
    lon = np.arange(118.0, 123.01, 0.25)
    with netCDF4.Dataset(path, 'w') as d:
        d.createDimension('valid_time', nt); d.createDimension('latitude', len(lat)); d.createDimension('longitude', len(lon))
        t = d.createVariable('valid_time', 'i8', ('valid_time',)); t[:] = np.arange(nt) * 3600; t.units = 'seconds since 2025-01-01'
        d.createVariable('latitude', 'f4', ('latitude',))[:] = lat
        d.createVariable('longitude', 'f4', ('longitude',))[:] = lon
        v = d.createVariable('t2m', 'f4', ('valid_time', 'latitude', 'longitude')); v.units = 'K'
        v[:] = 290 + np.arange(nt)[:, None, None] * 0.1 + lat[None, :, None] * 0 + lon[None, None, :] * 0.01
    return path


@pytest.fixture()
def data(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    (root / 'l2').mkdir(parents=True)
    swaths = [make_swath(root / 'l2' / S5P_NAME.replace('42602', f'4260{k}').replace('T040222', f'T04{k}222'), seed=k)
              for k in range(3)]
    grid = make_grid(root / 'era5_sfc_t2m_20250101_20250101.nc')
    page = root / 'l2' / 'MOD04_L2.A2022306.0200.061.2022306142547.hdf'
    page.write_text('<!DOCTYPE html><title>Earthdata Login</title>' + ' ' * 200)
    monkeypatch.setattr(s, 'ROOTS', [root.resolve()])
    s._open.clear(); s._roots.clear(); s.ARRAYS.__init__()
    return {'root': root.resolve(), 'swaths': [str(p.resolve()) for p in swaths], 'grid': str(grid.resolve()),
            'page': str(page.resolve())}


@pytest.fixture()
def http(data):
    srv = s.Server(('127.0.0.1', 0), s.Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f'http://127.0.0.1:{srv.server_address[1]}'
    srv.shutdown()
