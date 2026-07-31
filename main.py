"""Desktop Grid — labeled zones on the desktop, live-editable anytime, with
per-zone "auto-keep icons" enforcement and per-resolution layout memory.

Run with:  pythonw main.py   (or python main.py to see console errors)
"""

import ctypes
import os
import tkinter as tk
from tkinter import messagebox

import config
from desktop_icons import DesktopIcons
from overlay import ZoneOverlay, virtual_screen

# Real pixel coordinates on high-DPI screens (Per-Monitor V2).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass

POLL_MS = 3000           # display-change poll interval
ENFORCE_MS = 4000        # pinned-zone icon enforcement interval
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


class App:
    def __init__(self):
        self.cfg = config.load()
        self.root = tk.Tk()
        self.root.title("Desktop Grid")
        self.root.geometry("340x280")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self.last_key = resolution_key()
        self._di = None  # cached DesktopIcons handle

        # Load the custom label font privately (process-only, no install).
        self.font_family = None
        font_path = self.cfg.get("font_file", "")
        if font_path and os.path.exists(font_path):
            FR_PRIVATE = 0x10
            if ctypes.windll.gdi32.AddFontResourceExW(font_path, FR_PRIVATE, 0):
                self.font_family = self.cfg.get("font_family") or None

        pad = {"padx": 12, "pady": 4}
        tk.Label(self.root, text="Desktop Grid", font=("Segoe UI", 13, "bold")).pack(pady=(10, 2))
        self.status = tk.Label(self.root, text="", fg="#446", font=("Segoe UI", 9),
                               wraplength=320, justify="center")
        self.status.pack()

        self.edit_btn = tk.Button(self.root, text="Draw new zones", command=self.toggle_edit, width=30)
        self.edit_btn.pack(**pad)
        tk.Button(self.root, text="Save icon layout (this resolution)",
                  command=self.save_layout, width=30).pack(**pad)
        tk.Button(self.root, text="Restore icon layout",
                  command=lambda: self.restore_layout(manual=True), width=30).pack(**pad)

        self.auto_var = tk.BooleanVar(value=self.cfg.get("auto_restore", True))
        tk.Checkbutton(self.root, text="Auto-restore when screen/resolution changes",
                       variable=self.auto_var, command=self.toggle_auto).pack(pady=4)

        try:
            with DesktopIcons() as di:
                if not di.disable_auto_arrange():
                    messagebox.showwarning(
                        "Auto arrange",
                        "Could not turn off icon auto-arrange.\n"
                        "Right-click desktop > View > uncheck 'Auto arrange icons', "
                        "or saved positions will not stick.")
        except RuntimeError as e:
            messagebox.showerror("Desktop Grid", str(e))

        self.overlay = ZoneOverlay(self.root, self.cfg["zones"], {
            "changed": self.zones_changed,
            "capture_icons": self.capture_zone_icons,
            "zone_moved": self.zone_moved,
            "custom_colors": self.cfg.setdefault("custom_colors", []),
            "font_family": self.font_family,
        })

        self.update_status()
        self.root.after(POLL_MS, self.poll_display)
        self.root.after(ENFORCE_MS, self.enforce_loop)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

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
        self.status.config(text=text)

    # ---- zone hooks (called by overlay) ----
    def zones_changed(self):
        config.save(self.cfg)
        if self.overlay.locked:
            self.edit_btn.config(text="Draw new zones")
        self.update_status("Zones saved.")

    def capture_zone_icons(self, zone):
        """Icons currently sitting inside the zone -> {key: [sx, sy]}."""
        try:
            items = self.icons().keyed_icons()
        except RuntimeError:
            return {}
        others = {k for z in self.cfg["zones"] if z is not zone and z.get("pin")
                  for k in z.get("icons", {})}
        return {key: [sx, sy] for key, _i, sx, sy in items
                if zone_contains(zone, sx, sy) and key not in others}

    def zone_moved(self, zone, dx, dy):
        """Zone dragged: its kept icons travel with it."""
        kept = zone.get("icons", {})
        if not kept:
            return
        for k in kept:
            kept[k] = [kept[k][0] + dx, kept[k][1] + dy]
        try:
            di = self.icons()
            positions = {key: (i, sx, sy) for key, i, sx, sy in di.keyed_icons()}
            for key, pos in kept.items():
                if key in positions:
                    di.set_position_screen(positions[key][0], pos[0], pos[1])
            di.redraw()
        except RuntimeError:
            pass

    # ---- per-zone icon enforcement ----
    def enforce_loop(self):
        self.root.after(ENFORCE_MS, self.enforce_loop)
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
        for zone in pinned:
            kept = zone.setdefault("icons", {})
            for key, pos in list(kept.items()):
                cur = items.get(key)
                if cur is None:
                    continue  # icon gone (deleted/renamed); keep the slot
                idx, sx, sy = cur
                if zone_contains(zone, sx, sy):
                    if [sx, sy] != pos:  # rearranged inside its zone -> remember
                        kept[key] = [sx, sy]
                        cfg_dirty = True
                elif [sx, sy] != pos:  # escaped the zone -> put it back
                    di.set_position_screen(idx, pos[0], pos[1])
                    moved_any = True
            # adopt unowned icons dropped into this zone
            for key, (idx, sx, sy) in items.items():
                if key not in owned and zone_contains(zone, sx, sy):
                    kept[key] = [sx, sy]
                    owned.add(key)
                    cfg_dirty = True
        if moved_any:
            di.redraw()
        if cfg_dirty:
            config.save(self.cfg)

    # ---- actions ----
    def toggle_edit(self):
        if self.overlay.locked:
            self.overlay.unlock()
            self.root.lift()  # keep the control window clickable above the dim layer
            self.edit_btn.config(text="Done (or press Esc)")
            self.update_status("Drawing mode: drag on empty desktop to add a zone.")
        else:
            self.overlay.lock()  # triggers zones_changed

    def save_layout(self):
        try:
            layout = self.icons().capture_layout()
        except RuntimeError as e:
            messagebox.showerror("Desktop Grid", str(e))
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
                messagebox.showinfo("Desktop Grid",
                                    f"No saved layout for {key}.\nArrange icons, then 'Save icon layout'.")
            return
        try:
            di = self.icons()
            di.disable_auto_arrange()
            moved, missing = di.apply_layout(layout)
        except RuntimeError as e:
            if manual:
                messagebox.showerror("Desktop Grid", str(e))
            return
        note = f"Restored {moved} icons." + (f" {missing} saved icons no longer exist." if missing else "")
        self.update_status(note)

    def toggle_auto(self):
        self.cfg["auto_restore"] = self.auto_var.get()
        config.save(self.cfg)

    # ---- display change watcher ----
    def poll_display(self):
        key = resolution_key()
        if key != self.last_key:
            self.last_key = key
            self.rebuild_overlay()
            if self.auto_var.get():
                self.root.after(RESTORE_DELAY_MS, self.restore_layout)
            self.update_status(f"Display changed to {key}.")
        self.root.after(POLL_MS, self.poll_display)

    def rebuild_overlay(self):
        self.overlay.destroy()
        self.overlay = ZoneOverlay(self.root, self.cfg["zones"], {
            "changed": self.zones_changed,
            "capture_icons": self.capture_zone_icons,
            "zone_moved": self.zone_moved,
            "custom_colors": self.cfg.setdefault("custom_colors", []),
            "font_family": self.font_family,
        })

    def quit(self):
        config.save(self.cfg)
        self.overlay.destroy()
        if self._di:
            self._di.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
