"""tray.py - Windows system-tray icon for FaxDesk (stdlib ctypes; no PIL, no pystray). Left-click / 'Open FaxDesk' opens the
page; 'Quit FaxDesk' stops the server. On anything but Windows, run() returns at once and the caller serves in the foreground."""
import ctypes
import os
import sys
import webbrowser
from ctypes import wintypes

WM_DESTROY, WM_COMMAND, WM_USER = 0x0002, 0x0111, 0x0400
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
WM_TRAY = WM_USER + 20
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
IDI_APPLICATION = 32512
MF_STRING, MF_SEPARATOR = 0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
ID_OPEN, ID_QUIT = 1001, 1002

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def _log(state_dir, text):
    try:
        with open(os.path.join(state_dir, "tray.log"), "a", encoding="ascii") as f:
            f.write(text + "\n")
    except Exception:
        pass


def run(url, on_quit, tip="FaxDesk - click to open", state_dir="."):
    """Blocks in a Win32 message loop until Quit. Returns False immediately when not on Windows or the icon cannot be made
    (reason in <state>/tray.log)."""
    if os.name != "nt":
        return False
    u32, s32, k32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
    # 64-bit handles: without these, ctypes truncates HWND/HINSTANCE to 32 bits and everything fails quietly.
    k32.GetModuleHandleW.restype = wintypes.HMODULE
    k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    u32.DefWindowProcW.restype = ctypes.c_ssize_t
    u32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    u32.RegisterClassW.restype = wintypes.ATOM
    u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    u32.CreateWindowExW.restype = wintypes.HWND
    u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    u32.CreatePopupMenu.restype = wintypes.HMENU
    u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
    u32.TrackPopupMenu.restype = wintypes.BOOL
    u32.DestroyMenu.argtypes = [wintypes.HMENU]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.DestroyWindow.argtypes = [wintypes.HWND]
    u32.LoadIconW.restype = wintypes.HICON
    u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    s32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
    s32.Shell_NotifyIconW.restype = wintypes.BOOL
    s32.ExtractIconW.restype = wintypes.HICON
    s32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
    hinst = k32.GetModuleHandleW(None)
    nid = NOTIFYICONDATA()
    state = {"quit": False}

    def proc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = lparam & 0xFFFF
            if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                webbrowser.open(url)
            elif ev == WM_RBUTTONUP:
                menu = u32.CreatePopupMenu()
                u32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open FaxDesk")
                u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                u32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit FaxDesk")
                pt = wintypes.POINT()
                u32.GetCursorPos(ctypes.byref(pt))
                u32.SetForegroundWindow(hwnd)
                cmd = u32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
                u32.DestroyMenu(menu)
                if cmd == ID_OPEN:
                    webbrowser.open(url)
                elif cmd == ID_QUIT:
                    u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            s32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            state["quit"] = True
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(proc)
    wc = WNDCLASS()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.lpszClassName = "FaxDeskTray"
    if not u32.RegisterClassW(ctypes.byref(wc)):
        _log(state_dir, "RegisterClassW failed: %d" % k32.GetLastError())
        return False
    hwnd = u32.CreateWindowExW(0, "FaxDeskTray", "FaxDesk", 0, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        _log(state_dir, "CreateWindowExW failed: %d" % k32.GetLastError())
        return False
    icon = None
    try:
        exe = sys.executable if getattr(sys, "frozen", False) else None
        if exe:
            icon = s32.ExtractIconW(hinst, exe, 0)
    except Exception:
        icon = None
    if not icon or icon == 1:
        try:                                                   # running from source: the .ico shipped in www/
            ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www", "faxdesk.ico")
            u32.LoadImageW.restype = wintypes.HANDLE
            u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            icon = u32.LoadImageW(None, ico, 1, 0, 0, 0x00000010 | 0x00000040) if os.path.exists(ico) else None   # IMAGE_ICON, LR_LOADFROMFILE|LR_DEFAULTSIZE
        except Exception:
            icon = None
    if not icon or icon == 1:
        icon = u32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = icon
    nid.szTip = tip[:127]
    if not s32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        _log(state_dir, "Shell_NotifyIconW failed: %d" % k32.GetLastError())
        u32.DestroyWindow(hwnd)
        return False
    _log(state_dir, "tray icon shown")
    msg = wintypes.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u32.TranslateMessage(ctypes.byref(msg))
        u32.DispatchMessageW(ctypes.byref(msg))
    try:
        on_quit()
    except Exception:
        pass
    return True
