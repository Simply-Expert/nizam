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
    NSFont, NSAttributedString, NSColor, NSFontAttributeName, NSForegroundColorAttributeName,
    NSObject, NSTimer, NSWorkspace, NSURL, NSPanel, NSView, NSBezierPath, NSEvent,
    NSBackingStoreBuffered, NSMakePoint, NSScreen, NSFontWeightSemibold,
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
NSFloatingWindowLevel = 5
NSWindowStyleMaskBorderless = 0
NSWindowStyleMaskNonactivatingPanel = 1 << 7
NSWindowCollectionBehaviorCanJoinAllSpaces = 1 << 0
NSWindowCollectionBehaviorStationary = 1 << 4
NSWindowCollectionBehaviorFullScreenAuxiliary = 1 << 8
BADGE_H = 28


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


class BadgeView(NSView):
    """A draggable capsule showing the three counts. Click toggles the popover."""
    delegate = objc.ivar()
    counts = objc.ivar()
    _down = objc.ivar()
    _dragged = objc.ivar()

    def initWithFrame_delegate_(self, frame, delegate):
        self = objc.super(BadgeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.delegate = delegate
        self.counts = {"needs": 0, "working": 0, "inbox": 0}
        self._dragged = False
        return self

    def acceptsFirstMouse_(self, event):
        return True   # otherwise the first click after another app only focuses us

    @objc.python_method
    def segments(self):
        c = self.counts
        segs = []
        if c["needs"]:
            segs.append((f"🔔 {c['needs']}", NSColor.systemRedColor()))
        if c["working"]:
            segs.append((f"⚙ {c['working']}", NSColor.systemOrangeColor()))
        segs.append((f"📥 {c['inbox']}", NSColor.labelColor()))
        return segs

    @objc.python_method
    def attributed(self, text, color):
        font = NSFont.systemFontOfSize_weight_(12, NSFontWeightSemibold)
        return NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: font, NSForegroundColorAttributeName: color})

    @objc.python_method
    def desired_width(self):
        w = 14
        for text, color in self.segments():
            w += self.attributed(text, color).size().width + 12
        return max(60, w + 2)

    def drawRect_(self, rect):
        b = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, b.size.height / 2, b.size.height / 2)
        NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.92).setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.setLineWidth_(1)
        path.stroke()
        x = 8.0
        for text, color in self.segments():
            a = self.attributed(text, color)
            sz = a.size()
            a.drawAtPoint_(NSMakePoint(x, (b.size.height - sz.height) / 2))
            x += sz.width + 12

    def mouseDown_(self, event):
        self._down = event.locationInWindow()
        self._dragged = False

    def mouseDragged_(self, event):
        loc = event.locationInWindow()
        if not self._dragged and abs(loc.x - self._down.x) < 3 and abs(loc.y - self._down.y) < 3:
            return
        self._dragged = True
        win = self.window()
        o = win.frame().origin
        screen_loc = NSEvent.mouseLocation()
        win.setFrameOrigin_(NSMakePoint(screen_loc.x - self._down.x, screen_loc.y - self._down.y))

    def mouseUp_(self, event):
        if self._dragged:
            self.delegate.badgeMoved_(None)
        else:
            self.delegate.badgeClicked_(self)

    def rightMouseDown_(self, event):
        self.delegate.showMenuAt_(self)


def make_badge_panel(view_delegate):
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 120, BADGE_H),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, False)
    panel.setLevel_(NSFloatingWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setMovableByWindowBackground_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)
    view = BadgeView.alloc().initWithFrame_delegate_(NSMakeRect(0, 0, 120, BADGE_H), view_delegate)
    panel.setContentView_(view)
    return panel, view


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
    badge = objc.ivar()
    badge_view = objc.ivar()
    _menu_anchor = objc.ivar()

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
        self.popover.setDelegate_(self)
        self.badge, self.badge_view = make_badge_panel(self)
        self._restore_badge()
        if self.board.persist.data["prefs"].get("badge", True):
            self.badge.orderFrontRegardless()
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
        self.badge_view.counts = counts
        self.badge.setContentSize_(NSSize(self.badge_view.desired_width(), BADGE_H))
        self.badge_view.setFrameSize_(NSSize(self.badge_view.desired_width(), BADGE_H))
        self.badge_view.setNeedsDisplay_(True)
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
        self.status.button().setToolTip_("Nizam نظام — click for the board, right-click for options")

    @objc.python_method
    def _restore_badge(self):
        pos = self.board.persist.data["prefs"].get("badge_pos")
        screen = NSScreen.mainScreen().visibleFrame()
        if pos:
            self.badge.setFrameOrigin_(NSMakePoint(pos[0], pos[1]))
        else:
            self.badge.setFrameOrigin_(NSMakePoint(screen.origin.x + screen.size.width - 160,
                                                   screen.origin.y + screen.size.height - 50))

    def badgeMoved_(self, _):
        o = self.badge.frame().origin
        self.board.persist.set_pref("badge_pos", [o.x, o.y])

    def badgeClicked_(self, view):
        if self.popover.isShown():
            self.popover.close()
            return
        self.vc.view()
        self.popover.showRelativeToRect_ofView_preferredEdge_(view.bounds(), view, 1)
        NSApp.activateIgnoringOtherApps_(True)
        self._install_monitor()

    def showMenuAt_(self, view):
        self._menu_anchor = view
        self._show_menu()

    @objc.python_method
    def _install_monitor(self):
        # A transient popover on a non-activating panel misses outside clicks; watch for them.
        self._remove_monitor()
        def handler(event):
            if self.popover.isShown():
                self.performSelector_withObject_afterDelay_("closePopover:", None, 0.0)
        self.monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown, handler)

    @objc.python_method
    def _remove_monitor(self):
        if self.monitor is not None:
            NSEvent.removeMonitor_(self.monitor)
            self.monitor = None

    def closePopover_(self, _):
        if self.popover.isShown():
            self.popover.close()

    def popoverDidClose_(self, note):
        self._remove_monitor()

    def toggleBadge_(self, _):
        on = not self.board.persist.data["prefs"].get("badge", True)
        self.board.persist.set_pref("badge", on)
        if on:
            self.badge.orderFrontRegardless()
        else:
            self.badge.orderOut_(None)

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
        badge = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Floating badge", "toggleBadge:", "")
        badge.setState_(1 if self.board.persist.data["prefs"].get("badge", True) else 0)
        menu.addItem_(badge)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Nizam", "quit:", "q"))
        for i in range(menu.numberOfItems()):
            menu.itemAtIndex_(i).setTarget_(self)
        anchor = self._menu_anchor
        self._menu_anchor = None
        if anchor is not None:
            menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(0, 0), anchor)
        else:
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
