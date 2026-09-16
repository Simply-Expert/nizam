"""Menu-bar shell: live counts in the status bar, a popover hosting the
board, notifications when a session needs you. Runs the HTTP server in a
background thread of the same process.

Requires PyObjC (Cocoa + WebKit); `nizam install` builds ~/.nizam/venv.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSMenuItem,
    NSPopover, NSStatusBar, NSViewController, NSVariableStatusItemLength, NSMakeRect, NSSize,
    NSFont, NSAttributedString, NSColor, NSFontAttributeName,
    NSObject, NSTimer, NSWorkspace, NSURL,
)
from Foundation import NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration
import objc

from .paths import NIZAM_DIR, ensure_dirs
from .launch import PID_FILE, QUIT_FLAG, login_enabled, set_login
from .server import make_server
from .state import Board

POPOVER_SIZE = (1000, 660)
REFRESH_SECS = 3.0
NSPopoverBehaviorTransient = 1
NSEventMaskLeftMouseDown = 1 << 1
NSEventMaskRightMouseDown = 1 << 3


def _notify(title: str, body: str) -> None:
    def q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.Popen(["osascript", "-e",
                      f'display notification "{q(body)}" with title "{q(title)}" sound name "Tink"'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class BoardVC(NSViewController):
    web = objc.ivar()
    port = objc.ivar()

    def initWithPort_(self, port):
        self = objc.super(BoardVC, self).init()
        if self is None:
            return None
        self.port = port
        return self

    def loadView(self):
        cfg = WKWebViewConfiguration.alloc().init()
        self.web = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, *POPOVER_SIZE), cfg)
        self.web.setValue_forKey_(False, "drawsBackground")
        self.setView_(self.web)
        self.reload()

    @objc.python_method
    def reload(self):
        url = NSURL.URLWithString_(f"http://127.0.0.1:{self.port}/?embed=1")
        self.web.loadRequest_(NSURLRequest.requestWithURL_(url))


class AppDelegate(NSObject):
    status = objc.ivar()
    popover = objc.ivar()
    vc = objc.ivar()
    board = objc.ivar()
    port = objc.ivar()
    timer = objc.ivar()
    monitor = objc.ivar()
    seen_needs = objc.ivar()
    seeded = objc.ivar()

    def initWithBoard_port_(self, board, port):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.board = board
        self.port = port
        self.seen_needs = set()
        self.seeded = False
        return self

    def applicationDidFinishLaunching_(self, note):
        sys.stderr.write("nizam app: launched\n")
        self._install_edit_menu()
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        btn = self.status.button()
        btn.setTarget_(self)
        btn.setAction_("statusClicked:")
        btn.sendActionOn_(NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown)
        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setContentSize_(NSSize(*POPOVER_SIZE))
        self.vc = BoardVC.alloc().initWithPort_(self.port)
        self.popover.setContentViewController_(self.vc)
        self.refresh_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_SECS, self, "refresh:", None, True)

    @objc.python_method
    def _install_edit_menu(self):
        # An accessory app has no menu bar, so ⌘C/⌘V/⌘A inside the web view
        # only work if we register the key equivalents ourselves.
        menubar = NSMenu.alloc().init()
        edit_item = NSMenuItem.alloc().init()
        menubar.addItem_(edit_item)
        edit = NSMenu.alloc().initWithTitle_("Edit")
        for title, sel, key in (("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
            edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key))
        edit_item.setSubmenu_(edit)
        NSApp.setMainMenu_(menubar)

    def refresh_(self, _timer):
        if QUIT_FLAG.exists():
            QUIT_FLAG.unlink(missing_ok=True)
            self.quit_(None)
            return
        try:
            snap = self.board.snapshot()
        except Exception as e:  # keep the runloop alive no matter what
            sys.stderr.write(f"refresh failed: {e}\n")
            return
        counts = {"needs": 0, "working": 0, "inbox": 0}
        needs_now = {}
        for s in snap["sessions"]:
            if s["bucket"] in counts:
                counts[s["bucket"]] += 1
            if s["bucket"] == "needs":
                needs_now[s["id"]] = s
        self._set_title(counts)
        new_ids = set(needs_now) - self.seen_needs
        if self.seeded and new_ids:
            if len(new_ids) == 1:
                s = needs_now[next(iter(new_ids))]
                _notify(f"{s['agent_name']} needs you", f"{s['label']} · {s['title']}")
            else:
                _notify("Nizam", f"{len(new_ids)} sessions need you")
        self.seen_needs = set(needs_now)
        self.seeded = True

    @objc.python_method
    def _set_title(self, c):
        parts = []
        if c["needs"]:
            parts.append(("🔔", c["needs"], NSColor.systemRedColor()))
        if c["working"]:
            parts.append(("⚙", c["working"], NSColor.systemOrangeColor()))
        parts.append(("📥", c["inbox"], NSColor.labelColor()))
        font = NSFont.menuBarFontOfSize_(0)
        text = "  ".join(f"{e}{n}" for e, n, _ in parts)
        attrs = {NSFontAttributeName: font}
        self.status.button().setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
        if not getattr(self, "_titled", False):
            self._titled = True
            sys.stderr.write(f"nizam app: status item titled {text!r}, visible={self.status.isVisible()}\n")
        self.status.button().setToolTip_("Nizam نظام — click for the board, right-click for options")

    def statusClicked_(self, sender):
        ev = NSApp.currentEvent()
        if ev is not None and ev.type() == 3:   # right mouse down
            self._show_menu()
            return
        self.togglePopover_(sender)

    def togglePopover_(self, sender):
        if self.popover.isShown():
            self.popover.close()
            return
        self.vc.view()   # force loadView before the first show
        self.popover.showRelativeToRect_ofView_preferredEdge_(
            self.status.button().bounds(), self.status.button(), 1)
        NSApp.activateIgnoringOtherApps_(True)

    @objc.python_method
    def _show_menu(self):
        menu = NSMenu.alloc().init()
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Open in browser", "openBrowser:", ""))
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Reload board", "reloadBoard:", ""))
        login = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Start at login", "toggleLogin:", "")
        login.setState_(1 if login_enabled() else 0)
        menu.addItem_(login)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Nizam", "quit:", "q"))
        for i in range(menu.numberOfItems()):
            menu.itemAtIndex_(i).setTarget_(self)
        self.status.popUpStatusItemMenu_(menu)

    def openBrowser_(self, _):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(f"http://127.0.0.1:{self.port}/"))

    def reloadBoard_(self, _):
        self.vc.view()
        self.vc.reload()

    def toggleLogin_(self, _):
        set_login(not login_enabled())

    def quit_(self, _):
        if self.timer is not None:
            self.timer.invalidate()
        PID_FILE.unlink(missing_ok=True)
        NSApp.terminate_(None)


def run(port: int) -> int:
    ensure_dirs()
    from .launch import pid_alive
    if PID_FILE.exists() and pid_alive(int(PID_FILE.read_text() or 0)):
        sys.stderr.write("Nizam is already running in the menu bar.\n")
        return 1
    PID_FILE.write_text(str(os.getpid()))
    board = Board()
    httpd = make_server(board, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().initWithBoard_port_(board, port)
    app.setDelegate_(delegate)
    app.run()
    return 0
