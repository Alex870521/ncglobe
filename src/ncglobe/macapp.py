"""macOS 的 ncglobe.app:用系統的 WebKit(WKWebView)開 ncglobe 自己的視窗。

伺服器在背景執行緒跑,主執行緒跑 Cocoa 的事件迴圈。網頁本身不變,原生視窗補上瀏覽器原本就有、
WKWebView 預設沒有的幾樣:編輯選單(沒有它 ⌘C/⌘V 在輸入框裡無效)、confirm/alert 對話框、
下載(PNG/CSV/NetCDF)改成「另存新檔」、外部連結交給預設瀏覽器。

Finder「打開方式 → ncglobe」與拖到 Dock 圖示:App 開著時 macOS 會把檔案送給這個程序
(application:openFiles:),每個檔開一個新視窗。關掉最後一個視窗時 App 結束,伺服器一起停。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

import AppKit
import objc
import WebKit
from Foundation import NSURL, NSObject, NSURLRequest
from PyObjCTools import AppHelper

DOC_TYPES = ['nc', 'nc4', 'cdf', 'h5', 'hdf5', 'he5', 'hdf', 'h4', 'he4']
ALLOW, CANCEL, DOWNLOAD = (WebKit.WKNavigationActionPolicyAllow, WebKit.WKNavigationActionPolicyCancel,
                           WebKit.WKNavigationActionPolicyDownload)


def _item(title: str, action: str | None, key: str = '', target=None, mods: int | None = None):
    it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    if target is not None:
        it.setTarget_(target)
    if mods is not None:
        it.setKeyEquivalentModifierMask_(mods)
    return it


def _menu(title: str, items) -> AppKit.NSMenuItem:
    m = AppKit.NSMenu.alloc().initWithTitle_(title)
    for it in items:
        m.addItem_(it if it is not None else AppKit.NSMenuItem.separatorItem())
    top = AppKit.NSMenuItem.alloc().init()
    top.setSubmenu_(m)
    return top


class _Controller(NSObject):
    """App delegate + 每個 WKWebView 的 navigation / UI / download delegate。"""

    def initWithBase_addRoot_quit_(self, base: str, add_root: Callable, quit_server: Callable):
        self = objc.super(_Controller, self).init()
        self.base = base
        self.add_root = add_root
        self.quit_server = quit_server
        self.windows = []
        self.got_files = False
        return self

    # ------------------------------------------------------------ App
    def applicationDidFinishLaunching_(self, _note):
        self.build_menu()
        # 滑鼠側鍵(3 = 上一頁、4 = 下一頁):在系統層接住、交給網頁的上一層 / 返回,不讓 WebKit 自己處理
        mask = AppKit.NSEventMaskOtherMouseDown | AppKit.NSEventMaskOtherMouseUp

        def side(ev):
            if ev.buttonNumber() not in (3, 4):
                return ev
            if ev.type() == AppKit.NSEventTypeOtherMouseUp:
                self.nav('back' if ev.buttonNumber() == 3 else 'forward')
            return None
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, side)
        AppHelper.callLater(0.5, self.first_window)   # 用「打開方式」啟動時,檔案視窗由 openFiles 開

    @objc.python_method
    def first_window(self):
        if not self.got_files and not self.windows:
            self.choose()

    def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
        return True

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, visible):   # 點 Dock 圖示
        if not self.windows:
            self.choose()
        return True

    @objc.python_method
    def choose(self):
        """「開啟」對話框:選資料夾(瀏覽)或檔案(直接開)。只讀使用者選的地方 —— 經由系統對話框選的
        位置就是使用者的同意,macOS 不會再為桌面、文件、外接碟各跳一次權限詢問。"""
        defaults = AppKit.NSUserDefaults.standardUserDefaults()
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(True)
        panel.setAllowedFileTypes_(DOC_TYPES)
        panel.setPrompt_('開啟')
        panel.setMessage_('選一個資料夾來瀏覽,或直接選資料檔')
        last = defaults.stringForKey_('lastFolder')
        if last and Path(last).is_dir():
            panel.setDirectoryURL_(NSURL.fileURLWithPath_(last))
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        if panel.runModal() != AppKit.NSModalResponseOK:
            if not self.windows:
                self.open_window(self.base)   # 取消:還是給一個視窗,裡面提示用 ⌘O
            return
        for u in panel.URLs():
            p = Path(u.path())
            if p.is_dir():
                self.add_root(p)
                defaults.setObject_forKey_(str(p), 'lastFolder')
                self.open_window(self.base + '#d=' + quote(str(p)))
            else:
                defaults.setObject_forKey_(str(p.parent), 'lastFolder')
                self.open_file(p)

    def application_openFiles_(self, app, names):
        self.got_files = True
        for n in names:
            self.open_file(Path(str(n)))
        app.replyToOpenOrPrint_(AppKit.NSApplicationDelegateReplySuccess)

    def applicationWillTerminate_(self, _note):
        self.quit_server()

    @objc.python_method
    def open_file(self, f: Path):
        if f.is_file():
            self.add_root(f.parent)
            self.open_window(self.base + '#f=' + quote(str(f)))

    # ------------------------------------------------------------ 選單
    @objc.python_method
    def build_menu(self):
        cmd, shift, ctrl = (AppKit.NSEventModifierFlagCommand, AppKit.NSEventModifierFlagShift,
                            AppKit.NSEventModifierFlagControl)
        bar = AppKit.NSMenu.alloc().init()
        bar.addItem_(_menu('ncglobe', [
            _item('關於 ncglobe', 'orderFrontStandardAboutPanel:'), None,
            _item('隱藏 ncglobe', 'hide:', 'h'), _item('隱藏其他', 'hideOtherApplications:', 'h', mods=cmd | AppKit.NSEventModifierFlagOption),
            None, _item('結束 ncglobe', 'terminate:', 'q')]))
        bar.addItem_(_menu('檔案', [
            _item('新視窗', 'newWindow:', 'n', self), _item('開啟…', 'openDocument:', 'o', self), None,
            _item('關閉視窗', 'performClose:', 'w')]))
        bar.addItem_(_menu('編輯', [
            _item('還原', 'undo:', 'z'), _item('重做', 'redo:', 'z', mods=cmd | shift), None,
            _item('剪下', 'cut:', 'x'), _item('拷貝', 'copy:', 'c'), _item('貼上', 'paste:', 'v'),
            _item('全選', 'selectAll:', 'a')]))
        bar.addItem_(_menu('顯示', [
            _item('上一層', 'navBack:', '[', self), _item('返回', 'navForward:', ']', self), None,
            _item('重新載入', 'reloadPage:', 'r', self), None,
            _item('進入全螢幕', 'toggleFullScreen:', 'f', mods=cmd | ctrl)]))
        win = _menu('視窗', [_item('縮到最小', 'performMiniaturize:', 'm'), _item('縮放', 'performZoom:')])
        bar.addItem_(win)
        AppKit.NSApp().setMainMenu_(bar)
        AppKit.NSApp().setWindowsMenu_(win.submenu())

    def newWindow_(self, _sender):
        self.choose()

    def openDocument_(self, _sender):
        self.choose()

    @objc.python_method
    def nav(self, direction: str):
        w = AppKit.NSApp().keyWindow()
        if w is not None and isinstance(w.contentView(), WebKit.WKWebView):
            w.contentView().evaluateJavaScript_completionHandler_(
                f"window.ncglobeNav && window.ncglobeNav('{direction}')", None)

    def navBack_(self, _sender):
        self.nav('back')

    def navForward_(self, _sender):
        self.nav('forward')

    def reloadPage_(self, _sender):
        w = AppKit.NSApp().keyWindow()
        if w is not None and isinstance(w.contentView(), WebKit.WKWebView):
            w.contentView().reload()

    # ------------------------------------------------------------ 視窗
    @objc.python_method
    def open_window(self, url: str):
        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        cfg.preferences().setValue_forKey_(True, 'developerExtrasEnabled')   # 右鍵「檢閱元件」
        frame = ((0, 0), (1440, 900))
        wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(frame, cfg)
        wv.setNavigationDelegate_(self)
        wv.setUIDelegate_(self)
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskResizable)
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, AppKit.NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)
        win.setTitle_('ncglobe')
        win.setContentView_(wv)
        win.setDelegate_(self)
        win.setTabbingMode_(AppKit.NSWindowTabbingModePreferred)   # 多個檔:可以合成分頁(視窗 › 合併所有視窗)
        if self.windows:
            win.setFrame_display_(self.windows[-1].frame(), False)
            win.setFrameTopLeftPoint_(win.cascadeTopLeftFromPoint_(
                (self.windows[-1].frame().origin.x, self.windows[-1].frame().origin.y + self.windows[-1].frame().size.height)))
        elif not win.setFrameUsingName_('ncglobe-main'):
            win.center()
        win.setFrameAutosaveName_('ncglobe-main' if not self.windows else '')
        wv.addObserver_forKeyPath_options_context_(self, 'title', 0, None)
        self.windows.append(win)
        wv.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        win.makeKeyAndOrderFront_(None)
        AppKit.NSApp().activateIgnoringOtherApps_(True)

    def observeValueForKeyPath_ofObject_change_context_(self, _path, wv, _change, _ctx):
        if wv.window() is not None:
            wv.window().setTitle_(wv.title() or 'ncglobe')

    def windowWillClose_(self, note):
        win = note.object()
        wv = win.contentView()
        if isinstance(wv, WebKit.WKWebView):
            wv.removeObserver_forKeyPath_(self, 'title')
            wv.stopLoading()
        if win in self.windows:
            self.windows.remove(win)

    # ------------------------------------------------------------ 導覽與下載
    @objc.python_method
    def open_external(self, url) -> None:
        """只把 http/https 交給預設瀏覽器。資料檔屬性裡的連結可能是 file:// 或別的 App 的自訂網址,不開。"""
        if url is not None and (url.scheme() or '').lower() in ('http', 'https'):
            AppKit.NSWorkspace.sharedWorkspace().openURL_(url)

    @objc.python_method
    def is_local(self, url) -> bool:
        u = urlparse(str(url.absoluteString()) if url is not None else '')
        return u.scheme in ('blob', 'about') or (u.hostname in ('127.0.0.1', 'localhost')
                                                          and str(url.absoluteString()).startswith(self.base))

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, wv, action, handler):
        if action.shouldPerformDownload():                     # <a download>(PNG、CSV、NetCDF)
            handler(DOWNLOAD)
        elif not self.is_local(action.request().URL()):        # 外部連結(說明、文獻):交給預設瀏覽器
            self.open_external(action.request().URL())
            handler(CANCEL)
        else:
            handler(ALLOW)

    def webView_decidePolicyForNavigationResponse_decisionHandler_(self, wv, resp, handler):
        r = resp.response()
        cd = r.allHeaderFields().get('Content-Disposition', '') if hasattr(r, 'allHeaderFields') else ''
        handler(WebKit.WKNavigationResponsePolicyDownload if (not resp.canShowMIMEType() or 'attachment' in cd)
                else WebKit.WKNavigationResponsePolicyAllow)

    def webView_navigationAction_didBecomeDownload_(self, wv, action, download):
        download.setDelegate_(self)

    def webView_navigationResponse_didBecomeDownload_(self, wv, resp, download):
        download.setDelegate_(self)

    def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(self, dl, resp, name, handler):
        panel = AppKit.NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(name)
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(Path.home() / 'Downloads')))
        if panel.runModal() != AppKit.NSModalResponseOK:
            handler(None)                                       # 取消 = 不下載
            return
        dest = panel.URL()
        p = Path(dest.path())
        if p.exists():                                          # 對話框已確認過覆蓋;WebKit 不會自己覆蓋
            p.unlink()
        handler(dest)

    def download_didFailWithError_resumeData_(self, dl, err, _data):
        self.alert(f'下載失敗:{err.localizedDescription()}')

    # ------------------------------------------------------------ JS 對話框與新視窗
    @objc.python_method
    def alert(self, text: str, cancel: bool = False) -> bool:
        a = AppKit.NSAlert.alloc().init()
        a.setMessageText_(text)
        a.addButtonWithTitle_('好')
        if cancel:
            a.addButtonWithTitle_('取消')
        return a.runModal() == AppKit.NSAlertFirstButtonReturn

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(self, wv, msg, frame, handler):
        self.alert(str(msg))
        handler()

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(self, wv, msg, frame, handler):
        handler(self.alert(str(msg), cancel=True))

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, wv, cfg, action, feats):
        url = action.request().URL()                            # target=_blank / window.open
        if self.is_local(url):
            self.open_window(str(url.absoluteString()))
        else:
            self.open_external(url)
        return None


def run(server, base_url: str, add_root: Callable[[Path], None], first: Path | None) -> None:
    """主執行緒跑事件迴圈;server(None = 已有別的 ncglobe 在這個 port)在背景執行緒 serve_forever。"""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    ctl = _Controller.alloc().initWithBase_addRoot_quit_(
        base_url, add_root, server.shutdown if server else (lambda: None))
    app.setDelegate_(ctl)
    if server is not None:
        def serve():
            server.serve_forever()
            AppHelper.callAfter(AppKit.NSApp().terminate_, None)   # 網頁上的「結束」→ 整個 App 結束
        threading.Thread(target=serve, daemon=True).start()
    if first is not None:
        ctl.got_files = True
        AppHelper.callAfter(ctl.open_file, first)
    AppHelper.runEventLoop()
