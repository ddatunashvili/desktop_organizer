"""Read and set positions of desktop icons via the explorer SysListView32.

The desktop icon list lives inside explorer.exe, so item text/position calls
need cross-process memory (VirtualAllocEx + Read/WriteProcessMemory).
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ListView messages
LVM_FIRST = 0x1000
LVM_GETITEMCOUNT = LVM_FIRST + 4
LVM_SETITEMPOSITION = LVM_FIRST + 15
LVM_GETITEMPOSITION = LVM_FIRST + 16
LVM_REDRAWITEMS = LVM_FIRST + 21
LVM_GETITEMTEXTW = LVM_FIRST + 115

LVS_AUTOARRANGE = 0x0100
GWL_STYLE = -16

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
kernel32.VirtualAllocEx.restype = wintypes.LPVOID
kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


class LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT),
        ("pszText", wintypes.LPVOID),  # remote pointer, keep as void*
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_ssize_t),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", wintypes.UINT),
        ("puColumns", wintypes.LPVOID),
        ("piColFmt", wintypes.LPVOID),
        ("iGroup", ctypes.c_int),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def find_desktop_listview():
    """Return HWND of the desktop icon SysListView32, or None."""
    progman = user32.FindWindowW("Progman", None)
    defview = user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)

    if not defview:
        # Wallpaper-engine style setups move DefView under a WorkerW window.
        found = []

        @WNDENUMPROC
        def enum_proc(hwnd, lparam):
            dv = user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
            if dv:
                found.append(dv)
                return False
            return True

        user32.EnumWindows(enum_proc, 0)
        defview = found[0] if found else None

    if not defview:
        return None
    return user32.FindWindowExW(defview, 0, "SysListView32", None) or None


class DesktopIcons:
    def __init__(self):
        self.listview = find_desktop_listview()
        if not self.listview:
            raise RuntimeError("Desktop icon list not found (are desktop icons hidden?)")
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(self.listview, ctypes.byref(pid))
        self.process = kernel32.OpenProcess(
            PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION,
            False, pid.value)
        if not self.process:
            raise RuntimeError("Cannot open explorer.exe process")

    def close(self):
        if self.process:
            kernel32.CloseHandle(self.process)
            self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def count(self):
        return user32.SendMessageW(self.listview, LVM_GETITEMCOUNT, 0, 0)

    def disable_auto_arrange(self):
        """Auto-arrange overrides manual positions; try to switch it off."""
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            get_long, set_long = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
        else:
            get_long, set_long = user32.GetWindowLongW, user32.SetWindowLongW
        style = get_long(self.listview, GWL_STYLE)
        if style & LVS_AUTOARRANGE:
            set_long(self.listview, GWL_STYLE, style & ~LVS_AUTOARRANGE)
            return not (get_long(self.listview, GWL_STYLE) & LVS_AUTOARRANGE)
        return True

    def _alloc(self, size):
        mem = kernel32.VirtualAllocEx(self.process, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not mem:
            raise ctypes.WinError(ctypes.get_last_error())
        return mem

    def _free(self, mem):
        kernel32.VirtualFreeEx(self.process, mem, 0, MEM_RELEASE)

    def get_icons(self):
        """Return list of (name, x, y). Coordinates are listview-space pixels."""
        n = self.count()
        icons = []
        text_cap = 520  # wchars
        remote_pt = self._alloc(ctypes.sizeof(POINT))
        remote_item = self._alloc(ctypes.sizeof(LVITEMW))
        remote_text = self._alloc(text_cap * 2)
        try:
            for i in range(n):
                pt = POINT()
                user32.SendMessageW(self.listview, LVM_GETITEMPOSITION, i, remote_pt)
                kernel32.ReadProcessMemory(self.process, remote_pt, ctypes.byref(pt), ctypes.sizeof(POINT), None)

                item = LVITEMW()
                item.iSubItem = 0
                item.pszText = remote_text
                item.cchTextMax = text_cap
                kernel32.WriteProcessMemory(self.process, remote_item, ctypes.byref(item), ctypes.sizeof(LVITEMW), None)
                length = user32.SendMessageW(self.listview, LVM_GETITEMTEXTW, i, remote_item)
                buf = ctypes.create_unicode_buffer(text_cap)
                kernel32.ReadProcessMemory(self.process, remote_text, buf, text_cap * 2, None)
                name = buf.value if length > 0 else f"<item {i}>"
                icons.append((name, pt.x, pt.y))
        finally:
            self._free(remote_pt)
            self._free(remote_item)
            self._free(remote_text)
        return icons

    def set_position(self, index, x, y):
        lparam = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
        user32.SendMessageW(self.listview, LVM_SETITEMPOSITION, index, lparam)

    def apply_layout(self, layout):
        """layout: {icon name: [x, y]}. Returns (moved, missing)."""
        icons = self.get_icons()
        # capture_layout() keys duplicates as "name (2)", "name (3)" in
        # encounter order — rebuild the same keys here to match.
        seen = {}
        used = 0
        moved = 0
        for i, (name, _x, _y) in enumerate(icons):
            k = seen.get(name, 0)
            seen[name] = k + 1
            key = name if k == 0 else f"{name} ({k + 1})"
            pos = layout.get(key)
            if pos:
                self.set_position(i, pos[0], pos[1])
                moved += 1
                used += 1
        missing = len(layout) - used
        user32.SendMessageW(self.listview, LVM_REDRAWITEMS, 0, max(0, self.count() - 1))
        user32.UpdateWindow(self.listview)
        return moved, missing

    def client_origin(self):
        """Screen coordinates of the listview's (0,0) client point."""
        pt = POINT(0, 0)
        user32.ClientToScreen(self.listview, ctypes.byref(pt))
        return pt.x, pt.y

    def keyed_icons(self):
        """[(key, index, screen_x, screen_y)] — duplicate names keyed 'name (2)'..."""
        ox, oy = self.client_origin()
        seen = {}
        out = []
        for i, (name, x, y) in enumerate(self.get_icons()):
            k = seen.get(name, 0)
            seen[name] = k + 1
            key = name if k == 0 else f"{name} ({k + 1})"
            out.append((key, i, x + ox, y + oy))
        return out

    def set_position_screen(self, index, sx, sy):
        ox, oy = self.client_origin()
        self.set_position(index, sx - ox, sy - oy)

    def redraw(self):
        user32.SendMessageW(self.listview, LVM_REDRAWITEMS, 0, max(0, self.count() - 1))
        user32.UpdateWindow(self.listview)

    def capture_layout(self):
        """Return {icon name: [x, y]} for saving."""
        layout = {}
        for name, x, y in self.get_icons():
            key = name
            n = 2
            while key in layout:  # keep duplicate names distinguishable
                key = f"{name} ({n})"
                n += 1
            layout[key] = [x, y]
        return layout
