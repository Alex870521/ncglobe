"""HTTP layer: static whitelist, download names, concurrency without crashes."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from ncglobe import server as s


def _get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_static_files_are_whitelisted(http):
    assert _get(http + '/static/logo.svg')[0] == 200
    assert _get(http + '/favicon.ico')[0] == 200
    for bad in ('/static/../server.py', '/static/%2e%2e/server.py', '/static/index.html'):
        assert _get(http + bad)[0] == 404


def test_folder_outside_roots_is_forbidden(http):
    assert _get(http + '/api/ls?path=/etc')[0] == 403


def test_export_file_name_cannot_inject_headers(http, data):
    st = s.composite_start(data['swaths'], 'PRODUCT', 'nitrogendioxide_tropospheric_column', {}, 0.5, None)
    while s.composite_status(st['id'])['status'] == 'running':
        pass
    q = urllib.parse.urlencode({'id': st['id'], 'res': 0.1, 'name': 'a"\r\nX-Evil: 1'})
    code, headers, _ = _get(f'{http}/api/composite/export?{q}')
    assert code == 200 and 'X-Evil' not in headers
    assert headers['Content-Disposition'] == 'attachment; filename="a___X-Evil__1.nc"'


def test_many_threads_reading_files_at_once(http, data):
    errors = []

    def hit(i):
        f = data['swaths'][i % 3]
        try:
            for path in ('/api/info', '/api/var'):
                q = urllib.parse.urlencode({'file': f, 'group': 'PRODUCT', 'var': 'nitrogendioxide_tropospheric_column'})
                code, _, body = _get(f'{http}{path}?{q}')
                if code != 200:
                    errors.append(json.loads(body))
        except Exception as e:      # 執行緒裡的例外不會讓測試失敗,要自己收
            errors.append(repr(e))
    ts = [threading.Thread(target=hit, args=(i,)) for i in range(24)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []


def _post(url, headers=None):
    req = urllib.request.Request(url, data=b'', method='POST', headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_quit_needs_the_custom_header_then_stops_the_server(http):
    # 沒有 X-Ncglobe 標頭(例如別的網站用表單跨站送):不理
    assert _post(http + '/api/quit') == 404
    assert _get(http + '/')[0] == 200
    assert _post(http + '/api/quit', {'X-Ncglobe': 'quit'}) == 200
    import time
    for _ in range(50):
        try:
            urllib.request.urlopen(http + '/', timeout=0.5)
        except Exception:
            break
        time.sleep(0.1)
    else:
        raise AssertionError('server still answering after /api/quit')


def test_requests_with_a_foreign_host_header_are_refused(http):
    # DNS rebinding:對方網域解析到 127.0.0.1,請求的 Host 是對方的網域
    port = http.rsplit(':', 1)[1]
    for host in ('evil.example', f'evil.example:{port}', f'127.0.0.1.evil.example:{port}'):
        req = urllib.request.Request(http + '/api/ls', headers={'Host': host})
        try:
            urllib.request.urlopen(req, timeout=5)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 403, host
    assert _post(http + '/api/quit', {'X-Ncglobe': 'quit', 'Host': 'evil.example'}) == 403
    for host in (f'127.0.0.1:{port}', f'localhost:{port}', 'localhost'):
        req = urllib.request.Request(http + '/api/ls', headers={'Host': host})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200, host


def test_bad_parameters_get_plain_4xx_not_500(data, http):
    q = lambda **kw: http + '/api/' + kw.pop('_p') + '?' + urllib.parse.urlencode(kw)
    sw, V = data['swaths'][0], 'nitrogendioxide_tropospheric_column'
    assert _get(q(_p='slice', file=sw, group='PRODUCT', var=V, bbox='["abc",1,"NaN",9e999]'))[0] == 400
    assert _get(q(_p='info', file=str(data['root'])))[0] == 404          # 資料夾當成檔
    assert _get(q(_p='info', file=str(data['root'] / 'nope.nc')))[0] == 404
    assert _get(q(_p='composite/status', id='no-such-job'))[0] == 404
    # 色階填反:照樣畫(自動對調),不是 500
    code, _, body = _get(q(_p='render', file=sw, group='PRODUCT', var=V, idx='{}', bbox='[119,21,123,26]',
                           vmin=5, vmax=1, w=200, h=150))
    assert code == 200 and b'png' in body
    # 拖出地圖外、上下顛倒的範圍:夾回經緯度範圍並排好
    import pytest
    with pytest.raises(ValueError):          # 整個拖到南極以南:講清楚,不要 500
        s._bbox('[-180,-90,180,-664]')
    assert s._bbox('[121,26,119,21]') == [119.0, 21.0, 121.0, 26.0]


def test_settings_round_trip_and_validation(http, tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'SETTINGS_FILE', tmp_path / 'settings.json')
    monkeypatch.setattr(s, 'CONFIG_DIR', tmp_path)
    assert json.loads(_get(http + '/api/settings')[2])['cmap'] == 'Jet'     # 預設值

    def post(body, hdr='settings'):
        req = urllib.request.Request(http + '/api/settings', data=json.dumps(body).encode(), method='POST',
                                     headers={'X-Ncglobe': hdr, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    code, st = post({'name': ' 小明 ', 'theme': 'dark', 'qa': 0.75})
    assert code == 200 and st['name'] == '小明' and st['theme'] == 'dark' and st['qa'] == 0.75
    assert post({'qa': 5})[0] == 400 and post({'theme': 'pink'})[0] == 400 and post({'evil': 1})[0] == 400
    assert post({'name': 'x'}, hdr='')[0] == 404                         # 沒有自訂標頭:不理(防跨站)
    assert json.loads(_get(http + '/api/settings')[2])['name'] == '小明'   # 存到檔案、重讀還在
    (tmp_path / 'settings.json').write_text('{"qa": "garbage", "cmap": "Jet"')   # 被手改壞
    assert json.loads(_get(http + '/api/settings')[2])['qa'] == 0.5
    code, st = post({'update_repo': ''})                                 # 清空來源:不連網
    with urllib.request.urlopen(urllib.request.Request(http + '/api/update', headers={'X-Ncglobe': '1'}), timeout=5) as r:
        upd = json.loads(r.read())
    assert upd['repo'] == '' and upd['newer'] is False


def test_cross_site_requests_are_refused(data, http):
    # CSRF:別的網站用 <img>/fetch 打 127.0.0.1 —— Host 是對的,但瀏覽器會標 Sec-Fetch-Site、帶對方 Origin
    port = http.rsplit(':', 1)[1]

    def code(headers):
        req = urllib.request.Request(http + '/api/ls', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    assert code({'Sec-Fetch-Site': 'cross-site'}) == 403
    assert code({'Sec-Fetch-Site': 'same-site'}) == 403          # 同一台機器別的 port 的網頁
    assert code({'Origin': 'https://evil.example'}) == 403
    assert code({'Sec-Fetch-Site': 'same-origin', 'Origin': f'http://127.0.0.1:{port}'}) == 200
    assert code({'Sec-Fetch-Site': 'none'}) == 200                # 使用者直接開網址
    assert _post(http + '/api/quit', {'X-Ncglobe': 'quit', 'Sec-Fetch-Site': 'cross-site'}) == 403


def test_numeric_parameters_are_bounded(data, http):
    q = lambda **kw: http + '/api/' + kw.pop('_p') + '?' + urllib.parse.urlencode(kw)
    sw, V = data['swaths'][0], 'nitrogendioxide_tropospheric_column'
    base = dict(file=sw, group='PRODUCT', var=V, idx='{}', vmin=0, vmax=1e-4)
    assert _get(q(_p='tile', z=99, x=0, y=0, **base))[0] == 400           # 2**99 張圖磚
    assert _get(q(_p='tile', z=3, x=8, y=0, **base))[0] == 400            # x 超出這一層
    assert _get(q(_p='figure', w=99999, h=500, bbox='[119,21,123,26]', **base))[0] == 400
    assert _get(q(_p='figure', w=800, h=600, dpi=10**6, bbox='[119,21,123,26]', **base))[0] == 400
    assert _get(q(_p='slice', file=sw, group='PRODUCT', var=V, qa='nan'))[0] == 400


def test_side_effect_endpoints_need_the_custom_header(data, http):
    # 舊瀏覽器不送 Sec-Fetch-Site:跨站 <img src=…/api/composite/start> 仍要擋得住
    q = lambda **kw: http + '/api/composite/start?' + urllib.parse.urlencode(kw)
    url = q(files=json.dumps(data['swaths']), group='PRODUCT', var='nitrogendioxide_tropospheric_column', idx='{}')
    assert _get(url)[0] == 403
    with urllib.request.urlopen(urllib.request.Request(url, headers={'X-Ncglobe': '1'}), timeout=10) as r:
        assert r.status == 200
    assert _get(http + '/api/update')[0] == 403
