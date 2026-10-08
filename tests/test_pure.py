"""Pure helpers: number formatting, units, colour maps, file-name metadata, orbit dedupe."""
import numpy as np

from ncglobe import server as s


def test_values_keep_significant_figures_not_decimals():
    # 1e-5 mol m-2 columns used to round to one digit (np.round(a, 6))
    out = s.arr_to_list([1.23456789e-5, -3.3e-6, 0.0, np.nan, np.inf, 12345.678])
    assert out == [1.2346e-05, -3.3e-06, 0.0, None, None, 12346.0]
    assert s.arr_to_list([121.123456], 4) == [121.1235]          # coordinates: decimals


def test_unitless_spellings_are_dropped():
    for u in ('None', 'none', '1', '-', 'dimensionless', None):
        assert s.clean_units(u) == ''
    assert s.clean_units('mol m-2') == 'mol m-2'


def test_reversed_colour_maps():
    assert s._cmap('Viridis_r').name == 'viridis_r'
    assert s._cmap('RdBu_r').name == 'RdBu'                       # RdBu is stored reversed already


def test_file_meta_for_each_naming_scheme():
    m = s.file_meta('S5P_OFFL_L2__NO2____20260102T040222_20260102T054352_42602_03_020901_20260103T203051.nc')
    assert m == {'start': '2026-01-02 04:02', 'end': '2026-01-02 05:43', 'orbit': 42602, 'date': '2026-01-02',
                 'proc': 'OFFL', 'ver': '020901'}
    assert s.file_meta('GK2_GEMS_L2_20260614_0345_NO2_FW-ETC_DPRO_ORI.nc') == \
        {'start': '2026-06-14 03:45', 'date': '2026-06-14', 'mode': 'FW-ETC'}
    assert s.file_meta('MYD04_L2.A2025073.0650.061.2025073230704.hdf') == {'start': '2025-03-14 06:50', 'date': '2025-03-14'}
    assert s.file_meta('MCD19A2.A2025244.h28v06.061.2025245220514.hdf') == {'date': '2025-09-01', 'tile': 'h28v06'}
    assert s.file_meta('era5_sfc_d2m_t2m_20220101_20221231.nc') == {'date': '2022-01-01'}


def _s5p(proc, start, orbit, made):
    return f'/x/S5P_{proc}_L2__O3_____{start}_20260901T043616_{orbit}_03_020800_{made}.nc'


def test_nrti_granules_of_one_orbit_are_all_kept():
    nrti = [_s5p('NRTI', f'20260901T04{m}16', '46035', '20260901T050949') for m in ('31', '36', '41')]
    kept, notes = s._dedupe_orbits(nrti)
    assert sorted(kept) == sorted(nrti) and notes == []


def test_offl_replaces_nrti_and_newest_processing_wins():
    nrti = [_s5p('NRTI', '20260901T043116', '46035', '20260901T050949')]
    offl_old = _s5p('OFFL', '20260901T030000', '46035', '20260903T120000')
    offl_new = _s5p('OFFL', '20260901T030000', '46035', '20260910T120000')
    kept, notes = s._dedupe_orbits(nrti + [offl_old, offl_new])
    assert kept == [offl_new]
    assert notes and '去掉 2 個' in notes[0]


def test_saved_web_page_is_explained(tmp_path):
    p = tmp_path / 'x.hdf'
    p.write_text('<!DOCTYPE html><title>Earthdata Login</title>')
    assert '登入頁' in s._unreadable_reason(str(p))
    (tmp_path / 'e.nc').write_bytes(b'')
    assert '空的' in s._unreadable_reason(str(tmp_path / 'e.nc'))


def test_superscript_units_become_mathtext_for_figures():
    # CJK fonts lack ⁻²; matplotlib draws mathtext superscripts instead
    assert s._mpl_units('molec cm⁻²') == 'molec cm$^{-2}$'
    assert s._mpl_units('K') == 'K'


def test_export_with_display_unit_renders(data):
    png, _ = s.figure(data['swaths'][0], 'PRODUCT', 'nitrogendioxide_tropospheric_column', {}, [118, 19, 124, 27], 0.5,
                      0.0, 2e-5, 'Viridis', False, 'plate', 600, 400, 100, 't', 's', unit=(6.02214076e19, 0.0, 'molec cm⁻²'))
    assert png[:8] == b'\x89PNG\r\n\x1a\n'


def test_outline_layers_are_independent():
    from ncglobe import server as s
    box = [100.0, 0.0, 140.0, 45.0]
    both = s.outlines(box, frozenset({'coast', 'borders'}))
    assert both['coast']['x'] and both['borders']['x']
    assert 'borders' not in s.outlines(box, frozenset({'coast'}))
    assert 'coast' not in s.outlines(box, frozenset({'borders'}))
    assert s._layers({'ly': 'coast,grid,evil'}) == {'coast', 'grid'}
    assert s._layers({'coast': '0'}) == frozenset() and s._layers({}) == {'coast'}   # 舊網址
    assert s._nice_step(0.7) == 1 and s._nice_step(100) == 30
