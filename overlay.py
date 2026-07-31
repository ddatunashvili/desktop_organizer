"""Zone overlay (PySide6): always visible on the desktop, editable in place.

A frameless per-pixel-alpha window covers the virtual screen, pinned just
above the desktop (HWND_BOTTOM) so app windows cover it. WM_NCHITTEST is
answered per point: interactive elements (label pill, gear, borders, grips)
return HTCLIENT, everything else HTTRANSPARENT — so the desktop stays fully
usable while zones remain editable any time:

  drag label pill     -> move zone (its kept icons travel with it)
  gear button         -> settings menu
  drag border / grip  -> resize from that side (Figma-style snapping)

Draw mode (unlock) dims the screen for drawing brand-new zones.
"""

import ctypes
import time
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QColorDialog, QDialog, QHBoxLayout,
                               QInputDialog, QMenu, QPushButton, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

import config

user32 = ctypes.WinDLL("user32", use_last_error=True)

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

HWND_TOPMOST = -1
HWND_BOTTOM = 1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

EDIT_BG = QColor(14, 18, 24, 165)
GUIDE_COLOR = QColor("#FF4081")
HANDLE = 14
GRIP = 10
EDGE = 6
MIN_SIZE = 60
SNAP = 8
GAP = 80      # ~one desktop icon cell: minimum distance between zones
PIN_MS = 2000
HOVER_MS = 60

if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_long, _set_long = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
else:
    _get_long, _set_long = user32.GetWindowLongW, user32.SetWindowLongW


def virtual_screen():
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


