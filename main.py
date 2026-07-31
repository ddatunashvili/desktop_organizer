"""Desktop Grid (PySide6) — labeled zones on the desktop, live-editable
anytime, with per-zone "auto-keep icons" enforcement and per-resolution
icon layout memory.

Run with:  pythonw main.py   (or python main.py to see console errors)
"""

import ctypes
import os
import subprocess
import sys

# Real physical pixels everywhere: our zone/icon math is Win32 pixel based.
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QLabel, QMenu,
                               QMessageBox, QPushButton, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

import config
from desktop_icons import DesktopIcons
from overlay import ZoneOverlay, virtual_screen

POLL_MS = 3000           # display-change poll interval
ENFORCE_MS = 2000        # pinned-zone icon enforcement interval
RESTORE_DELAY_MS = 2500  # let Windows finish rearranging before we fix icons
ICON_ANCHOR = 40         # px offset from icon top-left used for "inside zone" test


def resolution_key():
    vx, vy, vw, vh = virtual_screen()
    monitors = ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS
    return f"{vw}x{vh}@{monitors}"


def zone_contains(zone, sx, sy):
    px, py = sx + ICON_ANCHOR, sy + ICON_ANCHOR
    return (zone["x"] <= px <= zone["x"] + zone["w"]
            and zone["y"] <= py <= zone["y"] + zone["h"])


