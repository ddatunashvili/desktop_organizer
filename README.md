# Desktop Grid

Draw labeled zones (bordered areas) directly on your Windows desktop wallpaper —
behind the icons — and keep your icon arrangement fixed even when the screen
resolution or monitor setup changes.

No dependencies: plain Python 3 (ctypes + tkinter).

## Run

Double-click `DesktopGrid.bat` (or run `pythonw main.py`).

A small control window opens and the zone overlay appears behind your desktop icons.

## Use

Zone lines and labels are **always visible** on the desktop (above icons,
underneath application windows). Empty areas are click-through; only the label
tag, gear button and corner handle catch the mouse.

**Edit any zone anytime, right on the desktop:**

- Zone titles appear on **hover** (move the mouse near a zone's border);
  they hide again ~0.6 s after the pointer leaves the zone
- Drag the **label tag** → move the zone (kept icons travel with it)
- Drag the **corner square** or any **mid-edge grip** → resize from that side
- Click the **⚙ gear** → menu:
  - Rename (any language incl. ქართული) / Set icon (emoji shown on the label)
  - Shape: rectangle / rounded rectangle / ellipse
  - Color: premade palette, your saved custom colors, or the color picker —
    type a hex code directly or use the eyedropper to grab any color from the
    screen; custom colors are saved forever and reappear in the menu
  - Auto-keep icons / Recapture icons
  - Lock zone: hides resize grips AND freezes position — the zone can't be
    moved or resized until unlocked
  - Delete
- While dragging, zones **snap** to other zones' edges & centers and screen
  edges (Figma-style pink guide lines)

**Auto-keep icons (per zone):** enable "Auto-keep icons in zone" in the gear
menu. The zone remembers every icon inside it (shown as ● on the label). If an
icon gets moved out — by you, an app, or Windows — it snaps back within a few
seconds. Rearranging icons *inside* the zone is fine and gets remembered. New
icons dropped into the zone are adopted automatically. Other icons are never
touched. To take an icon out of a zone: turn Auto-keep off, move it, then
"Recapture icons now".

**Draw new zones** button → desktop dims, drag on empty space to add zones,
Esc to finish.

Then drag your folders/shortcuts into zones the normal way.
3. **Save icon layout** — remembers every icon's position for the *current*
   resolution/monitor count.
4. Repeat step 3 once per setup you use (laptop alone, laptop + monitor, ...).
   When the display changes, icons snap back automatically (checkbox controls this).

Settings live in `%APPDATA%\DesktopGrid\config.json`.

## Start with Windows

Already installed (shortcut in `shell:startup`). To remove it, delete
`DesktopGrid.lnk` from the Startup folder or run `install_startup.bat` again
after editing. 

## Notes

- "Auto arrange icons" (desktop right-click → View) must stay **off**; the app
  turns it off automatically and warns if it can't.
- The overlay is a click-through layer pinned just above the desktop; app
  windows always cover it, mouse clicks pass straight through to the icons.
- If icons ever look stale after a restore, press F5 on the desktop.
