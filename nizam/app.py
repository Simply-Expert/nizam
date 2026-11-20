"""Menu-bar shell: live counts in the status bar, a popover hosting the
board, notifications when a session needs you. Runs the HTTP server in a
background thread of the same process.

Requires PyObjC (Cocoa + WebKit); `nizam install` builds ~/.nizam/venv.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import quote

from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSMenuItem,
    NSPopover, NSStatusBar, NSViewController, NSVariableStatusItemLength, NSMakeRect, NSSize,
    NSFont, NSAttributedString, NSColor, NSFontAttributeName, NSForegroundColorAttributeName,
    NSObject, NSTimer, NSWorkspace, NSURL, NSPanel, NSView, NSBezierPath, NSEvent,
    NSBackingStoreBuffered, NSMakePoint, NSScreen, NSFontWeightSemibold,
    NSImage, NSImageSymbolConfiguration, NSMutableAttributedString, NSTextAttachment,
    NSTrackingArea, NSMutableParagraphStyle, NSParagraphStyleAttributeName, NSWindow,
    NSGraphicsContext, NSAffineTransform, NSZeroRect, NSCompositingOperationSourceOver,
)
from Foundation import NSBundle, NSURLRequest, NSPointInRect, NSNotificationCenter
from UserNotifications import (
    UNUserNotificationCenter, UNMutableNotificationContent, UNNotificationRequest, UNNotificationSound,
    UNAuthorizationOptionAlert, UNAuthorizationOptionSound, UNNotificationPresentationOptionBanner,
    UNNotificationPresentationOptionList, UNNotificationPresentationOptionSound,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
import objc

from . import chip, providers, telegram, terminal, triage, update
from .paths import NIZAM_DIR, ensure_dirs
from .cli import start_update
from .launch import BUNDLE_ID, NOTIFICATION_SOUND, PID_FILE, QUIT_FLAG, app_running, login_enabled, set_login
from .runner import login_path
from .server import make_server
from .state import Board, needs_you

POPOVER_SIZE = (1080, 680)
BOARD_MIN_SIZE = (760, 440)
BOARD_FRAME = "NizamBoard"
REFRESH_SECS = 3.0
NOTIFICATION_SETTINGS = f"x-apple.systempreferences:com.apple.Notifications-Settings.extension?id={BUNDLE_ID}"
NSPopoverBehaviorTransient = 1
NSEventMaskLeftMouseDown = 1 << 1
NSEventMaskRightMouseDown = 1 << 3
NSFloatingWindowLevel = 5
NSWindowAbove = 1
NSWindowStyleMaskBorderless = 0
NSWindowStyleMaskTitled = 1 << 0
NSWindowStyleMaskClosable = 1 << 1
NSWindowStyleMaskResizable = 1 << 3
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
LIST_ICON = 14
HOVER_LINGER_SECS = 0.3
SPIN_SECS, SPIN_STEP = 1 / 30, 12
CHIP_H, CHIP_AV, CHIP_MARGIN, CHIP_GAP, CHIP_COMPACT_W, CHIP_LIST_W = 52, 34, 10, 4, 46, 380
CHIP_NEEDS_WEIGHT = 1.15
CHIP = "chip"
LIMITS = "limits"
UPDATE = "update"
LIST_HEADS = {LIMITS: "Plan usage", UPDATE: "Update"}
LIMIT_WARN, LIMIT_HIGH = 70, 90
LIMIT_AHEAD = 10             # points of slack before use counts as ahead of pace
LIMIT_STALE_SECS = 30 * 60   # headless runs spend the plan without reporting it


# bucket -> (SF Symbol, fallback glyph, colour, tooltip wording)
SEGMENTS = {
    "star": ("star.fill", "★", NSColor.systemYellowColor, "{n} starred waiting on you"),
    "needs": ("hand.raised.fill", "✋", NSColor.systemRedColor, "{n} waiting on you"),
    "working": ("hourglass", "⏳", NSColor.systemOrangeColor, "{n} working"),
    "inbox": ("arrowshape.turn.up.left.fill", "↩", NSColor.systemGreenColor, "{n} your turn to reply"),
    "due": ("diamond.fill", "◆", NSColor.systemPurpleColor, "{n} follow-ups due"),
}


ROW_ICONS = {
    "needs": ("hand.raised.fill", "✋", NSColor.systemRedColor),
    "working": ("hourglass", "⏳", NSColor.systemOrangeColor),
    "inbox": ("arrowshape.turn.up.left.fill", "↩", NSColor.systemGreenColor),
    "later": ("clock.fill", "◷", NSColor.systemPurpleColor),
    "closed": ("moon.zzz.fill", "☾", NSColor.tertiaryLabelColor),
    "followup": ("diamond.fill", "◆", NSColor.systemPurpleColor),
    "routine": ("exclamationmark.triangle.fill", "⚠", NSColor.systemRedColor),
    "new": ("plus", "+", NSColor.systemBlueColor),
}


def _shown(counts: dict) -> list[str]:
    return [k for k in SEGMENTS if counts.get(k) or k == "inbox"]


def _symbol(symbol: str, color, font):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
    if img is None:
        return None
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(font.pointSize(), NSFontWeightSemibold)
    cfg = cfg.configurationByApplyingConfiguration_(
        NSImageSymbolConfiguration.configurationWithPaletteColors_([color]))
    return img.imageWithSymbolConfiguration_(cfg)


def _icon(symbol: str, glyph: str, color, font) -> NSMutableAttributedString:
    out = NSMutableAttributedString.alloc().init()
    img = _symbol(symbol, color, font)
    if img is None:
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            glyph, {NSFontAttributeName: font, NSForegroundColorAttributeName: color}))
    else:
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
    return " · ".join(SEGMENTS[k][3].format(n=counts.get(k, 0)) for k in _shown(counts))


def _bundled() -> bool:
    return NSBundle.mainBundle().bundleIdentifier() == BUNDLE_ID


def _notify(title: str, body: str, target: str | None = None) -> None:
    """A click on it goes to target (a board item id), or opens the board."""
    if not _bundled():
        # Run outside Nizam.app, a notification's click can only ever open Script Editor.
        def q(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e",
                          f'display notification "{q(body)}" with title "{q(title)}" sound name "{NOTIFICATION_SOUND}"'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    content = UNMutableNotificationContent.alloc().init()
    content.setTitle_(title)
    content.setBody_(body)
    content.setSound_(UNNotificationSound.soundNamed_(f"{NOTIFICATION_SOUND}.aiff"))
    content.setUserInfo_({"target": target or ""})
    req = UNNotificationRequest.requestWithIdentifier_content_trigger_(target or str(uuid.uuid4()), content, None)
    UNUserNotificationCenter.currentNotificationCenter().addNotificationRequest_withCompletionHandler_(
        req, lambda _err: None)


class BoardVC(NSViewController, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    web = objc.ivar()
    port = objc.ivar()
    delegate = objc.ivar()

    def initWithPort_delegate_(self, port, delegate):
        self = objc.super(BoardVC, self).init()
        if self is None:
            return None
        self.port = port
        self.delegate = delegate
        return self

    def loadView(self):
        cfg = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(self, "nizam")
        cfg.setUserContentController_(ucc)
        self.web = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, *POPOVER_SIZE), cfg)
        self.web.setValue_forKey_(False, "drawsBackground")
        self.setView_(self.web)
        self.reload()

    def userContentController_didReceiveScriptMessage_(self, _ucc, message):
        body = message.body()
        if body is None:
            return
        # Re-hosting the web view from inside its own callback is asking for trouble.
        for key, sel in (("pin", "applyPinned:"), ("badge", "applyBadge:"), ("notifications", "applyNotifications:")):
            if key in body:
                self.delegate.performSelector_withObject_afterDelay_(sel, bool(body[key]), 0.0)
        if "update" in body:
            self.delegate.performSelector_withObject_afterDelay_("startUpdate:", None, 0.0)
        if "open" in body and str(body["open"]).startswith("https://"):
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(str(body["open"])))

    @objc.python_method
    def reload(self, select=None, settings=False, agent=None):
        url = (f"http://127.0.0.1:{self.port}/?embed=1" + (f"&select={quote(select, safe='')}" if select else "")
               + (f"&agent={quote(agent, safe='')}" if agent else "") + ("&settings=1" if settings else ""))
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

    @objc.python_method
    def show_settings(self):
        if self.web.isLoading():
            self.reload(settings=True)
        else:
            self.web.evaluateJavaScript_completionHandler_("openSettings()", None)

    @objc.python_method
    def select(self, item_id):
        if self.web.isLoading():
            self.reload(item_id)
        else:
            self.web.evaluateJavaScript_completionHandler_(f"nizamSelect({json.dumps(item_id)})", None)

    @objc.python_method
    def show_agent(self, root):
        if self.web.isLoading():
            self.reload(agent=root)
        else:
            self.web.evaluateJavaScript_completionHandler_(f"nizamAgent({json.dumps(root)})", None)

    @objc.python_method
    def refresh(self):
        if self.web is not None and not self.web.isLoading():
            self.web.evaluateJavaScript_completionHandler_("refresh(true)", None)

    @objc.python_method
    def shown(self):
        if self.web is not None and not self.web.isLoading():
            self.web.evaluateJavaScript_completionHandler_("typeof boardShown === 'function' && boardShown()", None)


def _track_hover(view):
    for t in list(view.trackingAreas()):
        view.removeTrackingArea_(t)
    view.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
        view.bounds(), NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved
        | NSTrackingActiveAlways | NSTrackingInVisibleRect, view, None))


class BadgeView(NSView):
    """A draggable capsule showing the counts. Click toggles the popover, or updates on the update arrow;
    hovering a count lists what is behind it."""
    delegate = objc.ivar()
    counts = objc.ivar()
    limits = objc.ivar()
    has_update = objc.ivar()
    _spans = objc.ivar()
    _spin = objc.ivar()
    _spin_timer = objc.ivar()
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
        self._spin = 0
        return self

    def acceptsFirstMouse_(self, event):
        return True   # otherwise the first click after another app only focuses us

    def updateTrackingAreas(self):
        objc.super(BadgeView, self).updateTrackingAreas()
        _track_hover(self)

    @objc.python_method
    def segments(self):
        font = NSFont.systemFontOfSize_weight_(12, NSFontWeightSemibold)
        out = [(k, _segment(k, self.counts.get(k, 0), font, True)) for k in _shown(self.counts)]
        if self.limits:
            out.append((LIMITS, _limits_segment(self.limits, font)))
        if self.has_update:
            symbol, glyph = ("arrow.triangle.2.circlepath", "↻") if self.delegate.updating else ("arrow.down.circle", "↓")
            out.append((UPDATE, _icon(symbol, glyph, NSColor.systemBlueColor(), font)))
        return out

    @objc.python_method
    def _span_at(self, event):
        x = self.convertPoint_fromView_(event.locationInWindow(), None).x
        for bucket, x0, x1 in self._spans:
            if x < x1:
                return bucket, x0
        return None, 0

    @objc.python_method
    def _hover(self, event):
        bucket, x0 = self._span_at(event)
        if bucket:
            self.delegate.hover(bucket, x0)

    @objc.python_method
    def start_spin(self):
        if self._spin_timer is None:
            self._spin_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                SPIN_SECS, self, "spin:", None, True)

    def spin_(self, _timer):
        if self.delegate.updating:
            self._spin = (self._spin + SPIN_STEP) % 360
        else:
            self._spin_timer.invalidate()
            self._spin_timer = None
            self._spin = 0
            self.delegate.resize_badge()
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _draw_spinning(self, x, width):
        img = _symbol("arrow.triangle.2.circlepath", NSColor.systemBlueColor(),
                      NSFont.systemFontOfSize_weight_(12, NSFontWeightSemibold))
        if img is None:
            return False
        sz = img.size()
        cx, cy = x + width / 2, self.bounds().size.height / 2
        NSGraphicsContext.saveGraphicsState()
        t = NSAffineTransform.transform()
        t.translateXBy_yBy_(cx, cy)
        t.rotateByDegrees_(-self._spin)
        t.concat()
        img.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(-sz.width / 2, -sz.height / 2, sz.width, sz.height),
            NSZeroRect, NSCompositingOperationSourceOver, 1.0)
        NSGraphicsContext.restoreGraphicsState()
        return True

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
            if not (k == UPDATE and self._spin_timer is not None and self._draw_spinning(x, sz.width)):
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
        elif self._span_at(event)[0] == UPDATE:
            self.delegate.startUpdate_(None)
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

    def rightMouseDown_(self, event):
        pass

    def rightMouseUp_(self, event):
        i = self._row_at(event)
        if i >= 0:
            self.delegate.list_star(self.rows[i])

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
            tx = 12
            if row.get("icon") in ROW_ICONS:
                symbol, glyph, color_fn = ROW_ICONS[row["icon"]]
                img = _symbol(symbol, color_fn(), NSFont.systemFontOfSize_(11))
                box = NSMakeRect(12, y + (LIST_ROW_H - LIST_ICON) / 2, LIST_ICON, LIST_ICON)
                if img is None:
                    text(glyph, 12, box.origin.y - 1, 11, 0.0, color_fn())
                else:
                    sz = img.size()
                    img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                        NSMakeRect(box.origin.x + (LIST_ICON - sz.width) / 2, box.origin.y + (LIST_ICON - sz.height) / 2,
                                   sz.width, sz.height), NSZeroRect, NSCompositingOperationSourceOver, 1.0, True, None)
                tx = 12 + LIST_ICON + 9
            text(row["title"], tx, y + 4, 12, 0.0, NSColor.labelColor())
            text(row["sub"], tx, y + 20, 10, 0.0, NSColor.secondaryLabelColor())


CHIP_STATE_COLORS = {"needs": NSColor.systemRedColor, "working": NSColor.systemOrangeColor,
                     "inbox": NSColor.systemGreenColor, "quiet": NSColor.tertiaryLabelColor}


def _hex_color(hex_str, alpha=1.0):
    r, g, b = (int(hex_str[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


def _draw_text(s, rect, size, weight, color):
    para = NSMutableParagraphStyle.alloc().init()
    para.setLineBreakMode_(NSLineBreakByTruncatingTail)
    NSAttributedString.alloc().initWithString_attributes_(s, {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(size, weight),
        NSForegroundColorAttributeName: color,
        NSParagraphStyleAttributeName: para,
    }).drawInRect_(rect)


def _draw_centered(s, cx, cy, size, weight, color):
    """Centres the capitals on (cx, cy), not the line box, which carries descender room below."""
    font = NSFont.systemFontOfSize_weight_(size, weight)
    a = NSAttributedString.alloc().initWithString_attributes_(s, {
        NSFontAttributeName: font, NSForegroundColorAttributeName: color})
    a.drawAtPoint_(NSMakePoint(cx - a.size().width / 2, cy - font.capHeight() / 2 + font.descender()))


class TeamChipView(NSView):
    """A full-width capsule, one segment per agent. Hover a segment for its sessions and follow-ups,
    click it for that agent's page on the board, drag it to another screen to fill that one."""
    delegate = objc.ivar()
    agents = objc.ivar()
    loaded = objc.ivar()
    hot = objc.ivar()
    _spans = objc.ivar()
    _down = objc.ivar()
    _dragged = objc.ivar()

    def initWithFrame_delegate_(self, frame, delegate):
        self = objc.super(TeamChipView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.delegate = delegate
        self.agents = []
        self.loaded = False
        self._spans = []
        self._dragged = False
        return self

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        objc.super(TeamChipView, self).updateTrackingAreas()
        _track_hover(self)

    @objc.python_method
    def _layout(self):
        agents = list(self.agents)
        if not agents:
            return []
        b = self.bounds()
        compact = sum(a["compact"] for a in agents)
        weights = sum(CHIP_NEEDS_WEIGHT if a["state"] == "needs" else 1 for a in agents if not a["compact"])
        free = b.size.width - 12 - compact * CHIP_COMPACT_W - CHIP_GAP * (len(agents) - 1)
        spans, x = [], 6.0
        for a in agents:
            w = CHIP_COMPACT_W if a["compact"] else free * (CHIP_NEEDS_WEIGHT if a["state"] == "needs" else 1) / weights
            spans.append((a, x, x + w))
            x += w + CHIP_GAP
        return spans

    @objc.python_method
    def _span_at(self, event):
        x = self.convertPoint_fromView_(event.locationInWindow(), None).x
        return next(((a, x0, x1) for a, x0, x1 in self._spans if x0 <= x <= x1 + CHIP_GAP), (None, 0, 0))

    def drawRect_(self, rect):
        b = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, b.size.height / 2, b.size.height / 2)
        NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.94).setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.setLineWidth_(1)
        path.stroke()
        self._spans = self._layout()
        if not self._spans:
            _draw_centered("All caught up" if self.loaded else "Loading…", b.size.width / 2, b.size.height / 2, 13, 0.0,
                           NSColor.secondaryLabelColor())
            return
        mid = b.size.height / 2
        for a, x0, x1 in self._spans:
            seg = NSMakeRect(x0, 4, x1 - x0, b.size.height - 8)
            pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(seg, seg.size.height / 2, seg.size.height / 2)
            if a["state"] == "needs":
                NSColor.systemRedColor().colorWithAlphaComponent_(0.14).setFill()
                pill.fill()
                NSColor.systemRedColor().colorWithAlphaComponent_(0.8).setStroke()
                pill.setLineWidth_(1)
                pill.stroke()
            elif a["root"] == self.hot:
                NSColor.labelColor().colorWithAlphaComponent_(0.08).setFill()
                pill.fill()
            av = NSMakeRect(x0 + 4, mid - CHIP_AV / 2, CHIP_AV, CHIP_AV)
            _hex_color(a["color"], 0.45 if a["compact"] else 1.0).setFill()
            NSBezierPath.bezierPathWithOvalInRect_(av).fill()
            _draw_centered(a["initials"], av.origin.x + CHIP_AV / 2, mid, 12, NSFontWeightSemibold,
                           NSColor.whiteColor().colorWithAlphaComponent_(0.75 if a["compact"] else 1.0))
            if a["compact"]:
                if a["due"]:
                    bubble = NSMakeRect(av.origin.x + CHIP_AV - 12, av.origin.y + CHIP_AV - 10, 16, 16)
                    NSColor.systemRedColor().setFill()
                    NSBezierPath.bezierPathWithOvalInRect_(bubble).fill()
                    _draw_centered(str(a["due"]), bubble.origin.x + 8, bubble.origin.y + 8, 10,
                                   NSFontWeightSemibold, NSColor.whiteColor())
                continue
            dot = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(av.origin.x + CHIP_AV - 10, av.origin.y - 1, 11, 11))
            CHIP_STATE_COLORS[a["state"]]().setFill()
            dot.fill()
            NSColor.windowBackgroundColor().setStroke()
            dot.setLineWidth_(2)
            dot.stroke()
            tx = av.origin.x + CHIP_AV + 9
            right = x1 - 12
            if a["due"]:
                _draw_centered(f"{a['due']} due", right - 22, mid, 10.5, NSFontWeightSemibold, NSColor.systemRedColor())
                right -= 50
            _draw_text(a["name"], NSMakeRect(tx, mid, right - tx, 18), 13, NSFontWeightSemibold, NSColor.labelColor())
            status_color = NSColor.secondaryLabelColor() if a["state"] == "quiet" else CHIP_STATE_COLORS[a["state"]]()
            _draw_text(a["status"], NSMakeRect(tx, mid - 16, right - tx, 15), 11, 0.0, status_color)

    @objc.python_method
    def _hover(self, event):
        a, x0, x1 = self._span_at(event)
        root = a["root"] if a else None
        if root != self.hot:
            self.hot = root
            self.setNeedsDisplay_(True)
        if a:
            self.delegate.chip_hover(root, (x0 + x1) / 2)

    def mouseEntered_(self, event):
        self._hover(event)

    def mouseMoved_(self, event):
        self._hover(event)

    def mouseExited_(self, event):
        self.hot = None
        self.setNeedsDisplay_(True)
        self.delegate.hover_left()

    def mouseDown_(self, event):
        self.delegate.hide_chip_list()
        self._down = event.locationInWindow()
        self._dragged = False

    def mouseDragged_(self, event):
        loc = event.locationInWindow()
        if not self._dragged and abs(loc.x - self._down.x) < 3 and abs(loc.y - self._down.y) < 3:
            return
        self._dragged = True
        p = NSEvent.mouseLocation()
        self.window().setFrameOrigin_(NSMakePoint(p.x - self._down.x, p.y - self._down.y))

    def mouseUp_(self, event):
        if self._dragged:
            self.delegate.chipMoved_(None)
            return
        a = self._span_at(event)[0]
        if a:
            self.delegate.chip_clicked(a["root"])

    def rightMouseDown_(self, event):
        self.delegate.hide_chip_list()
        self.delegate.showMenuAt_(self)


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