def resource_path(name):
    """Works both as a script and inside a PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def make_tray_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#4FC3F7"), 6))
    p.drawRoundedRect(6, 6, 52, 52, 10, 10)
    p.drawLine(32, 6, 32, 58)
    p.drawLine(6, 32, 58, 32)
    p.end()
    return QIcon(pm)


def app_icon():
    logo = resource_path("logo.png")
    if os.path.exists(logo):
        return QIcon(logo)
    return make_tray_icon()


class ControlPanel(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("Desktop Grid")
        self.setFixedSize(360, 360)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        lay = QVBoxLayout(self)
        title = QLabel("Desktop Grid")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet("color: #556;")
        lay.addWidget(self.status)

        self.edit_btn = QPushButton("Draw new zones")
        self.edit_btn.clicked.connect(app.toggle_edit)
        lay.addWidget(self.edit_btn)

        b = QPushButton("Save icon layout (this resolution)")
        b.clicked.connect(app.save_layout)
        lay.addWidget(b)

        b = QPushButton("Restore icon layout")
        b.clicked.connect(lambda: app.restore_layout(manual=True))
        lay.addWidget(b)

        self.auto_chk = QCheckBox("Auto-restore when screen/resolution changes")
        self.auto_chk.setChecked(app.cfg.get("auto_restore", True))
        self.auto_chk.toggled.connect(app.toggle_auto)
        lay.addWidget(self.auto_chk)

        self.autostart_chk = QCheckBox("Start with Windows")
        self.autostart_chk.setChecked(app.is_autostart())
        self.autostart_chk.toggled.connect(app.set_autostart)
        lay.addWidget(self.autostart_chk)

        self.drag_chk = QCheckBox("Dragging mode: moving a zone moves its icons")
        self.drag_chk.setChecked(app.cfg.get("drag_mode", False))
        self.drag_chk.toggled.connect(app.toggle_drag_mode)
        lay.addWidget(self.drag_chk)

        hint = QLabel("Closing this window keeps Desktop Grid running in the tray.")
        hint.setStyleSheet("color: #889; font-size: 11px;")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)

    def closeEvent(self, event):  # close = minimize to tray
        event.ignore()
        self.hide()


class App:
    def __init__(self):
        self.qapp = QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.cfg = config.load()
        self.last_key = resolution_key()
        self._di = None
        self._drag_icons = {}  # base positions captured at zone-drag start

        self.ui_scale = max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)

        # Optional custom label font (family auto-detected from the file).
        self.font_family = None
        font_path = self.cfg.get("font_file", "")
        if font_path and os.path.exists(font_path):
            fid = QFontDatabase.addApplicationFont(font_path)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                self.font_family = self.cfg.get("font_family") or fams[0]

        try:
            with DesktopIcons() as di:
                if not di.disable_auto_arrange():
                    QMessageBox.warning(
                        None, "Auto arrange",
                        "Could not turn off icon auto-arrange.\n"
                        "Right-click desktop > View > uncheck 'Auto arrange icons', "
                        "or saved positions will not stick.")
        except RuntimeError as e:
            QMessageBox.critical(None, "Desktop Grid", str(e))

        icon = app_icon()
        self.qapp.setWindowIcon(icon)
        self.panel = ControlPanel(self)
        self.panel.setWindowIcon(icon)
        self.panel.show()
        self.overlay = self._make_overlay()

        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip("Desktop Grid")
        tray_menu = QMenu()
        tray_menu.addAction("Open control panel", self.show_panel)
        tray_menu.addAction("Draw new zones", self.toggle_edit)
        tray_menu.addAction("Restore icon layout", lambda: self.restore_layout(manual=True))
        tray_menu.addSeparator()
        tray_menu.addAction("Quit Desktop Grid", self.quit)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(
            lambda reason: self.show_panel()
            if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

        self.display_timer = QTimer(interval=POLL_MS, timeout=self.poll_display)
        self.display_timer.start()
        self.enforce_timer = QTimer(interval=ENFORCE_MS, timeout=self.enforce_loop)
        self.enforce_timer.start()
        self.update_status()

    def _make_overlay(self):
        return ZoneOverlay(self.cfg["zones"], {
            "changed": self.zones_changed,
            "capture_icons": self.capture_zone_icons,
            "zone_moved": self.zone_moved,
            "custom_colors": self.cfg.setdefault("custom_colors", []),
            "font_family": self.font_family,
            "drag_mode": lambda: self.cfg.get("drag_mode", False),
            "drag_start": self.zone_drag_start,
            "drag_update": self.zone_dragging,
            "area_free": self.area_free,
            "fit_zone": self.fit_zone,
        }, ui_scale=self.ui_scale)

    def show_panel(self):
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    # ---- autostart (Startup-folder shortcut) ----
    @staticmethod
    def startup_lnk():
        return os.path.join(os.environ["APPDATA"],
                            r"Microsoft\Windows\Start Menu\Programs\Startup",
                            "DesktopGrid.lnk")

    def is_autostart(self):
        return os.path.exists(self.startup_lnk())

    def set_autostart(self, enabled):
        lnk = self.startup_lnk()
        if not enabled:
            try:
                os.remove(lnk)
            except OSError:
                pass
            return
        if getattr(sys, "frozen", False):  # packaged exe
            target, args = sys.executable, ""
            workdir = os.path.dirname(sys.executable)
        else:
            target = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(target):
                target = sys.executable
            script = os.path.abspath(__file__)
            args = f'"{script}"'
            workdir = os.path.dirname(script)
        ps = (f"$ws = New-Object -ComObject WScript.Shell; "
              f"$l = $ws.CreateShortcut('{lnk}'); "
              f"$l.TargetPath = '{target}'; "
              f"$l.Arguments = '{args}'; "
              f"$l.WorkingDirectory = '{workdir}'; $l.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       creationflags=subprocess.CREATE_NO_WINDOW)

    # ---- desktop icon access (cached, survives explorer restarts) ----
    def icons(self):
        if self._di is None or not ctypes.windll.user32.IsWindow(self._di.listview):
            if self._di:
                self._di.close()
            self._di = DesktopIcons()
        return self._di

    # ---- status ----
    def update_status(self, extra=""):
        key = resolution_key()
        saved = "yes" if key in self.cfg["layouts"] else "no"
        pinned = sum(1 for z in self.cfg["zones"] if z.get("pin"))
        text = (f"Screen {key}  |  zones: {len(self.cfg['zones'])}"
                f"  |  auto-keep zones: {pinned}  |  layout saved: {saved}")
        if extra:
            text += f"\n{extra}"
        self.panel.status.setText(text)

    # ---- zone hooks (called by overlay) ----
    def zones_changed(self):
        config.save(self.cfg)
        if self.overlay.locked:
            self.panel.edit_btn.setText("Draw new zones")
        self.update_status("Zones saved.")

    def capture_zone_icons(self, zone):
        try:
            items = self.icons().keyed_icons()
        except RuntimeError:
            return {}
        others = {k for z in self.cfg["zones"] if z is not zone and z.get("pin")
                  for k in z.get("icons", {})}
        return {key: [sx, sy] for key, _i, sx, sy in items
                if zone_contains(zone, sx, sy) and key not in others}

    def fit_zone(self, zone):
        """Resize the zone to its icons' bounding box with EQUAL padding on
        all four sides (based on the outermost icons)."""
        PAD, ICON_W, ICON_H = 14, 80, 92  # match the overlay's hug metrics
        try:
            items = self.icons().keyed_icons()
        except RuntimeError:
            return
        kept = set(zone.get("icons", {})) if zone.get("pin") else set()
        pts = [(sx, sy) for key, _i, sx, sy in items
               if (key in kept if kept else zone_contains(zone, sx, sy))]
        if not pts:
            return
        x1 = min(p[0] for p in pts) - PAD
        y1 = min(p[1] for p in pts) - PAD
        x2 = max(p[0] for p in pts) + ICON_W + PAD
        y2 = max(p[1] for p in pts) + ICON_H + PAD
        zone["x"], zone["y"] = x1, y1
        zone["w"], zone["h"] = max(60, x2 - x1), max(60, y2 - y1)
        config.save(self.cfg)
        self.overlay.update()

    def area_free(self, zone):
        """True when no foreign icons sit inside the zone's current rect —
        the zone's own carried/kept icons don't count."""
        try:
            items = self.icons().keyed_icons()
        except RuntimeError:
            return True
        own = set(self._drag_icons or {})
        if zone.get("pin"):
            own |= set(zone.get("icons", {}))
        for key, _i, sx, sy in items:
            if key not in own and zone_contains(zone, sx, sy):
                return False
        return True

    def zone_drag_start(self, zone):
        """Capture base positions of every icon that must travel with the
        zone. All later moves are base + total delta, so relative positions
        inside the zone stay pixel-exact with zero drift."""
        self._drag_icons = {}
        pinned = bool(zone.get("pin"))
        drag = self.cfg.get("drag_mode", False)
        if not (pinned or drag):
            return
        try:
            items = self.icons().keyed_icons()
        except RuntimeError:
            return
        kept = zone.get("icons", {}) if pinned else {}
        for key, i, sx, sy in items:
            if key in kept or (drag and zone_contains(zone, sx, sy)):
                self._drag_icons[key] = (i, sx, sy)

    def zone_dragging(self, zone, dx, dy):
        """Live-follow while the zone is being dragged (throttled by overlay)."""
        if not self._drag_icons:
            return
        try:
            di = self.icons()
        except RuntimeError:
            return
        for _key, (i, sx, sy) in self._drag_icons.items():
            di.set_position_screen(i, sx + dx, sy + dy)

    def zone_moved(self, zone, dx, dy):
        """Final placement on release: exact base + delta, then remember."""
        base = self._drag_icons or {}
        self._drag_icons = {}
        if not base:
            return
        try:
            di = self.icons()
        except RuntimeError:
            return
        for _key, (i, sx, sy) in base.items():
            di.set_position_screen(i, sx + dx, sy + dy)
        di.redraw()
        if zone.get("pin"):
            kept = zone.setdefault("icons", {})
            for key, (_i, sx, sy) in base.items():
                kept[key] = [sx + dx, sy + dy]
            # kept icons that are currently missing from the desktop still
            # shift with the zone so they land right if they come back
            for k, p in list(kept.items()):
                if k not in base:
                    kept[k] = [p[0] + dx, p[1] + dy]

    # ---- per-zone icon enforcement ----
    def enforce_loop(self):
        if self.overlay.busy or not self.overlay.locked:
            return
        pinned = [z for z in self.cfg["zones"] if z.get("pin")]
        if not pinned:
            return
        try:
            di = self.icons()
            items = {key: (i, sx, sy) for key, i, sx, sy in di.keyed_icons()}
        except RuntimeError:
            return
        owned = {k for z in pinned for k in z.get("icons", {})}
        cfg_dirty = False
        moved_any = False
        refit = []  # auto-fit zones whose icon set/layout changed this tick
        for zone in pinned:
            kept = zone.setdefault("icons", {})
            zone_dirty = False
            for key, pos in list(kept.items()):
                cur = items.get(key)
                if cur is None:
                    continue  # icon gone (deleted/renamed); keep the slot
                idx, sx, sy = cur
                if zone_contains(zone, sx, sy):
                    if [sx, sy] != pos:  # rearranged inside its zone -> remember
                        kept[key] = [sx, sy]
                        cfg_dirty = zone_dirty = True
                elif [sx, sy] != pos:
                    if zone.get("auto_fit"):
                        # auto-fit zones follow their icons instead of
                        # dragging them back — the border re-fits around them
                        kept[key] = [sx, sy]
                        cfg_dirty = zone_dirty = True
                    else:  # escaped the zone -> put it back
                        di.set_position_screen(idx, pos[0], pos[1])
                        moved_any = True
            for key, (idx, sx, sy) in items.items():
                if key not in owned and zone_contains(zone, sx, sy):
                    kept[key] = [sx, sy]
                    owned.add(key)
                    cfg_dirty = zone_dirty = True
            if zone_dirty and zone.get("auto_fit"):
                refit.append(zone)
        for zone in refit:
            self.fit_zone(zone)
        if moved_any:
            di.redraw()
        if cfg_dirty:
            config.save(self.cfg)
        if moved_any or cfg_dirty:
            self.overlay.update()  # border may need to re-hug the icons

    # ---- actions ----
    def toggle_edit(self):
        if self.overlay.locked:
            self.overlay.unlock()
            self.panel.edit_btn.setText("Done (or press Esc)")
            self.update_status("Drawing mode: drag on empty desktop to add a zone.")
        else:
            self.overlay.lock()  # triggers zones_changed

    def save_layout(self):
        try:
            layout = self.icons().capture_layout()
        except RuntimeError as e:
            QMessageBox.critical(self.panel, "Desktop Grid", str(e))
            return
        key = resolution_key()
        self.cfg["layouts"][key] = layout
        config.save(self.cfg)
        self.update_status(f"Saved {len(layout)} icon positions for {key}.")

    def restore_layout(self, manual=False):
        key = resolution_key()
        layout = self.cfg["layouts"].get(key)
        if not layout:
            if manual:
                QMessageBox.information(
                    self.panel, "Desktop Grid",
                    f"No saved layout for {key}.\nArrange icons, then 'Save icon layout'.")
            return
        try:
            di = self.icons()
            di.disable_auto_arrange()
            moved, missing = di.apply_layout(layout)
        except RuntimeError as e:
            if manual:
                QMessageBox.critical(self.panel, "Desktop Grid", str(e))
            return
        note = f"Restored {moved} icons." + (
            f" {missing} saved icons no longer exist." if missing else "")
        self.update_status(note)

    def toggle_auto(self, checked):
        self.cfg["auto_restore"] = bool(checked)
        config.save(self.cfg)

    def toggle_drag_mode(self, checked):
        self.cfg["drag_mode"] = bool(checked)
        config.save(self.cfg)

    # ---- display change watcher ----
    def poll_display(self):
        key = resolution_key()
        if key != self.last_key:
            self.last_key = key
            self.rebuild_overlay()
            if self.cfg.get("auto_restore", True):
                QTimer.singleShot(RESTORE_DELAY_MS, self.restore_layout)
            self.update_status(f"Display changed to {key}.")

    def rebuild_overlay(self):
        self.overlay.destroy_overlay()
        self.overlay = self._make_overlay()

    def quit(self):
        config.save(self.cfg)
        self.overlay.destroy_overlay()
        if self._di:
            self._di.close()
        self.tray.hide()
        self.qapp.quit()

    def run(self):
        return self.qapp.exec()


if __name__ == "__main__":
    sys.exit(App().run())