class ZoneOverlay(QWidget):
    def __init__(self, zones, hooks, ui_scale=1.0):
        """zones: the config's live zone list (shared object, not copied).
        hooks: changed() / capture_icons(zone) / zone_moved(zone, dx, dy),
        plus optional 'custom_colors' list and 'font_family' string."""
        super().__init__()
        self.zones = zones
        self.hooks = hooks
        self.s = ui_scale
        self.locked = True
        self.busy = False

        self.vx, self.vy, self.vw, self.vh = virtual_screen()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool |
                            Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(self.vx, self.vy, self.vw, self.vh)
        self.setMouseTracking(True)

        family = hooks.get("font_family") or "Segoe UI"
        self.label_font = QFont(family)
        self.label_font.setPixelSize(round(13 * self.s))
        self.label_font.setBold(True)
        self.hint_font = QFont("Segoe UI")
        self.hint_font.setPixelSize(round(15 * self.s))
        self.hint_font.setBold(True)

        # interaction state
        self.mode = None          # "move" | "resize" | "new"
        self.target = None
        self.start = (0, 0)
        self.orig = None
        self.resize_edges = ()
        self.guides = []
        self.hover_zone = None

        self._click_through = None  # tracked WS_EX_TRANSPARENT state

        self.pin_timer = QTimer(self, interval=PIN_MS, timeout=self._pin_bottom)
        self.pin_timer.start()
        self.hover_timer = QTimer(self, interval=HOVER_MS, timeout=self._hover_poll)
        self.hover_timer.start()

        self.show()
        self._apply_base_styles(click_through=True)
        self._pin_bottom()

    # ---------- OS-level input pass-through ----------
    def _apply_base_styles(self, click_through):
        """(Re)apply ex-styles — needed after every setWindowFlag() call,
        which recreates the native window."""
        hwnd = int(self.winId())
        ex = _get_long(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self._click_through = None  # force re-apply below
        _set_long(hwnd, GWL_EXSTYLE, ex)
        self._set_click_through(click_through)

    def _set_click_through(self, enabled):
        """Qt6 translucent windows swallow input even on transparent pixels,
        so click-through is done by toggling WS_EX_TRANSPARENT: on when the
        cursor is over nothing interactive, off when over a label/gear/border."""
        if enabled == self._click_through:
            return
        self._click_through = enabled
        hwnd = int(self.winId())
        ex = _get_long(hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if enabled else (ex & ~WS_EX_TRANSPARENT)
        _set_long(hwnd, GWL_EXSTYLE, ex)

    # ---------- z-order ----------
    def _pin_bottom(self):
        if self.locked:
            user32.SetWindowPos(int(self.winId()), HWND_BOTTOM, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    # ---------- lock / unlock ----------
    def unlock(self):
        if not self.locked:
            return
        self.locked = False
        self.busy = True
        self.hover_zone = None
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.show()
        self._apply_base_styles(click_through=False)
        self.activateWindow()
        self.setFocus()
        self.update()

    def lock(self):
        if self.locked:
            return
        self.locked = True
        self.busy = False
        self.zones[:] = [z for z in self.zones
                         if z["w"] >= MIN_SIZE and z["h"] >= MIN_SIZE]
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        self.show()
        self._apply_base_styles(click_through=True)
        self._pin_bottom()
        self.update()
        self.hooks["changed"]()

    # ---------- geometry helpers ----------
    def _z_rect(self, z):
        return QRect(z["x"] - self.vx, z["y"] - self.vy, z["w"], z["h"])

    def _effective_rect(self, z):
        """Display rect: if kept icons overflow the zone, the border stretches
        outward to hug them — the zone's stored size never changes."""
        r = self._z_rect(z)
        if z is self.target and self.mode == "move":
            return r  # while dragging, the outline keeps its true size
        if not (self.locked and z.get("pin") and z.get("icons")):
            return r
        ICON_W, ICON_H, PAD = 80, 92, 8
        x1, y1, x2, y2 = r.left(), r.top(), r.right(), r.bottom()
        for pos in z["icons"].values():
            ix, iy = pos[0] - self.vx, pos[1] - self.vy
            x1 = min(x1, ix - PAD)
            y1 = min(y1, iy - PAD)
            x2 = max(x2, ix + ICON_W + PAD)
            y2 = max(y2, iy + ICON_H + PAD)
        return QRect(x1, y1, x2 - x1, y2 - y1)

    def _label_geom(self, z):
        """(pill_rect, gear_rect, text) for a zone's title tag above the zone."""
        r = self._effective_rect(z)
        name = z.get("name", "") or "Zone"
        icon = z.get("icon", "")
        pin_mark = " ●" if z.get("pin") else ""
        text = (icon + "  " if icon else "") + name + pin_mark
        fm = QFontMetrics(self.label_font)
        tw = fm.horizontalAdvance(text) + round(18 * self.s)
        th = fm.height() + round(8 * self.s)
        ty2 = r.top() - 3
        ty1 = ty2 - th
        if ty1 < 2:  # zone touches screen top -> tag goes just inside
            ty1, ty2 = r.top() + 3, r.top() + 3 + th
        pill = QRect(r.left(), ty1, tw, th)
        gear = QRect(r.left() + tw + 4, ty1, th, th)
        return pill, gear, text

    def _edge_at(self, z, x, y):
        """Resize edges under (x, y) for zone z, or ()."""
        r = self._effective_rect(z)
        if not z.get("resizable", True):
            return ()
        if r.right() - HANDLE <= x <= r.right() + 4 and r.bottom() - HANDLE <= y <= r.bottom() + 4:
            return ("r", "b")
        if not (r.left() - EDGE <= x <= r.right() + EDGE
                and r.top() - EDGE <= y <= r.bottom() + EDGE):
            return ()
        edges = []
        if abs(x - r.left()) <= EDGE:
            edges.append("l")
        elif abs(x - r.right()) <= EDGE:
            edges.append("r")
        if abs(y - r.top()) <= EDGE:
            edges.append("t")
        elif abs(y - r.bottom()) <= EDGE:
            edges.append("b")
        return tuple(edges)

    def _element_at(self, x, y):
        """Locked-mode hit test -> (zone, kind, edges)."""
        if self.hover_zone and self.hover_zone in self.zones:
            pill, gear, _ = self._label_geom(self.hover_zone)
            if gear.contains(x, y):
                return self.hover_zone, "gear", ()
            if pill.contains(x, y):
                return self.hover_zone, "label", ()
        for z in reversed(self.zones):
            edges = self._edge_at(z, x, y)
            if edges:
                return z, "edge", edges
        return None, None, ()

    def _drag_mode(self):
        f = self.hooks.get("drag_mode")
        return bool(f and f())

    def _begin_icon_drag(self, zone):
        self._last_icon_emit = 0.0
        self.place_ok = True
        self._icon_ok = True
        f = self.hooks.get("drag_start")
        if f:
            f(zone)

    def _area_free(self, z):
        f = self.hooks.get("area_free")
        return f(z) if f else True

    def _zone_under(self, x, y):
        # the visible label/gear of the hovered zone counts as "inside" —
        # otherwise moving onto the tag (drawn above the zone) hides it
        if self.hover_zone and self.hover_zone in self.zones:
            pill, gear, _ = self._label_geom(self.hover_zone)
            hot = pill.united(gear).adjusted(-6, -6, 6, 6)
            if hot.contains(x, y):
                return self.hover_zone
        for z in reversed(self.zones):
            r = self._effective_rect(z).adjusted(-EDGE, -EDGE, EDGE, EDGE)
            if r.contains(x, y):
                return z
        return None

    # ---------- hover + input toggle ----------
    def _hover_poll(self):
        if not self.locked or self.busy or self.mode:
            return
        p = QCursor.pos()
        x, y = p.x() - self.vx, p.y() - self.vy
        z = self._zone_under(x, y)
        if z is not self.hover_zone:
            self.hover_zone = z
            self.update()
        zone, _kind, _ = self._element_at(x, y)
        self._set_click_through(zone is None)

    # ---------- snapping ----------
    def _snap_targets(self, exclude):
        xs = [self.vx, self.vx + self.vw, self.vx + self.vw // 2]
        ys = [self.vy, self.vy + self.vh, self.vy + self.vh // 2]
        for z in self.zones:
            if z is exclude:
                continue
            xs += [z["x"], z["x"] + z["w"], z["x"] + z["w"] // 2]
            ys += [z["y"], z["y"] + z["h"], z["y"] + z["h"] // 2]
        return xs, ys

    @staticmethod
    def _best_snap(values, targets):
        best = None
        for v in values:
            for t in targets:
                d = t - v
                if abs(d) <= SNAP and (best is None or abs(d) < abs(best[0])):
                    best = (d, t)
        return best

    def _apply_snap(self, z):
        self.guides = []
        xs, ys = self._snap_targets(z)
        if self.mode == "move":
            sx = self._best_snap([z["x"], z["x"] + z["w"], z["x"] + z["w"] // 2], xs)
            sy = self._best_snap([z["y"], z["y"] + z["h"], z["y"] + z["h"] // 2], ys)
            if sx:
                z["x"] += sx[0]
                self.guides.append(("v", sx[1]))
            if sy:
                z["y"] += sy[0]
                self.guides.append(("h", sy[1]))
            return
        edges = self.resize_edges if self.mode == "resize" else ("r", "b")
        if "l" in edges:
            s = self._best_snap([z["x"]], xs)
            if s and z["w"] - s[0] >= MIN_SIZE:
                z["x"] += s[0]
                z["w"] -= s[0]
                self.guides.append(("v", s[1]))
        if "r" in edges:
            s = self._best_snap([z["x"] + z["w"]], xs)
            if s and z["w"] + s[0] >= MIN_SIZE:
                z["w"] += s[0]
                self.guides.append(("v", s[1]))
        if "t" in edges:
            s = self._best_snap([z["y"]], ys)
            if s and z["h"] - s[0] >= MIN_SIZE:
                z["y"] += s[0]
                z["h"] -= s[0]
                self.guides.append(("h", s[1]))
        if "b" in edges:
            s = self._best_snap([z["y"] + z["h"]], ys)
            if s and z["h"] + s[0] >= MIN_SIZE:
                z["h"] += s[0]
                self.guides.append(("h", s[1]))

    # ---------- collision: zones never overlap, one icon-cell gap ----------
    def _collides(self, z):
        a = QRect(z["x"] - GAP, z["y"] - GAP, z["w"] + 2 * GAP, z["h"] + 2 * GAP)
        for o in self.zones:
            if o is z:
                continue
            if a.intersects(QRect(o["x"], o["y"], o["w"], o["h"])):
                return True
        return False

    def _mark_valid(self, z):
        self._valid = (z["x"], z["y"], z["w"], z["h"])

    def _keep_valid(self, z):
        """If the dragged geometry collides, fall back to the last valid one
        (sliding along one axis when possible) — the zone and its icons wait
        until the place is actually free."""
        if not self._collides(z):
            self._mark_valid(z)
            return
        vx_, vy_, vw_, vh_ = self._valid
        if self.mode == "move":
            cx, cy = z["x"], z["y"]
            z["x"], z["y"] = cx, vy_  # slide horizontally only
            if not self._collides(z):
                self._mark_valid(z)
                return
            z["x"], z["y"] = vx_, cy  # slide vertically only
            if not self._collides(z):
                self._mark_valid(z)
                return
        z["x"], z["y"], z["w"], z["h"] = vx_, vy_, vw_, vh_

    # ---------- mouse ----------
    def _start_resize(self, zone, edges):
        self.mode = "resize"
        self.target = zone
        self.resize_edges = edges
        self.orig = (zone["x"], zone["y"], zone["w"], zone["h"])
        self._mark_valid(zone)

    def mousePressEvent(self, e):
        x, y = int(e.position().x()), int(e.position().y())
        self.start = (x, y)
        if e.button() == Qt.RightButton:
            if self.locked:
                zone, kind, _ = self._element_at(x, y)
                if zone:
                    self._menu(zone)
            else:
                z, _ = self._hit_unlocked(x, y)
                if z:
                    self.zones.remove(z)
                    self.update()
            return
        if e.button() != Qt.LeftButton:
            return
        if self.locked:
            zone, kind, edges = self._element_at(x, y)
            if not zone:
                return
            self.busy = True
            if kind == "gear":
                self._menu(zone)
            elif kind == "label":
                # size-locked zones still move when dragging mode is on
                if zone.get("resizable", True) or self._drag_mode():
                    self.mode, self.target = "move", zone
                    self.orig = (zone["x"], zone["y"])
                    self._mark_valid(zone)
                    self._begin_icon_drag(zone)
                else:
                    self.busy = False
            elif kind == "edge":
                self._start_resize(zone, edges)
            return
        # draw mode
        z, edges = self._hit_unlocked(x, y)
        if z and edges:
            self._start_resize(z, edges)
        elif z and (z.get("resizable", True) or self._drag_mode()):
            self.mode, self.target = "move", z
            self.orig = (z["x"], z["y"])
            self._mark_valid(z)
            self._begin_icon_drag(z)
        elif z:
            pass  # locked-in-place zone
        else:
            self.mode = "new"
            self.target = {"name": "", "x": x + self.vx, "y": y + self.vy,
                           "w": 0, "h": 0, "color": config.next_zone_color(self.zones)}
            self.zones.append(self.target)
            self._mark_valid(self.target)

    def _hit_unlocked(self, x, y):
        for z in reversed(self.zones):
            edges = self._edge_at(z, x, y)
            if edges:
                return z, edges
            if self._z_rect(z).contains(x, y):
                return z, ()
        return None, None

    def mouseMoveEvent(self, e):
        if not self.target:
            return
        x, y = int(e.position().x()), int(e.position().y())
        dx, dy = x - self.start[0], y - self.start[1]
        z = self.target
        if self.mode == "move":
            # outline moves freely — icons only settle on genuinely free spots
            z["x"], z["y"] = self.orig[0] + dx, self.orig[1] + dy
            self._apply_snap(z)
            now = time.monotonic()
            throttled = now - getattr(self, "_last_icon_emit", 0.0) > 0.045
            if throttled:
                self._last_icon_emit = now
                self._icon_ok = self._area_free(z)  # foreign-icon check (costly)
            self.place_ok = (not self._collides(z)) and self._icon_ok
            if self.place_ok:
                self._mark_valid(z)
                if throttled:
                    f = self.hooks.get("drag_update")
                    if f:
                        f(z, z["x"] - self.orig[0], z["y"] - self.orig[1])
            self.update()
            return
        elif self.mode == "resize":
            ox, oy, ow, oh = self.orig
            if "l" in self.resize_edges:
                w = max(MIN_SIZE, ow - dx)
                z["x"], z["w"] = ox + ow - w, w
            if "r" in self.resize_edges:
                z["w"] = max(MIN_SIZE, ow + dx)
            if "t" in self.resize_edges:
                h = max(MIN_SIZE, oh - dy)
                z["y"], z["h"] = oy + oh - h, h
            if "b" in self.resize_edges:
                z["h"] = max(MIN_SIZE, oh + dy)
        elif self.mode == "new":
            z["x"] = min(self.start[0], x) + self.vx
            z["y"] = min(self.start[1], y) + self.vy
            z["w"], z["h"] = abs(dx), abs(dy)
        self._apply_snap(z)
        self._keep_valid(z)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        z, mode, orig = self.target, self.mode, self.orig
        self.mode = self.target = self.orig = None
        self.resize_edges = ()
        self.guides = []
        if self.locked:
            self.busy = False
        if not z:
            self.update()
            return
        if mode == "new":
            if z["w"] < MIN_SIZE or z["h"] < MIN_SIZE:
                self.zones.remove(z)
            else:
                name, ok = QInputDialog.getText(None, "Zone label", "Label for this zone:")
                z["name"] = (name or "Zone").strip() or "Zone" if ok else "Zone"
                # new zones auto-keep their icons by default
                z["pin"] = True
                z["icons"] = self.hooks["capture_icons"](z)
        elif mode == "move" and orig:
            if self._collides(z) or not self._area_free(z):
                # dropped on an occupied spot: back to the last free position
                z["x"], z["y"] = self._valid[0], self._valid[1]
            self.place_ok = True
            dx, dy = z["x"] - orig[0], z["y"] - orig[1]
            # always finalize: icons return to exact base + delta positions
            self.hooks["zone_moved"](z, dx, dy)
        self.update()
        self.hooks["changed"]()

    def mouseDoubleClickEvent(self, e):
        x, y = int(e.position().x()), int(e.position().y())
        if self.locked:
            zone, kind, _ = self._element_at(x, y)
            self.mode = self.target = None  # cancel the move the 1st click began
            if zone and kind == "label":
                self._rename(zone)
            return
        z, _ = self._hit_unlocked(x, y)
        if z:
            self._rename(z)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter) and not self.locked:
            self.lock()
        else:
            super().keyPressEvent(e)

    # ---------- zone actions ----------
    def _rename(self, zone):
        self.busy = True
        name, ok = QInputDialog.getText(None, "Rename zone", "New label:",
                                        text=zone.get("name", ""))
        self.busy = False
        if ok and name is not None:
            zone["name"] = name.strip() or zone.get("name", "Zone")
            self.update()
            self.hooks["changed"]()

    def _set_icon(self, zone):
        self.busy = True
        icon, ok = QInputDialog.getText(None, "Zone icon",
                                        "Emoji or symbol (empty to remove):",
                                        text=zone.get("icon", ""))
        self.busy = False
        if ok and icon is not None:
            zone["icon"] = icon.strip()[:4]
            self.update()
            self.hooks["changed"]()

    def _set_color(self, zone, color):
        zone["color"] = color
        self.update()
        self.hooks["changed"]()

    def _pick_color(self, zone):
        saved = self.hooks.get("custom_colors")
        for i, c in enumerate((saved or [])[:16]):
            QColorDialog.setCustomColor(i, QColor(c))
        self.busy = True
        col = QColorDialog.getColor(
            QColor(zone.get("color", "#4FC3F7")), None, "Zone color",
            QColorDialog.DontUseNativeDialog)  # Qt dialog: hex field + eyedropper
        self.busy = False
        if not col.isValid():
            return
        hexcol = col.name().upper()
        self._set_color(zone, hexcol)
        if saved is not None and hexcol not in config.ZONE_COLORS and hexcol not in saved:
            saved.insert(0, hexcol)
            del saved[16:]
            self.hooks["changed"]()

    def _toggle_pin(self, zone):
        if zone.get("pin"):
            zone["pin"] = False
        else:
            zone["pin"] = True
            zone["icons"] = self.hooks["capture_icons"](zone)
        self.update()
        self.hooks["changed"]()

    def _recapture(self, zone):
        zone["icons"] = self.hooks["capture_icons"](zone)
        self.hooks["changed"]()

    def _toggle_resizable(self, zone):
        zone["resizable"] = not zone.get("resizable", True)
        self.update()
        self.hooks["changed"]()

    def _delete_zone(self, zone):
        self.zones.remove(zone)
        if self.hover_zone is zone:
            self.hover_zone = None
        self.update()
        self.hooks["changed"]()

    @staticmethod
    def _swatch(color):
        pm = QPixmap(16, 16)
        pm.fill(QColor(color))
        return pm

    def _menu(self, zone):
        self.busy = True
        m = QMenu()
        pinned = bool(zone.get("pin"))
        resizable = zone.get("resizable", True)
        shape = zone.get("shape", "rect")
        tcol = zone.get("title_color", "#101010")
        kept = len(zone.get("icons", {}))

        header = m.addAction(f"Zone: {zone.get('name', 'Zone')}")
        header.setEnabled(False)
        m.addSeparator()
        m.addAction("Rename...", lambda: self._rename(zone))
        m.addAction("Set icon (emoji)...", lambda: self._set_icon(zone))

        shapes = m.addMenu("Shape")
        for key, label in (("rect", "Rectangle"), ("round", "Rounded rectangle"),
                           ("ellipse", "Ellipse")):
            a = shapes.addAction(label, lambda k=key: self._set_shape(zone, k))
            a.setCheckable(True)
            a.setChecked(shape == key)

        colors = m.addMenu("Color")
        for col in config.ZONE_COLORS:
            colors.addAction(self._swatch(col), col,
                             lambda c=col: self._set_color(zone, c))
        saved = self.hooks.get("custom_colors") or []
        if saved:
            colors.addSeparator()
            for col in saved:
                colors.addAction(self._swatch(col), col,
                                 lambda c=col: self._set_color(zone, c))
        colors.addSeparator()
        colors.addAction("Color picker (hex / eyedropper)...",
                         lambda: self._pick_color(zone))

        titles = m.addMenu("Title color")
        for label, col in (("White", "#FFFFFF"), ("Black", "#101010")):
            a = titles.addAction(label, lambda c=col: self._set_title_color(zone, c))
            a.setCheckable(True)
            a.setChecked(tcol == col)

        m.addAction(f"Border radius... ({zone.get('radius', 10)} px)",
                    lambda: self._set_radius(zone))
        m.addAction(f"Background opacity... ({zone.get('bg_opacity', 0)} %)",
                    lambda: self._set_bg_opacity(zone))

        m.addSeparator()
        a = m.addAction("Auto-keep icons in zone", lambda: self._toggle_pin(zone))
        a.setCheckable(True)
        a.setChecked(pinned)
        r = m.addAction(f"Recapture icons now ({kept} kept)",
                        lambda: self._recapture(zone))
        r.setEnabled(pinned)
        a = m.addAction("Lock zone (no move/resize)",
                        lambda: self._toggle_resizable(zone))
        a.setCheckable(True)
        a.setChecked(not resizable)
        m.addSeparator()
        m.addAction("Delete zone", lambda: self._delete_zone(zone))

        m.exec(QCursor.pos())
        self.busy = False

    def _set_shape(self, zone, shape):
        zone["shape"] = shape
        self.update()
        self.hooks["changed"]()

    def _live_slider(self, title, initial, on_preview, on_cancel, suffix=" px",
                     maximum=100):
        """Slider + spinbox dialog; on_preview(v) fires on every change so the
        zone re-renders live. Returns True if accepted."""
        dlg = QDialog()
        dlg.setWindowTitle(title)
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(dlg)

        row = QHBoxLayout()
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, maximum)
        slider.setValue(initial)
        spin = QSpinBox()
        spin.setRange(0, maximum)
        spin.setValue(initial)
        spin.setSuffix(suffix)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        lay.addLayout(row)

        btns = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        lay.addLayout(btns)

        def preview(v):
            slider.blockSignals(True)
            spin.blockSignals(True)
            slider.setValue(v)
            spin.setValue(v)
            slider.blockSignals(False)
            spin.blockSignals(False)
            on_preview(v)

        slider.valueChanged.connect(preview)
        spin.valueChanged.connect(preview)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)

        self.busy = True
        accepted = dlg.exec() == QDialog.Accepted
        self.busy = False
        if accepted:
            self.hooks["changed"]()
        else:
            on_cancel()
            self.update()
        return accepted

    def _set_radius(self, zone):
        orig_rad = zone.get("radius", 10)
        orig_shape = zone.get("shape", "rect")

        def preview(v):
            zone["radius"] = v
            zone["shape"] = "round"  # radius implies rounded rectangle
            self.update()

        def cancel():
            zone["radius"] = orig_rad
            zone["shape"] = orig_shape

        self._live_slider("Border radius", orig_rad, preview, cancel)

    def _set_bg_opacity(self, zone):
        orig = zone.get("bg_opacity", 0)

        def preview(v):
            zone["bg_opacity"] = v
            self.update()

        def cancel():
            zone["bg_opacity"] = orig

        self._live_slider("Background opacity", orig, preview, cancel,
                          suffix=" %")

    def _set_title_color(self, zone, col):
        zone["title_color"] = col
        self.update()
        self.hooks["changed"]()

    # ---------- painting ----------
    def _shape_path(self, z, r):
        path = QPainterPath()
        shape = z.get("shape", "rect")
        rf = QRectF(r)
        if shape == "ellipse":
            path.addEllipse(rf)
        elif shape == "round":
            rad = min(z.get("radius", 10), r.width() // 2, r.height() // 2)
            path.addRoundedRect(rf, rad, rad)
        else:
            path.addRect(rf)
        return path

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        if not self.locked:
            p.fillRect(self.rect(), EDIT_BG)
            p.setFont(self.hint_font)
            p.setPen(QColor("#9adcf0"))
            p.drawText(QRect(0, 8, self.vw, round(30 * self.s)), Qt.AlignCenter,
                       "Drag empty space: new zone   |   Drag inside: move   |   "
                       "Border: resize   |   Double-click: rename   |   "
                       "Right-click: delete   |   Esc: done")

        for z in self.zones:
            r = self._effective_rect(z)
            color = QColor(z.get("color", "#4FC3F7"))
            path = self._shape_path(z, r)
            hovered = self.locked and z is self.hover_zone

            if not self.locked:
                p.fillPath(path, QColor(32, 38, 46, 220))
            else:
                alpha = round(z.get("bg_opacity", 0) * 2.55)
                if hovered:
                    alpha = min(255, alpha + 30)  # soft extra wash on hover
                if alpha:
                    fill = QColor(color)
                    fill.setAlpha(alpha)
                    p.fillPath(path, fill)

            if (z is self.target and self.mode == "move"
                    and not getattr(self, "place_ok", True)):
                pen = QPen(GUIDE_COLOR, 2, Qt.DashLine)  # spot not free
            else:
                pen = QPen(color, 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

            if not self.locked:
                if z.get("name"):
                    p.setFont(self.label_font)
                    p.setPen(QColor(z.get("title_color", "#101010")))
                    p.drawText(r.adjusted(10, 8, -8, 0),
                               Qt.AlignTop | Qt.AlignLeft, z["name"])
                if z.get("resizable", True):
                    p.fillRect(r.right() - HANDLE, r.bottom() - HANDLE,
                               HANDLE, HANDLE, color)
                continue

            if hovered:
                self._paint_label(p, z, color)
                if z.get("resizable", True):
                    self._paint_grips(p, r, color)

        # snap guides
        if self.guides:
            pen = QPen(GUIDE_COLOR, 1, Qt.DashLine)
            p.setPen(pen)
            for kind, pos in self.guides:
                if kind == "v":
                    gx = pos - self.vx
                    p.drawLine(gx, 0, gx, self.vh)
                else:
                    gy = pos - self.vy
                    p.drawLine(0, gy, self.vw, gy)
        p.end()

    def _paint_label(self, p, z, color):
        pill, gear, text = self._label_geom(z)
        rad = pill.height() / 2
        # pill with a soft shadow
        shadow = QRectF(pill).translated(1.5, 2)
        sp = QPainterPath()
        sp.addRoundedRect(shadow, rad, rad)
        p.fillPath(sp, QColor(0, 0, 0, 70))
        pp = QPainterPath()
        pp.addRoundedRect(QRectF(pill), rad, rad)
        p.fillPath(pp, color)
        p.setFont(self.label_font)
        p.setPen(QColor(z.get("title_color", "#101010")))
        p.drawText(pill, Qt.AlignCenter, text)
        # gear
        gp = QPainterPath()
        gp.addEllipse(QRectF(gear))
        p.fillPath(gp, QColor(30, 34, 40, 235))
        p.setPen(QPen(color, 1.2))
        p.drawPath(gp)
        p.setPen(color)
        p.drawText(gear, Qt.AlignCenter, "⚙")

    def _paint_grips(self, p, r, color):
        g = GRIP
        cx, cy = r.center().x(), r.center().y()
        p.setPen(QPen(color, 1))
        for gx, gy in ((r.left(), cy), (r.right(), cy),
                       (cx, r.top()), (cx, r.bottom()),
                       (r.right(), r.bottom())):
            p.fillRect(gx - g // 2, gy - g // 2, g, g, QColor(30, 34, 40, 235))
            p.drawRect(gx - g // 2, gy - g // 2, g, g)

    def destroy_overlay(self):
        self.pin_timer.stop()
        self.hover_timer.stop()
        self.close()
        self.deleteLater()