def make_list_panel(view_delegate, width=LIST_W):
    panel = _floating_panel(width, LIST_HEAD_H)
    view = HoverListView.alloc().initWithFrame_delegate_(NSMakeRect(0, 0, width, LIST_HEAD_H), view_delegate)
    panel.setContentView_(view)
    return panel, view


def make_chip_panel(view_delegate):
    panel = _floating_panel(600, CHIP_H)
    view = TeamChipView.alloc().initWithFrame_delegate_(NSMakeRect(0, 0, 600, CHIP_H), view_delegate)
    view.setAutoresizingMask_(2 | 16)   # width and height follow the panel
    panel.setContentView_(view)
    return panel, view


def _chip_frame(screen, y=None):
    """Spans the screen's width; keeps the height you dropped it at, or sits at the bottom."""
    vf = screen.visibleFrame()
    lo, hi = vf.origin.y + CHIP_MARGIN, vf.origin.y + vf.size.height - CHIP_H - CHIP_MARGIN
    y = lo if y is None else min(max(y, lo), hi)
    return NSMakeRect(vf.origin.x + CHIP_MARGIN, y, vf.size.width - 2 * CHIP_MARGIN, CHIP_H)


def _screen_at(point):
    return next((s for s in NSScreen.screens() if NSPointInRect(point, s.frame())), None)


