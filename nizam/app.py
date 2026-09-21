"""Menu-bar shell: live counts in the status bar, a popover hosting the
board, notifications when a session needs you. Runs the HTTP server in a
background thread of the same process.

Requires PyObjC (Cocoa + WebKit); `nizam install` builds ~/.nizam/venv.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from urllib.parse import quote

from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSMenuItem,
    NSPopover, NSStatusBar, NSViewController, NSVariableStatusItemLength, NSMakeRect, NSSize,
    NSFont, NSAttributedString, NSColor, NSFontAttributeName, NSForegroundColorAttributeName,
    NSObject, NSTimer, NSWorkspace, NSURL, NSPanel, NSView, NSBezierPath, NSEvent,
    NSBackingStoreBuffered, NSMakePoint, NSScreen, NSFontWeightSemibold,
    NSImage, NSImageSymbolConfiguration, NSMutableAttributedString, NSTextAttachment,
    NSTrackingArea, NSMutableParagraphStyle, NSParagraphStyleAttributeName,
)
from Foundation import NSURLRequest, NSPointInRect
from WebKit import WKWebView, WKWebViewConfiguration
import objc

from . import terminal, update
from .paths import NIZAM_DIR, ensure_dirs
from .cli import start_update
from .launch import PID_FILE, QUIT_FLAG, app_running, login_enabled, set_login
from .server import make_server
from .state import Board

POPOVER_SIZE = (1080, 680)
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
NSTrackingMouseEnteredAndExited = 1 << 0
NSTrackingMouseMoved = 1 << 1
NSTrackingActiveAlways = 1 << 7
NSTrackingInVisibleRect = 1 << 9
NSLineBreakByTruncatingTail = 4
BADGE_H = 28
LIST_W, LIST_ROW_H, LIST_HEAD_H, LIST_PAD, LIST_MAX = 300, 38, 26, 6, 10
HOVER_LINGER_SECS = 0.3
LIMITS = "limits"
UPDATE = "update"
LIST_HEADS = {LIMITS: "Plan usage", UPDATE: "Update"}
LIMIT_WARN, LIMIT_HIGH = 70, 90
LIMIT_AHEAD = 10             # points of slack before use counts as ahead of pace
LIMIT_STALE_SECS = 30 * 60   # headless runs spend the plan without reporting it


# bucket -> (SF Symbol, fallback glyph, colour, tooltip wording)
SEGMENTS = {
    "needs": ("hand.raised.fill", "✋", NSColor.systemRedColor, "{n} waiting on you"),
    "working": ("hourglass", "⏳", NSColor.systemOrangeColor, "{n} working"),
    "inbox": ("arrowshape.turn.up.left.fill", "↩", NSColor.systemGreenColor, "{n} your turn to reply"),
    "due": ("diamond.fill", "◆", NSColor.systemPurpleColor, "{n} follow-ups due"),
}


def _shown(counts: dict) -> list[str]:
    return [k for k in SEGMENTS if counts[k] or k == "inbox"]


def _icon(symbol: str, glyph: str, color, font) -> NSMutableAttributedString:
    out = NSMutableAttributedString.alloc().init()
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
    if img is None:
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            glyph, {NSFontAttributeName: font, NSForegroundColorAttributeName: color}))
    else:
        cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(font.pointSize(), NSFontWeightSemibold)
        cfg = cfg.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithPaletteColors_([color]))
        img = img.imageWithSymbolConfiguration_(cfg)
        att = NSTextAttachment.alloc().init()
        att.setImage_(img)
        sz = img.size()
        # Sit the symbol on the digits' optical centre rather than the baseline.
        att.setBounds_(NSMakeRect(0, (font.capHeight() - sz.height) / 2, sz.width, sz.height))
        out.appendAttributedString_(NSAttributedString.attributedStringWithAttachment_(att))
    return out


def _segment(bucket: str, n: int, font, tint_count: bool) -> NSAttributedString:
    symbol, glyph, color_fn, _ = SEGMENTS[bucket]
    color = color_fn()
    count_attrs = {NSFontAttributeName: font}
    if tint_count:
        count_attrs[NSForegroundColorAttributeName] = color
    out = _icon(symbol, glyph, color, font)
    out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(f" {n}", count_attrs))
    return out


def _passed(limit: dict, now: float) -> float | None:
    """Percent of the window already gone."""
    if not (limit["resets_at"] and limit.get("window_secs")):
        return None
    return min(max(1 - (limit["resets_at"] - now) / limit["window_secs"], 0), 1) * 100


def _limit_color(limit: dict, now: float):
    if now - limit["seen_at"] > LIMIT_STALE_SECS:
        return NSColor.tertiaryLabelColor()
    if limit["used"] >= LIMIT_HIGH:
        return NSColor.systemRedColor()
    passed = _passed(limit, now)
    if limit["used"] >= LIMIT_WARN or (passed is not None and limit["used"] - passed > LIMIT_AHEAD):
        return NSColor.systemOrangeColor()
    return NSColor.secondaryLabelColor()


def _limits_segment(limits: list, font) -> NSAttributedString:
    now = time.time()
    worst = max(limits, key=lambda l: l["used"])
    out = _icon("gauge.with.needle", "◔", _limit_color(worst, now), font)
    for i, l in enumerate(limits):
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            " · " if i else " ", {NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.tertiaryLabelColor()}))
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            f"{l['used']:.0f}%", {NSFontAttributeName: font, NSForegroundColorAttributeName: _limit_color(l, now)}))
    return out


def _span(secs: float) -> str:
    m = int(secs // 60)
    if m < 60:
        return f"{m}m"
    if m < 48 * 60:
        return f"{m // 60}h {m % 60}m"
    return f"{m // 1440}d {m % 1440 // 60}h"


def _limit_rows(limits: list) -> list[dict]:
    now = time.time()
    rows = []
    for l in limits:
        resets = (time.strftime("resets %a %H:%M", time.localtime(l["resets_at"])) + f" · in {_span(l['resets_at'] - now)}"
                  if l["resets_at"] else "window restarted")
        title = f"{l['label']} · {l['used']:.0f}% used"
        passed = _passed(l, now)
        if passed is not None:
            title += f" · {passed:.0f}% of time passed"
        rows.append({"id": None, "inert": True, "title": title,
                     "sub": f"{resets} · seen {_span(now - l['seen_at'])} ago"})
    return rows


def _legend(counts: dict) -> str:
    return " · ".join(SEGMENTS[k][3].format(n=counts[k]) for k in _shown(counts))


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
    def reload(self, select=None):
        url = f"http://127.0.0.1:{self.port}/?embed=1" + (f"&select={quote(select, safe='')}" if select else "")
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

    @objc.python_method
    def select(self, item_id):
        if self.web.isLoading():
            self.reload(item_id)
        else:
            self.web.evaluateJavaScript_completionHandler_(f"nizamSelect({json.dumps(item_id)})", None)


def _track_hover(view):
    for t in list(view.trackingAreas()):
        view.removeTrackingArea_(t)
    view.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
        view.bounds(), NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved
        | NSTrackingActiveAlways | NSTrackingInVisibleRect, view, None))


class BadgeView(NSView):
    """A draggable capsule showing the counts. Click toggles the popover; hovering a count lists what is behind it."""
    delegate = objc.ivar()
    counts = objc.ivar()
    limits = objc.ivar()
    has_update = objc.ivar()
    _spans = objc.ivar()
    _down = objc.ivar()
    _dragged = objc.ivar()

    def initWithFrame_delegate_(self, frame, delegate):
        self = objc.super(BadgeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.delegate = delegate
        self.counts = {"needs": 0, "working": 0, "inbox": 0, "due": 0}
        self.limits = []
        self._dragged = False
        self._spans = []
        return self

    def acceptsFirstMouse_(self, event):
        return True   # otherwise the first click after another app only focuses us

    def updateTrackingAreas(self):
        objc.super(BadgeView, self).updateTrackingAreas()
        _track_hover(self)

    @objc.python_method
    def segments(self):
        font = NSFont.systemFontOfSize_weight_(12, NSFontWeightSemibold)
        out = [(k, _segment(k, self.counts[k], font, True)) for k in _shown(self.counts)]
        if self.limits:
            out.append((LIMITS, _limits_segment(self.limits, font)))
        if self.has_update:
            out.append((UPDATE, _icon("arrow.down.circle", "↓", NSColor.systemBlueColor(), font)))
        return out

    @objc.python_method
    def _hover(self, event):
        x = self.convertPoint_fromView_(event.locationInWindow(), None).x
        for bucket, x0, x1 in self._spans:
            if x < x1:
                return self.delegate.hover(bucket, x0)

    def mouseEntered_(self, event):
        self._hover(event)

    def mouseMoved_(self, event):
        self._hover(event)

    def mouseExited_(self, event):
        self.delegate.hover_left()

    @objc.python_method
    def desired_width(self):
        w = 14
        for _, a in self.segments():
            w += a.size().width + 12
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
        spans = []
        for k, a in self.segments():
            sz = a.size()
            a.drawAtPoint_(NSMakePoint(x, (b.size.height - sz.height) / 2))
            spans.append((k, x, x + sz.width + 6))
            x += sz.width + 12
        if spans:
            spans[-1] = (spans[-1][0], spans[-1][1], b.size.width)
        self._spans = spans

    def mouseDown_(self, event):
        self.delegate.hide_list()
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
        self.delegate.hide_list()
        self.delegate.showMenuAt_(self)


class HoverListView(NSView):
    """The rows behind one badge count. Click a row to go to it."""
    delegate = objc.ivar()
    bucket = objc.ivar()
    head = objc.ivar()
    rows = objc.ivar()
    hot = objc.ivar()

    def initWithFrame_delegate_(self, frame, delegate):
        self = objc.super(HoverListView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.delegate = delegate
        self.rows = []
        self.hot = -1
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        objc.super(HoverListView, self).updateTrackingAreas()
        _track_hover(self)

    @objc.python_method
    def desired_height(self):
        return LIST_HEAD_H + LIST_ROW_H * len(self.rows) + LIST_PAD

    @objc.python_method
    def _row_at(self, event):
        y = self.convertPoint_fromView_(event.locationInWindow(), None).y - LIST_HEAD_H
        i = int(y // LIST_ROW_H)
        return i if y >= 0 and i < len(self.rows) else -1

    @objc.python_method
    def _set_hot(self, i):
        if i != self.hot:
            self.hot = i
            self.setNeedsDisplay_(True)

    def mouseEntered_(self, event):
        self._set_hot(self._row_at(event))

    def mouseMoved_(self, event):
        self._set_hot(self._row_at(event))

    def mouseExited_(self, event):
        self._set_hot(-1)
        self.delegate.hover_left()

    def mouseUp_(self, event):
        i = self._row_at(event)
        if i >= 0:
            self.delegate.list_picked(self.rows[i])

    def drawRect_(self, rect):
        if not self.bucket:
            return
        b = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, 10, 10)
        NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.97).setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.setLineWidth_(1)
        path.stroke()
        para = NSMutableParagraphStyle.alloc().init()
        para.setLineBreakMode_(NSLineBreakByTruncatingTail)

        def text(s, x, y, size, weight, color):
            NSAttributedString.alloc().initWithString_attributes_(s, {
                NSFontAttributeName: NSFont.systemFontOfSize_weight_(size, weight),
                NSForegroundColorAttributeName: color,
                NSParagraphStyleAttributeName: para,
            }).drawInRect_(NSMakeRect(x, y, b.size.width - x - 12, size + 5))

        head_color = SEGMENTS[self.bucket][2]() if self.bucket in SEGMENTS else NSColor.secondaryLabelColor()
        text(self.head, 12, 7, 11, NSFontWeightSemibold, head_color)
        for i, row in enumerate(self.rows):
            y = LIST_HEAD_H + i * LIST_ROW_H
            if i == self.hot and not row.get("inert"):
                NSColor.labelColor().colorWithAlphaComponent_(0.1).setFill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(4, y, b.size.width - 8, LIST_ROW_H), 6, 6).fill()
            text(row["title"], 12, y + 4, 12, 0.0, NSColor.labelColor())
            text(row["sub"], 12, y + 20, 10, 0.0, NSColor.secondaryLabelColor())


def _floating_panel(width, height):
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, width, height),
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
    return panel


def make_badge_panel(view_delegate):
    panel = _floating_panel(120, BADGE_H)
    view = BadgeView.alloc().initWithFrame_delegate_(NSMakeRect(0, 0, 120, BADGE_H), view_delegate)
    panel.setContentView_(view)
    return panel, view


def make_list_panel(view_delegate):
    panel = _floating_panel(LIST_W, LIST_HEAD_H)
    view = HoverListView.alloc().initWithFrame_delegate_(NSMakeRect(0, 0, LIST_W, LIST_HEAD_H), view_delegate)
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
    list_panel = objc.ivar()
    list_view = objc.ivar()
    items = objc.ivar()
    hover_bucket = objc.ivar()
    hover_x = objc.ivar()
    update_info = objc.ivar()
    updating = objc.ivar()

    def initWithBoard_port_(self, board, port):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.board = board
        self.port = port
        self.seen_needs = set()
        self.seeded = False
        self.items = {}
        self.updating = False
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
        self.list_panel, self.list_view = make_list_panel(self)
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
        items = {k: [] for k in SEGMENTS}
        needs_now = {}
        agent_names = {a["root"]: a["display_name"] for a in snap["agents"]}
        for s in snap["sessions"]:
            if s["bucket"] in items:
                items[s["bucket"]].append({"id": s["id"], "title": s["title"], "session": s,
                                           "sub": f"{s['agent_name']} · {s['label']} · {s['ago']}"})
            if s["bucket"] == "needs":
                needs_now[s["id"]] = s
        for f in snap.get("followups", []):
            if f["due"] != "upcoming":
                when = "today" if f["due"] == "today" else f"{-f['days']}d overdue"
                items["due"].append({"id": f["id"], "title": f["title"],
                                     "sub": f"{agent_names.get(f['agent'], '')} · {when}"})
        for r in snap.get("routines", []):
            if r["failing"]:
                n = {"agent_name": agent_names.get(r["agent"], ""),
                     "label": "Routine " + r["status"].replace("_", " "), "title": r["name"]}
                needs_now[f"{r['id']}:{r['last']['run_id']}"] = n
                items["needs"].append({"id": r["id"], "title": r["name"], "sub": f"{n['agent_name']} · {n['label']}"})
        counts = {k: len(v) for k, v in items.items()}
        limits = snap.get("limits", [])
        items[LIMITS] = _limit_rows(limits)
        self._sync_update(items)
        self.items = items
        self._sync_list()
        self._set_title(counts)
        self.badge_view.counts = counts
        self.badge_view.limits = limits
        self.badge_view.has_update = bool(items[UPDATE])
        self.badge.setContentSize_(NSSize(self.badge_view.desired_width(), BADGE_H))
        self.badge_view.setFrameSize_(NSSize(self.badge_view.desired_width(), BADGE_H))
        self.badge_view.setNeedsDisplay_(True)
        new_ids = set(needs_now) - self.seen_needs
        if self.seeded and new_ids:
            if len(new_ids) == 1:
                s = needs_now[next(iter(new_ids))]
                _notify(f"{s['agent_name']} needs you", f"{s['label']} · {s['title']}")
            else:
                _notify("Nizam", f"{len(new_ids)} need you")
        self.seen_needs = set(needs_now)
        self.seeded = True

    @objc.python_method
    def _sync_update(self, items):
        info = update.poll()
        self.update_info = info if info and info["latest"] else None
        items[UPDATE] = []
        if not self.update_info:
            return
        latest = self.update_info["latest"]
        items[UPDATE].append({"id": None, "update": True, "title": f"Nizam {latest} is available",
                              "sub": "Updating…" if self.updating else f"You have {self.update_info['current']} · click to update"})
        if self.board.persist.data["prefs"].get("update_notified") != latest:
            self.board.persist.set_pref("update_notified", latest)
            _notify("Nizam", f"{latest} is available. Right-click the badge to update.")

    @objc.python_method
    def _set_title(self, c):
        font = NSFont.menuBarFontOfSize_(0)
        title = NSMutableAttributedString.alloc().init()
        for i, k in enumerate(_shown(c)):
            if i:
                title.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_("   ", {NSFontAttributeName: font}))
            title.appendAttributedString_(_segment(k, c[k], font, False))
        self.status.button().setAttributedTitle_(title)
        tip = f"Nizam نظام — {_legend(c)}\nClick for the board, right-click for options"
        if self.update_info:
            tip += f"\n{self.update_info['latest']} is available"
        self.status.button().setToolTip_(tip)

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

    @objc.python_method
    def hover(self, bucket, x):
        if self.popover.isShown():
            return
        if bucket != self.hover_bucket:
            self.hover_bucket, self.hover_x = bucket, x
            self._sync_list()

    @objc.python_method
    def hover_left(self):
        # Leaving the badge for the list crosses a gap, so decide a beat later.
        self.performSelector_withObject_afterDelay_("hoverCheck:", None, HOVER_LINGER_SECS)

    def hoverCheck_(self, _):
        p = NSEvent.mouseLocation()
        if NSPointInRect(p, self.badge.frame()) or (self.list_panel.isVisible() and NSPointInRect(p, self.list_panel.frame())):
            return
        self.hide_list()

    @objc.python_method
    def hide_list(self):
        self.hover_bucket = None
        self.list_panel.orderOut_(None)

    @objc.python_method
    def _sync_list(self):
        rows = self.items.get(self.hover_bucket) if self.hover_bucket else None
        if not rows:
            self.list_panel.orderOut_(None)
            return
        v = self.list_view
        v.bucket = self.hover_bucket
        v.head = (SEGMENTS[self.hover_bucket][3].format(n=len(rows)) if self.hover_bucket in SEGMENTS
                  else LIST_HEADS[self.hover_bucket])
        more = len(rows) - LIST_MAX
        v.rows = rows[:LIST_MAX] + ([{"id": None, "title": f"+{more} more", "sub": "Open the board"}] if more > 0 else [])
        h = v.desired_height()
        badge = self.badge.frame()
        screen = (self.badge.screen() or NSScreen.mainScreen()).visibleFrame()
        x = min(max(badge.origin.x + self.hover_x - 12, screen.origin.x), screen.origin.x + screen.size.width - LIST_W)
        y = badge.origin.y - 4 - h
        if y < screen.origin.y:
            y = badge.origin.y + BADGE_H + 4
        self.list_panel.setFrame_display_(NSMakeRect(x, y, LIST_W, h), True)
        v.setNeedsDisplay_(True)
        self.list_panel.orderFrontRegardless()

    @objc.python_method
    def list_picked(self, row):
        if row.get("update"):
            self.hide_list()
            self.startUpdate_(None)
            return
        if row.get("inert"):
            return
        self.hide_list()
        if row.get("session"):
            launcher = self.board.persist.data["prefs"].get("launcher", "Terminal")
            threading.Thread(target=terminal.open_session, args=(row["session"], launcher), daemon=True).start()
            return
        self.badgeClicked_(self.badge_view)
        if row["id"]:
            self.vc.select(row["id"])

    def badgeClicked_(self, view):
        if self.popover.isShown():
            self.popover.close()
            return
        self.hide_list()
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
        if self.update_info:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"Update to {self.update_info['latest']}", None if self.updating else "startUpdate:", "")
        else:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Check for updates", "checkUpdates:", "")
        menu.addItem_(item)
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

    def startUpdate_(self, _):
        if self.updating or not self.update_info:
            return
        self.updating = True
        _notify("Nizam", f"Updating to {self.update_info['latest']}…")

        def work():
            failed = start_update().wait()
            self.updating = False
            if failed:
                _notify("Nizam update failed", f"See {NIZAM_DIR / 'update.log'}")
        threading.Thread(target=work, daemon=True).start()

    def checkUpdates_(self, _):
        def work():
            try:
                info = update.check()
            except update.UpdateError as e:
                _notify("Nizam", f"Could not check for updates: {e}")
                return
            if info["latest"]:
                self.board.persist.set_pref("update_notified", info["latest"])
                _notify("Nizam", f"{info['latest']} is available. Right-click the badge to update.")
            else:
                _notify("Nizam", f"{info['current']} is up to date")
        threading.Thread(target=work, daemon=True).start()

    def quit_(self, _):
        if self.timer is not None:
            self.timer.invalidate()
        PID_FILE.unlink(missing_ok=True)
        NSApp.terminate_(None)


def run(port: int) -> int:
    ensure_dirs()
    if app_running():
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
