"""Zone overlay: always visible on the desktop, editable in place at any time.

The window covers the virtual screen and is pinned just above the desktop
(HWND_BOTTOM) so app windows still cover it. Empty (colorkey) pixels are
click-through, so the desktop works normally — but the drawn label tag, the
gear button and the corner handle DO catch the mouse:

  drag label tag      -> move zone (its kept icons travel with it)
  gear button         -> menu: rename / auto-keep icons / recapture / color / delete
  drag corner square  -> resize

"Edit zones" mode (unlock) is still available for drawing brand-new zones.
"""

import ctypes
from ctypes import wintypes
import tkinter as tk
import tkinter.font as tkfont
from tkinter import simpledialog

import config

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

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

TRANSPARENT_COLOR = "#000001"
EDIT_BG = "#14181d"
GUIDE_COLOR = "#FF4081"
HANDLE = 14
GRIP = 10     # px: mid-edge resize grips
EDGE = 6      # px: edge grab band in drawing mode
MIN_SIZE = 60
SNAP = 8      # px: snap distance to other zones' edges/centers (Figma-style)
PIN_MS = 2000

if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_long, _set_long = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
else:
    _get_long, _set_long = user32.GetWindowLongW, user32.SetWindowLongW


def virtual_screen():
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


class ColorDialog:
    """Small color dialog: hex entry + screen eyedropper + live preview."""

    def __init__(self, parent, initial, on_ok):
        self.on_ok = on_ok
        self.picker = None
        self._dc = None

        self.top = tk.Toplevel(parent)
        self.top.title("Zone color")
        self.top.attributes("-topmost", True)
        self.top.resizable(False, False)

        self.var = tk.StringVar(value=initial or "#4FC3F7")
        row = tk.Frame(self.top)
        row.pack(padx=14, pady=(12, 6))
        self.preview = tk.Canvas(row, width=38, height=38, highlightthickness=1,
                                 highlightbackground="#888")
        self.preview.pack(side="left", padx=(0, 10))
        entry = tk.Entry(row, textvariable=self.var, width=9, font=("Consolas", 12))
        entry.pack(side="left")
        tk.Button(row, text="Eyedropper 🖉", command=self.eyedrop).pack(side="left", padx=10)

        tk.Label(self.top, text="Type a hex color (#RRGGBB) or pick one from the screen.",
                 fg="#667", font=("Segoe UI", 8)).pack()
        btns = tk.Frame(self.top)
        btns.pack(pady=(6, 12))
        tk.Button(btns, text="OK", width=10, command=self.ok).pack(side="left", padx=6)
        tk.Button(btns, text="Cancel", width=10, command=self._cancel).pack(side="left")

        self.var.trace_add("write", lambda *a: self._update_preview())
        self._update_preview()
        entry.focus_set()
        self.top.bind("<Return>", lambda e: self.ok())
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

    @staticmethod
    def normalize(s):
        s = s.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            return None
        try:
            int(s, 16)
        except ValueError:
            return None
        return "#" + s.upper()

    def _update_preview(self):
        col = self.normalize(self.var.get())
        self.preview.config(bg=col if col else "#333333")

    # -- eyedropper: near-invisible fullscreen window catches the click,
    #    GetPixel samples the real screen color under the cursor --
    def eyedrop(self):
        if self.picker:
            return
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        p = tk.Toplevel(self.top)
        self.picker = p
        p.overrideredirect(True)
        p.geometry(f"{vw}x{vh}+{vx}+{vy}")
        p.attributes("-topmost", True)
        p.attributes("-alpha", 0.01)
        p.config(cursor="crosshair", bg="black")
        self._dc = user32.GetDC(0)
        p.bind("<Button-1>", self._picked)
        p.bind("<Escape>", lambda e: self._close_picker())
        p.bind("<Button-3>", lambda e: self._close_picker())
        p.focus_force()
        self._sample_loop()

    def _cursor_color(self):
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        col = gdi32.GetPixel(self._dc, pt.x, pt.y)
        if col == 0xFFFFFFFF:  # CLR_INVALID
            return None
        return "#{:02X}{:02X}{:02X}".format(col & 0xFF, (col >> 8) & 0xFF, (col >> 16) & 0xFF)

    def _sample_loop(self):
        if not self.picker or self._dc is None:
            return
        col = self._cursor_color()
        if col:
            self.preview.config(bg=col)
        try:
            self.picker.after(60, self._sample_loop)
        except tk.TclError:
            pass

    def _picked(self, e):
        col = self._cursor_color()
        self._close_picker()
        if col:
            self.var.set(col)

    def _close_picker(self):
        if self._dc is not None:
            user32.ReleaseDC(0, self._dc)
            self._dc = None
        if self.picker:
            self.picker.destroy()
            self.picker = None

    def ok(self):
        col = self.normalize(self.var.get())
        if not col:
            self.top.bell()
            return
        self._close_picker()
        self.top.destroy()
        self.on_ok(col)

    def _cancel(self):
        self._close_picker()
        self.top.destroy()