def _default_chip_screen():
    """A portrait screen if there is one: that is where a strip across the bottom costs least."""
    tall = [s for s in NSScreen.screens() if s.frame().size.height > s.frame().size.width]
    return tall[0] if tall else NSScreen.mainScreen()


class AppDelegate(NSObject):
    status = objc.ivar()
    popover = objc.ivar()
    window = objc.ivar()
    vc = objc.ivar()
    board = objc.ivar()
    port = objc.ivar()
    snap = objc.ivar()
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
    chip = objc.ivar()
    chip_view = objc.ivar()
    chip_list_panel = objc.ivar()
    chip_list_view = objc.ivar()
    chip_root = objc.ivar()
    chip_x = objc.ivar()
    chip_cards = objc.ivar()

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
        self.vc = BoardVC.alloc().initWithPort_delegate_(self.port, self)
        self.popover.setContentViewController_(self.vc)
        self.popover.setDelegate_(self)
        self.badge, self.badge_view = make_badge_panel(self)
        self.list_panel, self.list_view = make_list_panel(self)
        self.chip, self.chip_view = make_chip_panel(self)
        self.chip_list_panel, self.chip_list_view = make_list_panel(self, CHIP_LIST_W)
        self._restore_badge()
        self._restore_chip()
        if self.board.persist.data["prefs"].get("badge", True):
            self.badge.orderFrontRegardless()
        if self.board.persist.data["prefs"].get("chip"):
            self.chip.orderFrontRegardless()
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "screensChanged:", "NSApplicationDidChangeScreenParametersNotification", None)
        if _bundled():
            UNUserNotificationCenter.currentNotificationCenter().setDelegate_(self)
        NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self, "spaceChanged:", "NSWorkspaceActiveSpaceDidChangeNotification", None)
        NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self, "didWake:", "NSWorkspaceDidWakeNotification", None)
        threading.Thread(target=self._build_loop, daemon=True).start()

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
        edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Close Window", "performClose:", "w"))
        edit_item.setSubmenu_(edit)
        NSApp.setMainMenu_(menubar)

    @objc.python_method
    def _build_loop(self):
        """Off the main thread: a cold build takes seconds and would freeze the menu bar and badge."""
        while True:
            if QUIT_FLAG.exists():
                QUIT_FLAG.unlink(missing_ok=True)
                self.performSelectorOnMainThread_withObject_waitUntilDone_("quit:", None, False)
                return
            try:
                self.snap = self.board.snapshot()
            except Exception as e:  # keep the loop alive no matter what
                sys.stderr.write(f"refresh failed: {e}\n")
            else:
                self.performSelectorOnMainThread_withObject_waitUntilDone_("refresh:", None, False)
            self._poll_usage()
            time.sleep(REFRESH_SECS)

    @objc.python_method
    def _poll_usage(self):
        now = time.time()
        for p in providers.active():
            if not p.claim_usage_poll(now):
                continue
            env = providers.clean_env()
            env["PATH"] = login_path()
            exe = shutil.which(p.cli, path=env["PATH"])
            if exe:
                threading.Thread(target=p.poll_usage, args=(exe, env), daemon=True).start()

    def refresh_(self, _):
        snap = self.snap
        if snap is None:
            return
        items = {k: [] for k in SEGMENTS}
        needs_now = needs_you(snap)
        agent_names = {a["root"]: a["display_name"] for a in snap["agents"]}
        tri = triage.get()
        view = tri.view(snap) if tri else {}
        cards = {i["id"]: i["card"] for i in view.get("items", []) if i["card"]}
        for s in snap["sessions"]:
            if s["bucket"] in items:
                c = cards.get(s["id"])
                title = c["headline"] if c else s["title"]
                items[s["bucket"]].append({"id": s["id"], "title": f"★ {title}" if s["starred"] else title, "session": s,
                                           "sub": f"{s['agent_name']} · {c['needs']}" if c
                                           else f"{s['agent_name']} · {s['label']} · {s['ago']}"})
        if view.get("enabled"):
            rank = {i["id"]: n for n, i in enumerate(view["items"])}
            for b in ("needs", "inbox"):
                items[b].sort(key=lambda r: rank.get(r["id"], len(rank)))
        for b in ("needs", "inbox"):
            items[b].sort(key=lambda r: not r["session"]["starred"])
        items["star"] = [r for b in ("needs", "inbox") for r in items[b] if r["session"]["starred"]]
        for f in snap.get("followups", []):
            if f["due"] != "upcoming" and not f.get("in_session"):
                when = f["when"] if f["due"] == "today" else f"{-f['days']}d overdue"
                items["due"].append({"id": f["id"], "title": f["title"],
                                     "sub": f"{agent_names.get(f['agent'], '')} · {when}"})
        for r in snap.get("routines", []):
            if r["failing"] or r["attention"]:
                why = "Routine " + r["status"].replace("_", " ") if r["failing"] else "Needs attention"
                items["needs"].append({"id": r["id"], "title": r["name"],
                                       "sub": f"{agent_names.get(r['agent'], '')} · {why}"})
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
        self.resize_badge()
        self.chip_cards = cards
        self.chip_view.agents = chip.agents(snap)
        self.chip_view.loaded = True
        self.chip_view.setNeedsDisplay_(True)
        self._sync_chip_list()
        new_ids = set(needs_now) - self.seen_needs
        if self.seeded and new_ids and self._notifications_on():
            if len(new_ids) == 1:
                s = needs_now[next(iter(new_ids))]
                _notify(f"{s['agent_name']} needs you", f"{s['label']} · {s['title']}", s["target"])
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
            if self._notifications_on():
                _notify("Nizam", f"{latest} is available. Click ↓ on the badge to update.")

    @objc.python_method
    def resize_badge(self):
        w = self.badge_view.desired_width()
        self.badge.setContentSize_(NSSize(w, BADGE_H))
        self.badge_view.setFrameSize_(NSSize(w, BADGE_H))
        self.badge_view.setNeedsDisplay_(True)

    @objc.python_method
    def _set_title(self, c):
        font = NSFont.menuBarFontOfSize_(0)
        title = NSMutableAttributedString.alloc().init()
        for i, k in enumerate(_shown(c)):
            if i:
                title.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_("   ", {NSFontAttributeName: font}))
            title.appendAttributedString_(_segment(k, c.get(k, 0), font, False))
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

    def spaceChanged_(self, _note):
        # An all-Spaces panel keeps its hover tracking only on the Space it was ordered in.
        self.hide_list()
        self.hide_chip_list()
        for panel, view in ((self.badge, self.badge_view), (self.chip, self.chip_view)):
            if panel.isVisible():
                panel.orderFrontRegardless()
                _track_hover(view)

    @objc.python_method
    def _restore_chip(self):
        pos = self.board.persist.data["prefs"].get("chip_pos")
        screen = pos and _screen_at(NSMakePoint(pos[0] + CHIP_H, pos[1] + CHIP_H / 2))
        self.chip.setFrame_display_(_chip_frame(screen, pos[1]) if screen else _chip_frame(_default_chip_screen()), True)

    def screensChanged_(self, _note):
        self.hide_chip_list()
        self._restore_chip()

    def chipMoved_(self, _):
        screen = _screen_at(NSEvent.mouseLocation()) or self.chip.screen() or NSScreen.mainScreen()
        self.chip.setFrame_display_(_chip_frame(screen, self.chip.frame().origin.y), True)
        o = self.chip.frame().origin
        self.board.persist.set_pref("chip_pos", [o.x, o.y])

    @objc.python_method
    def chip_hover(self, root, x):
        if self.popover.isShown():
            return
        if root != self.chip_root:
            self.chip_root, self.chip_x = root, x
            self._sync_chip_list()

    @objc.python_method
    def hide_chip_list(self):
        self.chip_root = None
        self.chip.removeChildWindow_(self.chip_list_panel)
        self.chip_list_panel.orderOut_(None)

    @objc.python_method
    def _sync_chip_list(self):
        a = next((a for a in self.chip_view.agents if a["root"] == self.chip_root), None) if self.snap else None
        rows = chip.rows(self.snap, self.chip_root, self.chip_cards) if a else []
        if a:
            rows = [{"id": None, "new": a["root"], "icon": "new", "title": "New session", "sub": f"Start Claude in {a['name']}"}] + rows
        if not rows:
            self.chip.removeChildWindow_(self.chip_list_panel)
            self.chip_list_panel.orderOut_(None)
            return
        v = self.chip_list_view
        v.bucket, v.head, v.rows = CHIP, a["name"], [{**r, CHIP: True} for r in rows]
        h = v.desired_height()
        frame = self.chip.frame()
        screen = (self.chip.screen() or NSScreen.mainScreen()).visibleFrame()
        x = min(max(frame.origin.x + self.chip_x - CHIP_LIST_W / 2, screen.origin.x),
                screen.origin.x + screen.size.width - CHIP_LIST_W)
        y = frame.origin.y + CHIP_H + 4
        if y + h > screen.origin.y + screen.size.height:
            y = frame.origin.y - 4 - h
        self.chip_list_panel.setFrame_display_(NSMakeRect(x, y, CHIP_LIST_W, h), True)
        v.setNeedsDisplay_(True)
        if self.chip_list_panel.parentWindow() is None:
            self.chip.addChildWindow_ordered_(self.chip_list_panel, NSWindowAbove)

    @objc.python_method
    def chip_clicked(self, root):
        self.hide_chip_list()
        self._show_board(self.chip_view)
        self.vc.show_agent(root)

    def didWake_(self, _note):
        telegram.woke()

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
        if not (NSPointInRect(p, self.badge.frame()) or (self.list_panel.isVisible() and NSPointInRect(p, self.list_panel.frame()))):
            self.hide_list()
        if not (NSPointInRect(p, self.chip.frame()) or (self.chip_list_panel.isVisible() and NSPointInRect(p, self.chip_list_panel.frame()))):
            self.hide_chip_list()

    @objc.python_method
    def hide_list(self):
        self.hover_bucket = None
        self._close_list()

    @objc.python_method
    def _close_list(self):
        self.badge.removeChildWindow_(self.list_panel)
        self.list_panel.orderOut_(None)

    @objc.python_method
    def _sync_list(self):
        rows = self.items.get(self.hover_bucket) if self.hover_bucket else None
        if not rows:
            self._close_list()
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
        # On its own the list can stick to one Space; as a child it follows the badge.
        if self.list_panel.parentWindow() is None:
            self.badge.addChildWindow_ordered_(self.list_panel, NSWindowAbove)

    @objc.python_method
    def list_picked(self, row):
        if row.get("update"):
            self.hide_list()
            self.startUpdate_(None)
            return
        if row.get("inert"):
            return
        self.hide_list()
        self.hide_chip_list()
        if row.get("new"):
            self._start_session(row["new"])
            return
        if row.get("session"):
            self._open_session(row["session"])
            return
        self._show_board(self.chip_view if row.get(CHIP) else self.badge_view)
        if row["id"]:
            self.vc.select(row["id"])

    @objc.python_method
    def list_star(self, row):
        s = row.get("session")
        if not s:
            return
        self.board.persist.star(s["id"], not s["starred"])
        self.board.invalidate()
        self.hide_list()
        self.hide_chip_list()

    @objc.python_method
    def _open_session(self, s):
        prefs = self.board.persist.data["prefs"]
        threading.Thread(target=terminal.open_session, daemon=True,
                         args=(s, terminal.terminal_for(prefs), False, terminal.remote_for(prefs))).start()

    @objc.python_method
    def _start_session(self, cwd):
        prefs = self.board.persist.data["prefs"]
        mode = (prefs.get("last_start") or {}).get("permission_mode") or "acceptEdits"
        self.board.persist.set_pref("last_start", {"cwd": cwd, "permission_mode": mode, "at": time.time()})
        threading.Thread(target=terminal.start, daemon=True, kwargs={
            "cwd": cwd, "permission_mode": mode, "launcher": prefs.get("launcher", "Terminal"),
            "remote": terminal.remote_for(prefs)}).start()

    @objc.python_method
    def _notifications_on(self):
        return bool(self.board.persist.data["prefs"].get("notifications"))

    def toggleNotifications_(self, _):
        if self._notifications_on():
            self.board.persist.set_pref("notifications", False)
        elif not _bundled():
            self.board.persist.set_pref("notifications", True)
        else:
            def answered(granted, _err):
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "notificationsAnswered:", bool(granted), False)
            UNUserNotificationCenter.currentNotificationCenter().requestAuthorizationWithOptions_completionHandler_(
                UNAuthorizationOptionAlert | UNAuthorizationOptionSound, answered)

    def notificationsAnswered_(self, granted):
        if granted:
            self.board.persist.set_pref("notifications", True)
            _notify("Nizam", "Notifications are on. Click one to go straight to what needs you.")
        else:
            # Once declined, macOS never asks again; only System Settings can allow it.
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(NOTIFICATION_SETTINGS))
        self.vc.refresh()

    def applyNotifications_(self, on):
        if bool(on) != self._notifications_on():
            self.toggleNotifications_(None)
        self.vc.refresh()

    def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, _center, _note, done):
        # While the board is open Nizam is the frontmost app, and macOS would otherwise hold the banner back.
        done(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList
             | UNNotificationPresentationOptionSound)

    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(self, _center, response, done):
        target = response.notification().request().content().userInfo().get("target")
        done()
        self.performSelectorOnMainThread_withObject_waitUntilDone_("notificationClicked:", target or None, False)

    def notificationClicked_(self, target):
        if target:
            s = next((s for s in (self.snap or {}).get("sessions", []) if s["id"] == target), None)
            if s:
                self._open_session(s)
                return
        badge_on = self.board.persist.data["prefs"].get("badge", True)
        self._show_board(self.badge_view if badge_on else self.status.button())
        if target:
            self.vc.select(target)

    def badgeClicked_(self, view):
        self.hide_list()
        self._toggle_board(view)

    @objc.python_method
    def _pinned(self):
        return bool(self.board.persist.data["prefs"].get("board_pinned"))

    @objc.python_method
    def _board_open(self):
        return self.popover.isShown() or (self.window is not None and self.window.isVisible())

    @objc.python_method
    def _toggle_board(self, anchor):
        if self.popover.isShown():
            self.popover.close()
        elif self._pinned() and self._board_open() and self.window.isKeyWindow():
            self.window.orderOut_(None)
        else:
            self._show_board(anchor)

    @objc.python_method
    def _show_board(self, anchor):
        self.vc.view()   # force loadView before the first show
        self._host(self._pinned())
        if self._pinned():
            self.window.makeKeyAndOrderFront_(None)
        else:
            self.popover.showRelativeToRect_ofView_preferredEdge_(anchor.bounds(), anchor, 1)
            if anchor is self.badge_view:
                self._install_monitor()
        NSApp.activateIgnoringOtherApps_(True)
        self.vc.shown()

    @objc.python_method
    def _host(self, pinned):
        """The board lives in one place at a time: the popover, or a regular window when pinned."""
        if self.window is None:
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, *POPOVER_SIZE),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
                NSBackingStoreBuffered, False)
            self.window.setTitle_("Nizam")
            self.window.setReleasedWhenClosed_(False)
            self.window.setContentMinSize_(NSSize(*BOARD_MIN_SIZE))
        if pinned and self.window.contentViewController() is None:
            self.popover.setAnimates_(False)
            self.popover.close()
            self.popover.setAnimates_(True)
            self.popover.setContentViewController_(None)
            self.window.setContentViewController_(self.vc)
            if not self.window.setFrameUsingName_(BOARD_FRAME):
                self.window.center()
            self.window.setFrameAutosaveName_(BOARD_FRAME)
        elif not pinned and self.popover.contentViewController() is None:
            self.window.orderOut_(None)
            self.window.setContentViewController_(None)
            self.popover.setContentViewController_(self.vc)
            self.popover.setContentSize_(NSSize(*POPOVER_SIZE))

    @objc.python_method
    def set_pinned(self, pinned):
        was_open = self._board_open()
        self.board.persist.set_pref("board_pinned", pinned)
        self.board.invalidate()
        self._host(pinned)
        if was_open:
            badge_on = self.board.persist.data["prefs"].get("badge", True)
            self._show_board(self.badge_view if badge_on else self.status.button())
        self.vc.refresh()

    def applyPinned_(self, pinned):
        self.set_pinned(bool(pinned))

    def togglePinned_(self, _):
        self.set_pinned(not self._pinned())

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
        self.applyBadge_(not self.board.persist.data["prefs"].get("badge", True))

    def applyBadge_(self, on):
        self.board.persist.set_pref("badge", bool(on))
        if on:
            self.badge.orderFrontRegardless()
        else:
            self.hide_list()
            self.badge.orderOut_(None)
        self.vc.refresh()

    def toggleChip_(self, _):
        on = not self.board.persist.data["prefs"].get("chip")
        self.board.persist.set_pref("chip", on)
        if on:
            self._restore_chip()
            self.chip.orderFrontRegardless()
        else:
            self.hide_chip_list()
            self.chip.orderOut_(None)

    def statusClicked_(self, sender):
        ev = NSApp.currentEvent()
        if ev is not None and ev.type() == 3:   # right mouse down
            self._show_menu()
            return
        self.togglePopover_(sender)

    def togglePopover_(self, sender):
        self._toggle_board(self.status.button())

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
        team = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Team chip", "toggleChip:", "")
        team.setState_(1 if self.board.persist.data["prefs"].get("chip") else 0)
        menu.addItem_(team)
        pinned = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Keep board open", "togglePinned:", "")
        pinned.setState_(1 if self._pinned() else 0)
        menu.addItem_(pinned)
        notifications = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Notifications", "toggleNotifications:", "")
        notifications.setState_(1 if self._notifications_on() else 0)
        menu.addItem_(notifications)
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Settings…", "openSettings:", ","))
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

    def openSettings_(self, _):
        badge_on = self.board.persist.data["prefs"].get("badge", True)
        self._show_board(self.badge_view if badge_on else self.status.button())
        self.vc.show_settings()

    def reloadBoard_(self, _):
        self.vc.view()
        self.vc.reload()

    def toggleLogin_(self, _):
        set_login(not login_enabled())

    def startUpdate_(self, _):
        if self.updating or not self.update_info:
            return
        self.updating = True
        self.resize_badge()
        self.badge_view.start_spin()
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
                _notify("Nizam", f"{info['latest']} is available. Click ↓ on the badge to update.")
            else:
                _notify("Nizam", f"{info['current']} is up to date")
        threading.Thread(target=work, daemon=True).start()

    def quit_(self, _):
        PID_FILE.unlink(missing_ok=True)
        NSApp.terminate_(None)


class _Stamped:
    """app.log is plain stderr from many threads; stamp each line with when it was written."""

    def __init__(self, out):
        self.out, self.lock, self.fresh = out, threading.Lock(), True

    def write(self, s: str) -> int:
        with self.lock:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S ")
            parts = []
            for line in s.splitlines(keepends=True):
                parts.append(stamp + line if self.fresh else line)
                self.fresh = line.endswith("\n")
            self.out.write("".join(parts))
            self.out.flush()
        return len(s)

    def flush(self):
        self.out.flush()

    def __getattr__(self, name):
        return getattr(self.out, name)


def run(port: int) -> int:
    sys.stdout = sys.stderr = _Stamped(sys.stderr)
    ensure_dirs()
    if app_running():
        sys.stderr.write("Nizam is already running in the menu bar.\n")
        return 0   # not a failure, or launchd would keep starting another
    PID_FILE.write_text(str(os.getpid()))
    board = Board()
    httpd = make_server(board, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    telegram.start(board)
    triage.start(board)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().initWithBoard_port_(board, port)
    app.setDelegate_(delegate)
    app.run()
    return 0