class ZoneOverlay:
    def __init__(self, root, zones, hooks):
        """zones: the config's live zone list (shared object, not copied).
        hooks: dict of callbacks —
          changed()                  save config after any zone edit
          capture_icons(zone)->dict  icons currently inside zone (for pinning)
          zone_moved(zone, dx, dy)   shift+apply the zone's kept icons
        """
        self.zones = zones
        self.hooks = hooks
        # Dialogs must be parented to a NORMAL window: the overlay carries
        # WS_EX_NOACTIVATE/override-redirect styles, and dialogs transient to
        # it get broken activation — Windows then delivers typed characters
        # through the ANSI path, turning Georgian (and other non-Latin) into '?'.
        self.dlg_parent = root
        self.locked = True
        self.busy = False  # True while a drag or menu is active
        self.alive = True

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.vx, self.vy, self.vw, self.vh = virtual_screen()
        self.win.geometry(f"{self.vw}x{self.vh}+{self.vx}+{self.vy}")
        self.win.config(bg=TRANSPARENT_COLOR)
        self.win.attributes("-transparentcolor", TRANSPARENT_COLOR)

        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        family = hooks.get("font_family") or "Segoe UI"
        self.label_font = tkfont.Font(family=family, size=10, weight="bold")
        self.name_font = tkfont.Font(family=family, size=11, weight="bold")
        self.gear_font = tkfont.Font(family="Segoe UI Symbol", size=10)

        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<ButtonPress-3>", self._right)
        c.bind("<Motion>", self._motion)
        self.win.bind("<Escape>", lambda e: self.lock())
        self.win.bind("<Return>", lambda e: self.lock())

        self.mode = None
        self.target = None
        self.start = (0, 0)
        self.orig = None
        self.resize_edges = ()  # subset of ("l","r","t","b") during a resize
        self._hitboxes = []  # (zone, kind, x1, y1, x2, y2) rebuilt on redraw
        self._guides = []    # [('v', abs_x) | ('h', abs_y)] snap guide lines
        self.hover_zone = None  # zone whose label tag is currently shown
        self._hide_job = None

        self.win.update_idletasks()
        self.hwnd = user32.GetParent(int(self.win.winfo_id())) or int(self.win.winfo_id())
        self._apply_locked_styles()
        self.redraw()
        self._pin_loop()
        self._hover_loop()

    # ---- lock / unlock ----
    def _apply_locked_styles(self):
        ex = _get_long(self.hwnd, GWL_EXSTYLE)
        # No WS_EX_TRANSPARENT: colorkey pixels stay click-through, but the
        # drawn tags/handles catch the mouse so zones are editable anytime.
        _set_long(self.hwnd, GWL_EXSTYLE,
                  (ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_TRANSPARENT)
        self.win.attributes("-alpha", 1.0)
        self.canvas.config(bg=TRANSPARENT_COLOR)
        user32.SetWindowPos(self.hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _apply_unlocked_styles(self):
        ex = _get_long(self.hwnd, GWL_EXSTYLE)
        _set_long(self.hwnd, GWL_EXSTYLE,
                  (ex | WS_EX_TOOLWINDOW) & ~WS_EX_TRANSPARENT & ~WS_EX_NOACTIVATE)
        self.canvas.config(bg=EDIT_BG)
        self.win.attributes("-alpha", 0.5)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE)
        self.win.focus_force()

    def unlock(self):
        if not self.locked:
            return
        self.locked = False
        self.busy = True
        self._apply_unlocked_styles()
        self.redraw()

    def lock(self):
        if self.locked:
            return
        self.locked = True
        self.busy = False
        self.zones[:] = [z for z in self.zones if z["w"] >= MIN_SIZE and z["h"] >= MIN_SIZE]
        self._apply_locked_styles()
        self.redraw()
        self.hooks["changed"]()

    def _pin_loop(self):
        if not self.alive:
            return
        if self.locked:
            user32.SetWindowPos(self.hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        try:
            self.win.after(PIN_MS, self._pin_loop)
        except tk.TclError:
            self.alive = False

    # ---- geometry ----
    def _z_rect(self, z):
        return (z["x"] - self.vx, z["y"] - self.vy,
                z["x"] - self.vx + z["w"], z["y"] - self.vy + z["h"])

    def _hit_zone(self, x, y):
        """Returns (zone, edges) — edges is a tuple of resize sides, () = body."""
        for z in reversed(self.zones):
            x1, y1, x2, y2 = self._z_rect(z)
            resizable = z.get("resizable", True)
            if resizable and x2 - HANDLE <= x <= x2 + 4 and y2 - HANDLE <= y <= y2 + 4:
                return z, ("r", "b")
            if x1 - EDGE <= x <= x2 + EDGE and y1 - EDGE <= y <= y2 + EDGE:
                edges = []
                if resizable:
                    if abs(x - x1) <= EDGE:
                        edges.append("l")
                    elif abs(x - x2) <= EDGE:
                        edges.append("r")
                    if abs(y - y1) <= EDGE:
                        edges.append("t")
                    elif abs(y - y2) <= EDGE:
                        edges.append("b")
                if edges:
                    return z, tuple(edges)
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return z, ()
        return None, None

    def _hit_box(self, x, y):
        for zone, kind, x1, y1, x2, y2 in self._hitboxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return zone, kind
        return None, None

    # ---- Figma-style snapping ----
    def _snap_targets(self, exclude):
        """All interesting x and y lines: other zones' edges + centers, screen edges."""
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
        """values: candidate lines of the moving zone. Returns (delta, target) or None."""
        best = None
        for v in values:
            for t in targets:
                d = t - v
                if abs(d) <= SNAP and (best is None or abs(d) < abs(best[0])):
                    best = (d, t)
        return best

    def _apply_snap(self, z):
        self._guides = []
        xs, ys = self._snap_targets(z)
        if self.mode == "move":
            sx = self._best_snap([z["x"], z["x"] + z["w"], z["x"] + z["w"] // 2], xs)
            sy = self._best_snap([z["y"], z["y"] + z["h"], z["y"] + z["h"] // 2], ys)
            if sx:
                z["x"] += sx[0]
                self._guides.append(("v", sx[1]))
            if sy:
                z["y"] += sy[0]
                self._guides.append(("h", sy[1]))
        elif self.mode in ("resize", "new"):
            edges = self.resize_edges if self.mode == "resize" else ("r", "b")
            if "l" in edges:
                s = self._best_snap([z["x"]], xs)
                if s and z["w"] - s[0] >= MIN_SIZE:
                    z["x"] += s[0]
                    z["w"] -= s[0]
                    self._guides.append(("v", s[1]))
            if "r" in edges:
                s = self._best_snap([z["x"] + z["w"]], xs)
                if s and z["w"] + s[0] >= MIN_SIZE:
                    z["w"] += s[0]
                    self._guides.append(("v", s[1]))
            if "t" in edges:
                s = self._best_snap([z["y"]], ys)
                if s and z["h"] - s[0] >= MIN_SIZE:
                    z["y"] += s[0]
                    z["h"] -= s[0]
                    self._guides.append(("h", s[1]))
            if "b" in edges:
                s = self._best_snap([z["y"] + z["h"]], ys)
                if s and z["h"] + s[0] >= MIN_SIZE:
                    z["h"] += s[0]
                    self._guides.append(("h", s[1]))

    # ---- hover (labels show only near a zone) ----
    def _zone_near(self, x, y):
        zone, _ = self._hit_box(x, y)
        if zone:
            return zone
        for z in reversed(self.zones):
            x1, y1, x2, y2 = self._z_rect(z)
            if x1 - EDGE <= x <= x2 + EDGE and y1 - EDGE <= y <= y2 + EDGE:
                return z
        return None

    def _motion(self, e):
        if not self.locked:
            return
        z = self._zone_near(e.x, e.y)
        if z is not self.hover_zone:
            self.hover_zone = z
            self.redraw()

    def _hover_loop(self):
        """Poll the cursor: interior pixels are click-through and emit no
        events, so hovering anywhere INSIDE a zone is detected here."""
        if not self.alive:
            return
        if self.locked and not self.busy:
            try:
                px = self.win.winfo_pointerx() - self.win.winfo_rootx()
                py = self.win.winfo_pointery() - self.win.winfo_rooty()
                z = self._zone_near(px, py)
                if z is not self.hover_zone:
                    self.hover_zone = z
                    self.redraw()
            except tk.TclError:
                return
        try:
            self.win.after(150, self._hover_loop)
        except tk.TclError:
            self.alive = False

    # ---- mouse ----
    def _start_resize(self, zone, edges):
        self.mode = "resize"
        self.target = zone
        self.resize_edges = edges
        self.orig = (zone["x"], zone["y"], zone["w"], zone["h"])

    def _press(self, e):
        self.start = (e.x, e.y)
        if self.locked:
            zone, kind = self._hit_box(e.x, e.y)
            if not zone:
                return
            self.busy = True
            if kind == "gear":
                self._menu(zone, e)
            elif kind == "label":
                if zone.get("resizable", True):  # locked zones don't move
                    self.mode, self.target, self.orig = "move", zone, (zone["x"], zone["y"])
                else:
                    self.busy = False
            elif kind == "handle":
                self._start_resize(zone, ("r", "b"))
            elif kind.startswith("edge-"):
                self._start_resize(zone, (kind[5:],))
            return
        # unlocked: free editing incl. drawing new zones
        z, edges = self._hit_zone(e.x, e.y)
        if z and edges:
            self._start_resize(z, edges)
        elif z and z.get("resizable", True):
            self.mode, self.target, self.orig = "move", z, (z["x"], z["y"])
        elif z:
            pass  # locked zone: stays where it is
        else:
            self.mode = "new"
            self.target = {"name": "", "x": e.x + self.vx, "y": e.y + self.vy,
                           "w": 0, "h": 0, "color": config.next_zone_color(self.zones)}
            self.zones.append(self.target)

    def _drag(self, e):
        if not self.target:
            return
        dx, dy = e.x - self.start[0], e.y - self.start[1]
        z = self.target
        if self.mode == "move":
            z["x"], z["y"] = self.orig[0] + dx, self.orig[1] + dy
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
            z["x"] = min(self.start[0], e.x) + self.vx
            z["y"] = min(self.start[1], e.y) + self.vy
            z["w"], z["h"] = abs(dx), abs(dy)
        self._apply_snap(z)
        self.redraw()

    def _release(self, e):
        z, mode, orig = self.target, self.mode, self.orig
        self.mode = self.target = self.orig = None
        self.resize_edges = ()
        self._guides = []
        if self.locked:
            self.busy = False
        if not z:
            return
        if mode == "new":
            if z["w"] < MIN_SIZE or z["h"] < MIN_SIZE:
                self.zones.remove(z)
            else:
                name = simpledialog.askstring("Zone label", "Label for this zone:", parent=self.dlg_parent)
                z["name"] = (name or "Zone").strip() or "Zone"
        elif mode == "move" and orig:
            dx, dy = z["x"] - orig[0], z["y"] - orig[1]
            if (dx or dy) and z.get("pin"):
                self.hooks["zone_moved"](z, dx, dy)
        self.redraw()
        self.hooks["changed"]()

    def _double(self, e):
        if self.locked:
            z, kind = self._hit_box(e.x, e.y)
            if z and kind == "label":  # double-click the title -> rename
                self._rename(z)
            return
        z, _ = self._hit_zone(e.x, e.y)
        if z:
            self._rename(z)

    def _right(self, e):
        if self.locked:
            zone, kind = self._hit_box(e.x, e.y)
            if zone:
                self._menu(zone, e)
            return
        z, _ = self._hit_zone(e.x, e.y)
        if z:
            self.zones.remove(z)
            self.redraw()

    # ---- zone actions ----
    def _rename(self, zone):
        name = simpledialog.askstring("Rename zone", "New label:",
                                      initialvalue=zone.get("name", ""), parent=self.dlg_parent)
        if name is not None:
            zone["name"] = name.strip() or zone.get("name", "Zone")
        self.redraw()
        self.hooks["changed"]()

    def _toggle_pin(self, zone):
        if zone.get("pin"):
            zone["pin"] = False
        else:
            zone["pin"] = True
            zone["icons"] = self.hooks["capture_icons"](zone)
        self.redraw()
        self.hooks["changed"]()

    def _recapture(self, zone):
        zone["icons"] = self.hooks["capture_icons"](zone)
        self.hooks["changed"]()

    def _set_color(self, zone, color):
        zone["color"] = color
        self.redraw()
        self.hooks["changed"]()

    def _pick_color(self, zone):
        saved = self.hooks.get("custom_colors")

        def ok(col):
            self._set_color(zone, col)
            # remember every custom color permanently (shown in the Color menu)
            if saved is not None and col not in config.ZONE_COLORS and col not in saved:
                saved.insert(0, col)
                del saved[16:]
                self.hooks["changed"]()

        ColorDialog(self.dlg_parent, zone.get("color", "#4FC3F7"), ok)

    def _set_shape(self, zone, shape):
        zone["shape"] = shape
        self.redraw()
        self.hooks["changed"]()

    def _set_icon(self, zone):
        icon = simpledialog.askstring(
            "Zone icon", "Emoji or symbol for this zone (empty to remove):",
            initialvalue=zone.get("icon", ""), parent=self.dlg_parent)
        if icon is not None:
            zone["icon"] = icon.strip()[:4]
            self.redraw()
            self.hooks["changed"]()

    def _set_title_color(self, zone, col):
        zone["title_color"] = col
        self.redraw()
        self.hooks["changed"]()

    def _toggle_resizable(self, zone):
        zone["resizable"] = not zone.get("resizable", True)
        self.redraw()
        self.hooks["changed"]()

    def _delete_zone(self, zone):
        self.zones.remove(zone)
        self.redraw()
        self.hooks["changed"]()

    def _menu(self, zone, e):
        m = tk.Menu(self.win, tearoff=0)
        pinned = bool(zone.get("pin"))
        resizable = zone.get("resizable", True)
        shape = zone.get("shape", "rect")
        kept = len(zone.get("icons", {}))

        m.add_command(label=f"Zone: {zone.get('name', 'Zone')}", state="disabled")
        m.add_separator()
        m.add_command(label="Rename...", command=lambda: self._rename(zone))
        m.add_command(label="Set icon (emoji)...", command=lambda: self._set_icon(zone))

        shapes = tk.Menu(m, tearoff=0)
        for key, label in (("rect", "Rectangle"), ("round", "Rounded rectangle"),
                           ("ellipse", "Ellipse")):
            mark = "✔ " if shape == key else "    "
            shapes.add_command(label=mark + label,
                               command=lambda k=key: self._set_shape(zone, k))
        m.add_cascade(label="Shape", menu=shapes)

        colors = tk.Menu(m, tearoff=0)
        for col in config.ZONE_COLORS:
            colors.add_command(label=col, background=col, foreground="#101010",
                               command=lambda c=col: self._set_color(zone, c))
        saved = self.hooks.get("custom_colors") or []
        if saved:
            colors.add_separator()
            for col in saved:
                colors.add_command(label=col, background=col, foreground="#101010",
                                   command=lambda c=col: self._set_color(zone, c))
        colors.add_separator()
        colors.add_command(label="Color picker (hex / eyedropper)...",
                           command=lambda: self._pick_color(zone))
        m.add_cascade(label="Color", menu=colors)

        tcol = zone.get("title_color", "#101010")
        titles = tk.Menu(m, tearoff=0)
        for label, col in (("White", "#FFFFFF"), ("Black", "#101010")):
            mark = "✔ " if tcol == col else "    "
            titles.add_command(label=mark + label,
                               command=lambda c=col: self._set_title_color(zone, c))
        m.add_cascade(label="Title color", menu=titles)

        m.add_separator()
        m.add_command(label=("✔ Auto-keep icons in zone" if pinned else "Auto-keep icons in zone"),
                      command=lambda: self._toggle_pin(zone))
        m.add_command(label=f"Recapture icons now ({kept} kept)",
                      state=("normal" if pinned else "disabled"),
                      command=lambda: self._recapture(zone))
        m.add_command(label=("Lock zone (no move/resize)" if resizable
                             else "✔ Locked — click to unlock"),
                      command=lambda: self._toggle_resizable(zone))
        m.add_separator()
        m.add_command(label="Delete zone", command=lambda: self._delete_zone(zone))

        def done(_=None):
            self.busy = False
        m.bind("<Unmap>", done)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
            self.busy = False

    # ---- drawing ----
    def _draw_shape(self, z, x1, y1, x2, y2, color, fill=""):
        c = self.canvas
        shape = z.get("shape", "rect")
        if shape == "ellipse":
            c.create_oval(x1, y1, x2, y2, outline=color, width=2, fill=fill)
        elif shape == "round":
            r = min(18, (x2 - x1) // 4, (y2 - y1) // 4)
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
                   x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                   x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            c.create_polygon(pts, smooth=True, outline=color, width=2, fill=fill)
        else:
            c.create_rectangle(x1, y1, x2, y2, outline=color, width=2, fill=fill)
            if self.locked:
                c.create_rectangle(x1 + 3, y1 + 3, x2 - 3, y2 - 3,
                                   outline=color, width=1, stipple="gray25")

    def redraw(self):
        c = self.canvas
        c.delete("all")
        self._hitboxes = []
        if not self.locked:
            c.create_text(
                self.vw // 2, 26,
                text="Drag empty space: new zone   |   Drag inside: move   |   Corner: resize   |"
                     "   Double-click: rename   |   Right-click: delete   |   Esc: done",
                fill="#9adcf0", font=("Segoe UI", 11, "bold"))
        for z in self.zones:
            x1, y1, x2, y2 = self._z_rect(z)
            color = z.get("color", "#4FC3F7")
            resizable = z.get("resizable", True)
            if not self.locked:
                self._draw_shape(z, x1, y1, x2, y2, color, fill="#20262e")
                if resizable:
                    c.create_rectangle(x2 - HANDLE, y2 - HANDLE, x2, y2, fill=color, outline=color)
                if z.get("name"):
                    c.create_text(x1 + 8, y1 + 12, text=z["name"], anchor="w",
                                  fill=z.get("title_color", "#101010"),
                                  font=self.name_font)
                continue

            # locked: outline + resize grips; label tag + gear only on hover
            self._draw_shape(z, x1, y1, x2, y2, color, fill="")
            if z is self.hover_zone:
                name = z.get("name", "") or "Zone"
                pin_mark = " ●" if z.get("pin") else ""
                icon = z.get("icon", "")
                text = (icon + " " if icon else "") + name + pin_mark
                tw = self.label_font.measure(text) + 12
                th = self.label_font.metrics("linespace") + 6
                gw = th + 4  # gear button width
                # tag sits ABOVE the zone, outside it (inside only if no room)
                ty2 = y1 - 2
                ty1 = ty2 - th
                if ty1 < 2:
                    ty1, ty2 = y1 + 2, y1 + 2 + th
                tcy = (ty1 + ty2) // 2
                # label tag (draggable = moves the zone)
                c.create_rectangle(x1, ty1, x1 + tw, ty2, fill=color, outline=color)
                c.create_text(x1 + 6, tcy, text=text, anchor="w",
                              font=self.label_font,
                              fill=z.get("title_color", "#101010"))
                self._hitboxes.append((z, "label", x1, ty1, x1 + tw, ty2))
                # gear button
                gx1 = x1 + tw + 1
                c.create_rectangle(gx1, ty1, gx1 + gw, ty2, fill="#22262b", outline=color)
                c.create_text(gx1 + gw // 2, tcy, text="⚙", font=self.gear_font, fill=color)
                self._hitboxes.append((z, "gear", gx1, ty1, gx1 + gw, ty2))
            if resizable:
                # corner resize handle
                c.create_rectangle(x2 - HANDLE, y2 - HANDLE, x2, y2, outline=color, fill="#22262b")
                self._hitboxes.append((z, "handle", x2 - HANDLE, y2 - HANDLE, x2 + 4, y2 + 4))
                # mid-edge resize grips (left / right / top / bottom)
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                g = GRIP // 2
                for kind, gx, gy in (("edge-l", x1, cy), ("edge-r", x2, cy),
                                     ("edge-t", cx, y1), ("edge-b", cx, y2)):
                    c.create_rectangle(gx - g, gy - g, gx + g, gy + g,
                                       outline=color, fill="#22262b")
                    self._hitboxes.append((z, kind, gx - g - 3, gy - g - 3, gx + g + 3, gy + g + 3))

        # snap guide lines (only visible mid-drag)
        for kind, pos in self._guides:
            if kind == "v":
                gx = pos - self.vx
                c.create_line(gx, 0, gx, self.vh, fill=GUIDE_COLOR, width=1, dash=(6, 3))
            else:
                gy = pos - self.vy
                c.create_line(0, gy, self.vw, gy, fill=GUIDE_COLOR, width=1, dash=(6, 3))

    def destroy(self):
        self.alive = False
        try:
            self.win.destroy()
        except tk.TclError:
            pass
