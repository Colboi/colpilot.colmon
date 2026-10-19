import ctypes
import ctypes.wintypes as wintypes
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


WINDOW_DEFINITIONS = (
    {
        'id': 'codex-weekly',
        'state_file': 'state.json',
        'title': 'Codex weekly',
        'kind': 'quota',
    },
    {
        'id': 'codex-five-hour',
        'state_file': 'codex-five-hour.state.json',
        'title': 'Codex 5hlimit',
        'kind': 'quota',
    },
    {
        'id': 'codex-tokens-today',
        'state_file': 'tokens-today.state.json',
        'title': 'Tokens today',
        'kind': 'count',
    },
)
WEEKLY_WINDOW_MINUTES = 7 * 24 * 60
FIVE_HOUR_WINDOW_MINUTES = 5 * 60
CODEX_QUERY_TIMEOUT_SECONDS = 20.0
WINDOW_WIDTH = 105
WINDOW_HEIGHT = 42
WINDOW_GAP = 4

WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_SETTINGCHANGE = 0x001A
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_DISPLAYCHANGE = 0x007E
WM_DPICHANGED = 0x02E0
WM_NCHITTEST = 0x0084
WM_MOUSEACTIVATE = 0x0021
WM_QUIT = 0x0012
WM_APP_INVALIDATE = 0x8000 + 1
HTCLIENT = 1
MA_NOACTIVATE = 3
WS_TABSTOP = 0x00010000
WS_CHILD = 0x40000000
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
WS_VISIBLE = 0x10000000
WS_HOST = WS_TABSTOP | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
WS_CONTENT = WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
WS_EX_CONTROLPARENT = 0x00010000
SW_SHOWNOACTIVATE = 4
HWND_TOP = wintypes.HWND(0)
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020
GWL_STYLE = -16
GWL_EXSTYLE = -20
LWA_ALPHA = 0x00000002
GA_PARENT = 1
DT_SINGLELINE = 0x20
DT_END_ELLIPSIS = 0x8000
DT_NOPREFIX = 0x800
TRANSPARENT = 1
DEFAULT_CHARSET = 1
OUT_DEFAULT_PRECIS = 0
CLIP_DEFAULT_PRECIS = 0
CLEARTYPE_QUALITY = 5
DEFAULT_PITCH = 0
FF_DONTCARE = 0
ERROR_CLASS_ALREADY_EXISTS = 1410
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
UINT_PTR = ctypes.c_size_t
COLORREF = ctypes.c_uint32
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(
    (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 4
)


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ('hdc', ctypes.c_void_p),
        ('fErase', wintypes.BOOL),
        ('rcPaint', wintypes.RECT),
        ('fRestore', wintypes.BOOL),
        ('fIncUpdate', wintypes.BOOL),
        ('rgbReserved', ctypes.c_byte * 32),
    ]


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', ctypes.c_void_p),
        ('hIcon', ctypes.c_void_p),
        ('hCursor', ctypes.c_void_p),
        ('hbrBackground', ctypes.c_void_p),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
    ]


if os.name == 'nt':
    USER32 = ctypes.WinDLL('user32', use_last_error=True)
    GDI32 = ctypes.WinDLL('gdi32', use_last_error=True)
    KERNEL32 = ctypes.WinDLL('kernel32', use_last_error=True)
else:
    USER32 = None
    GDI32 = None
    KERNEL32 = None


def _configure_win32() -> None:
    if USER32 is None or GDI32 is None or KERNEL32 is None:
        return
    USER32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    USER32.RegisterClassW.restype = wintypes.ATOM
    USER32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    USER32.CreateWindowExW.restype = wintypes.HWND
    USER32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    USER32.DefWindowProcW.restype = LRESULT
    USER32.DestroyWindow.argtypes = [wintypes.HWND]
    USER32.DestroyWindow.restype = wintypes.BOOL
    USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    USER32.ShowWindow.restype = wintypes.BOOL
    USER32.UpdateWindow.argtypes = [wintypes.HWND]
    USER32.UpdateWindow.restype = wintypes.BOOL
    USER32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    USER32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    USER32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    USER32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    USER32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND,
        COLORREF,
        wintypes.BYTE,
        wintypes.DWORD,
    ]
    USER32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    USER32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    USER32.PostMessageW.restype = wintypes.BOOL
    USER32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    USER32.GetMessageW.restype = ctypes.c_int
    USER32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    USER32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    USER32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    USER32.PostThreadMessageW.restype = wintypes.BOOL
    USER32.SetTimer.argtypes = [wintypes.HWND, UINT_PTR, wintypes.UINT, ctypes.c_void_p]
    USER32.SetTimer.restype = UINT_PTR
    USER32.KillTimer.argtypes = [wintypes.HWND, UINT_PTR]
    USER32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
    USER32.InvalidateRect.restype = wintypes.BOOL
    USER32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    USER32.BeginPaint.restype = ctypes.c_void_p
    USER32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    USER32.EndPaint.restype = wintypes.BOOL
    USER32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p]
    USER32.FillRect.restype = ctypes.c_int
    USER32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    USER32.FindWindowW.restype = wintypes.HWND
    USER32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    USER32.FindWindowExW.restype = wintypes.HWND
    USER32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    USER32.SetParent.restype = wintypes.HWND
    USER32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    USER32.GetAncestor.restype = wintypes.HWND
    USER32.IsWindow.argtypes = [wintypes.HWND]
    USER32.IsWindow.restype = wintypes.BOOL
    USER32.IsWindowVisible.argtypes = [wintypes.HWND]
    USER32.IsWindowVisible.restype = wintypes.BOOL
    USER32.MoveWindow.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
    ]
    USER32.MoveWindow.restype = wintypes.BOOL
    USER32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    USER32.SetWindowPos.restype = wintypes.BOOL
    USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    USER32.GetWindowRect.restype = wintypes.BOOL
    USER32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    USER32.GetClientRect.restype = wintypes.BOOL
    USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
    USER32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    USER32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    USER32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    USER32.RegisterWindowMessageW.restype = wintypes.UINT
    USER32.GetDpiForWindow.argtypes = [wintypes.HWND]
    USER32.GetDpiForWindow.restype = wintypes.UINT
    USER32.DrawTextW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
    USER32.DrawTextW.restype = ctypes.c_int
    GDI32.CreateSolidBrush.argtypes = [COLORREF]
    GDI32.CreateSolidBrush.restype = ctypes.c_void_p
    GDI32.CreateFontW.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.LPCWSTR,
    ]
    GDI32.CreateFontW.restype = ctypes.c_void_p
    GDI32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    GDI32.SelectObject.restype = ctypes.c_void_p
    GDI32.DeleteObject.argtypes = [ctypes.c_void_p]
    GDI32.DeleteObject.restype = wintypes.BOOL
    GDI32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    GDI32.SetTextColor.argtypes = [ctypes.c_void_p, COLORREF]
    KERNEL32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    KERNEL32.GetModuleHandleW.restype = ctypes.c_void_p
    KERNEL32.GetCurrentThreadId.restype = wintypes.DWORD


_configure_win32()


def _color(red: int, green: int, blue: int) -> int:
    return red | (green << 8) | (blue << 16)


def _handle_value(value: Any) -> int:
    return int(value or 0)


def _read_window_rect(handle: wintypes.HWND | None) -> tuple[int, int, int, int] | None:
    if handle is None or not USER32:
        return None
    rectangle = wintypes.RECT()
    if not USER32.GetWindowRect(handle, ctypes.byref(rectangle)):
        return None
    return (
        int(rectangle.left),
        int(rectangle.top),
        int(rectangle.right),
        int(rectangle.bottom),
    )


def _rect_intersects(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> bool:
    return (
        left[0] < right[2]
        and left[2] > right[0]
        and left[1] < right[3]
        and left[3] > right[1]
    )


def _rect_payload(rectangle: tuple[int, int, int, int] | None) -> dict[str, int] | None:
    if rectangle is None:
        return None
    left, top, right, bottom = rectangle
    return {
        'left': left,
        'top': top,
        'right': right,
        'bottom': bottom,
        'width': right - left,
        'height': bottom - top,
    }


_WINDOWS_BY_HANDLE: dict[int, 'NativeTaskbarWindow'] = {}
_WINDOW_CLASS_NAME = 'ColpilotColmonTaskbarWindow'
_WINDOW_CLASS_LOCK = threading.Lock()
_WINDOW_PROC: WNDPROC | None = None
_WINDOW_CLASS_REGISTERED = False


def _window_proc(hwnd: wintypes.HWND, message: int, wparam: int, lparam: int) -> int:
    window = _WINDOWS_BY_HANDLE.get(_handle_value(hwnd))
    if window is None:
        return USER32.DefWindowProcW(hwnd, message, wparam, lparam)
    return window.handle_message(message, wparam, lparam, hwnd)


@dataclass(frozen=True)
class MetricReading:
    value: str
    detail: str
    display: dict[str, object]
    source_text: str


class NativeTaskbarWindow:
    def __init__(
        self,
        controller: 'TaskbarWindowController',
        definition: dict[str, str],
        state_path: Path,
    ) -> None:
        self.controller = controller
        self.definition = definition
        self.state_path = state_path
        self.handle: wintypes.HWND | None = None
        self.content_handle: wintypes.HWND | None = None
        self.parent: wintypes.HWND | None = None
        self.attached = False
        self.lock = threading.RLock()
        self.title = definition['title']
        self.value = '--'
        self.detail = '等待数据'
        self.status = 'waiting'
        self.updated_at: str | None = None
        self.refresh_interval_seconds = controller.refresh_interval_seconds
        self.display: dict[str, object] = {}
        self.source_text = ''
        self.placement: dict[str, int] = {}
        self.taskbar_handle: int | None = None
        self.taskbar_rect: tuple[int, int, int, int] | None = None
        self.actual_rect: tuple[int, int, int, int] | None = None
        self.dpi = 96
        self.visible = False

    def create(self) -> None:
        handle = USER32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
            _WINDOW_CLASS_NAME,
            self.title,
            WS_HOST,
            0,
            0,
            WINDOW_WIDTH,
            WINDOW_HEIGHT,
            None,
            None,
            self.controller.hinstance,
            None,
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        _WINDOWS_BY_HANDLE[_handle_value(handle)] = self
        if not USER32.SetLayeredWindowAttributes(handle, 0, 255, LWA_ALPHA):
            _WINDOWS_BY_HANDLE.pop(_handle_value(handle), None)
            USER32.DestroyWindow(handle)
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        content_handle = USER32.CreateWindowExW(
            0,
            _WINDOW_CLASS_NAME,
            '',
            WS_CONTENT,
            0,
            0,
            WINDOW_WIDTH,
            WINDOW_HEIGHT,
            handle,
            None,
            self.controller.hinstance,
            None,
        )
        if not content_handle:
            _WINDOWS_BY_HANDLE.pop(_handle_value(handle), None)
            USER32.DestroyWindow(handle)
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        self.content_handle = content_handle
        _WINDOWS_BY_HANDLE[_handle_value(content_handle)] = self
        if not USER32.SetTimer(handle, 1, 1000, None):
            _WINDOWS_BY_HANDLE.pop(_handle_value(content_handle), None)
            _WINDOWS_BY_HANDLE.pop(_handle_value(handle), None)
            USER32.DestroyWindow(content_handle)
            USER32.DestroyWindow(handle)
            self.handle = None
            self.content_handle = None
            raise ctypes.WinError(ctypes.get_last_error())

    def destroy(self) -> None:
        if self.handle is None:
            return
        if self.content_handle is not None:
            _WINDOWS_BY_HANDLE.pop(_handle_value(self.content_handle), None)
            USER32.DestroyWindow(self.content_handle)
            self.content_handle = None
        _WINDOWS_BY_HANDLE.pop(_handle_value(self.handle), None)
        USER32.KillTimer(self.handle, 1)
        USER32.DestroyWindow(self.handle)
        self.handle = None
        self.parent = None
        self.attached = False
        self.taskbar_handle = None
        self.taskbar_rect = None
        self.actual_rect = None
        self.visible = False

    def set_reading(self, reading: MetricReading) -> None:
        with self.lock:
            self.value = reading.value
            self.detail = reading.detail
            self.display = dict(reading.display)
            self.source_text = reading.source_text
            self.status = 'ready'
            self.updated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        self.controller.write_window_state(self)
        self.invalidate()

    def set_error(self, detail: str) -> None:
        with self.lock:
            self.status = 'error'
            self.detail = detail
            self.updated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        self.controller.write_window_state(self)
        self.invalidate()

    def invalidate(self) -> None:
        target_handle = self.content_handle or self.handle
        if target_handle is not None:
            USER32.PostMessageW(target_handle, WM_APP_INVALIDATE, 0, 0)

    def handle_message(
        self,
        message: int,
        wparam: int,
        lparam: int,
        target_handle: wintypes.HWND,
    ) -> int:
        if message == WM_PAINT:
            self.paint(target_handle)
            return 0
        if message == WM_ERASEBKGND:
            return 1
        if message == WM_NCHITTEST:
            return HTCLIENT
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if message == WM_APP_INVALIDATE:
            USER32.InvalidateRect(target_handle, None, True)
            return 0
        if message == WM_TIMER:
            self.controller.place_windows()
            return 0
        if message in {
            self.controller.taskbar_created_message,
            WM_DISPLAYCHANGE,
            WM_SETTINGCHANGE,
            WM_DPICHANGED,
        }:
            self.controller.place_windows()
            return 0
        if message == WM_DESTROY:
            if _handle_value(target_handle) == _handle_value(self.handle):
                self.attached = False
            return 0
        return USER32.DefWindowProcW(target_handle, message, wparam, lparam)

    def paint(self, target_handle: wintypes.HWND) -> None:
        if self.handle is None or not target_handle:
            return
        paint_struct = PAINTSTRUCT()
        hdc = USER32.BeginPaint(target_handle, ctypes.byref(paint_struct))
        if not hdc:
            return
        try:
            rectangle = wintypes.RECT()
            if not USER32.GetClientRect(target_handle, ctypes.byref(rectangle)):
                return
            width = max(0, int(rectangle.right - rectangle.left))
            height = max(0, int(rectangle.bottom - rectangle.top))
            background = GDI32.CreateSolidBrush(_color(15, 23, 42))
            USER32.FillRect(hdc, ctypes.byref(rectangle), background)
            GDI32.DeleteObject(background)
            GDI32.SetBkMode(hdc, TRANSPARENT)
            with self.lock:
                title = self.title
                value = self.value
                detail = self.detail
            dpi = max(96, int(USER32.GetDpiForWindow(target_handle) or 96))
            scale = dpi / 96
            padding = max(4, int(7 * scale))
            title_height = max(11, int(height * 15 / WINDOW_HEIGHT))
            value_height = max(13, int(height * 18 / WINDOW_HEIGHT))
            detail_top = min(height, title_height + value_height)
            self._draw_text(
                hdc,
                title,
                padding,
                max(0, int(3 * scale)),
                max(padding, width - padding),
                min(height, title_height),
                _color(148, 163, 184),
                max(8, int(9 * scale)),
            )
            self._draw_text(
                hdc,
                value,
                padding,
                min(height, title_height),
                max(padding, width - padding),
                min(height, title_height + value_height),
                _color(248, 250, 252),
                max(10, int(13 * scale)),
                True,
            )
            self._draw_text(
                hdc,
                detail,
                padding,
                detail_top,
                max(padding, width - padding),
                height,
                _color(100, 116, 139),
                max(7, int(8 * scale)),
            )
        finally:
            USER32.EndPaint(target_handle, ctypes.byref(paint_struct))

    @staticmethod
    def _draw_text(
        hdc: ctypes.c_void_p,
        text: str,
        left: int,
        top: int,
        right: int,
        bottom: int,
        color: int,
        size: int,
        bold: bool = False,
    ) -> None:
        font = GDI32.CreateFontW(
            -size,
            0,
            0,
            0,
            700 if bold else 400,
            False,
            False,
            False,
            DEFAULT_CHARSET,
            OUT_DEFAULT_PRECIS,
            CLIP_DEFAULT_PRECIS,
            CLEARTYPE_QUALITY,
            DEFAULT_PITCH | FF_DONTCARE,
            'Segoe UI',
        )
        old_font = GDI32.SelectObject(hdc, font)
        GDI32.SetTextColor(hdc, color)
        rectangle = wintypes.RECT(left, top, right, bottom)
        USER32.DrawTextW(
            hdc,
            text,
            -1,
            ctypes.byref(rectangle),
            DT_SINGLELINE | DT_END_ELLIPSIS | DT_NOPREFIX,
        )
        GDI32.SelectObject(hdc, old_font)
        GDI32.DeleteObject(font)


class TaskbarWindowController:
    def __init__(
        self,
        artifact_directory: Path,
        refresh_interval_seconds: int,
        process_registry: Any,
    ) -> None:
        self.artifact_directory = artifact_directory
        self.refresh_interval_seconds = refresh_interval_seconds
        self.process_registry = process_registry
        self.owner_prefix = 'colmon:'
        self.stop_event = threading.Event()
        self.state_lock = threading.RLock()
        self.state_file_lock = threading.RLock()
        self.ready_event = threading.Event()
        self.ui_thread: threading.Thread | None = None
        self.poll_thread: threading.Thread | None = None
        self.ui_thread_id: int | None = None
        self.hinstance: ctypes.c_void_p | None = None
        self.taskbar_created_message = 0
        self.windows: dict[str, NativeTaskbarWindow] = {}
        self.last_error: str | None = None

    def start(self) -> None:
        if os.name != 'nt':
            raise RuntimeError('colmon 任务栏窗口只支持 Windows')
        self._enable_process_dpi_awareness()
        with self.state_lock:
            if self.is_running():
                return
            self.stop_event.clear()
            self.ready_event.clear()
            self.last_error = None
            self.ui_thread = threading.Thread(target=self._run_ui, name='colmon-taskbar-ui')
            self.poll_thread = threading.Thread(target=self._run_poll, name='colmon-taskbar-poll')
            self.ui_thread.start()
        if not self.ready_event.wait(timeout=10):
            self.stop()
            raise RuntimeError('Colpilot 任务栏窗口线程启动超时')
        with self.state_lock:
            poll_thread = self.poll_thread
            last_error = self.last_error
            windows_created = len(self.windows)
        if last_error is not None or windows_created != len(WINDOW_DEFINITIONS):
            self.stop()
            raise RuntimeError(last_error or 'Colpilot 任务栏窗口创建失败')
        if poll_thread is not None:
            poll_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.process_registry.stop_owner(self.owner_prefix)
        with self.state_lock:
            ui_thread = self.ui_thread
            poll_thread = self.poll_thread
            ui_thread_id = self.ui_thread_id
        if ui_thread_id is not None and USER32 is not None:
            USER32.PostThreadMessageW(ui_thread_id, WM_QUIT, 0, 0)
        current_thread = threading.current_thread()
        if poll_thread is not None and poll_thread is not current_thread:
            poll_thread.join(timeout=10)
        if ui_thread is not None and ui_thread is not current_thread:
            ui_thread.join(timeout=10)
        with self.state_lock:
            self.ui_thread = None
            self.poll_thread = None
            self.ui_thread_id = None
            self.windows.clear()

    def is_running(self) -> bool:
        with self.state_lock:
            return any(
                thread is not None and thread.is_alive()
                for thread in (self.ui_thread, self.poll_thread)
            )

    def runtime_status(self) -> dict[str, object]:
        with self.state_lock:
            last_error = self.last_error
        if last_error is not None:
            return {'status': 'error', 'detail': last_error, 'exit_code': None}
        if self.is_running():
            with self.state_lock:
                windows = tuple(self.windows.values())
            invalid_windows = []
            for window in windows:
                handle = window.handle
                if (
                    handle is None
                    or window.content_handle is None
                    or not USER32.IsWindow(handle)
                    or not USER32.IsWindow(window.content_handle)
                    or not USER32.IsWindowVisible(handle)
                    or not USER32.IsWindowVisible(window.content_handle)
                    or not window.attached
                ):
                    invalid_windows.append(window.definition['id'])
            if invalid_windows:
                detail = f"任务栏窗口状态异常: {', '.join(invalid_windows)}"
                return {'status': 'error', 'detail': detail, 'exit_code': None}
            return {
                'status': 'running',
                'detail': 'Colpilot 插件正在管理三个任务栏窗口',
                'exit_code': None,
            }
        return {'status': 'stopped', 'detail': '任务栏窗口已经停止', 'exit_code': None}

    def write_window_state(self, window: NativeTaskbarWindow) -> None:
        with self.state_file_lock:
            with window.lock:
                timestamp = window.updated_at or datetime.now().astimezone().isoformat(timespec='seconds')
                payload = {
                    'timestamp': timestamp,
                    'reason': 'data-change',
                    'attached': window.attached,
                    'processId': os.getpid(),
                    'windowId': window.definition['id'],
                    'sourceText': window.source_text,
                    'display': {
                        'title': window.title,
                        **window.display,
                    },
                    'status': window.status,
                    'value': window.value,
                    'detail': window.detail,
                    'refreshIntervalSeconds': window.refresh_interval_seconds,
                    'placement': dict(window.placement),
                    'handles': {
                        'app': f'0x{_handle_value(window.handle):X}' if window.handle else None,
                        'content': f'0x{_handle_value(window.content_handle):X}' if window.content_handle else None,
                        'taskbar': f'0x{window.taskbar_handle:X}' if window.taskbar_handle else None,
                        'parent': f'0x{_handle_value(window.parent):X}' if window.parent else None,
                    },
                    'visible': window.visible,
                    'dpi': window.dpi,
                    'rectangles': {
                        'app': _rect_payload(window.actual_rect),
                        'taskbar': _rect_payload(window.taskbar_rect),
                    },
                }
            self.artifact_directory.mkdir(parents=True, exist_ok=True)
            path = window.state_path
            temporary_path = path.with_suffix(path.suffix + '.tmp')
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
            temporary_path.replace(path)

    def _enable_process_dpi_awareness(self) -> None:
        if USER32 is None:
            raise RuntimeError('Windows 图形接口不可用')
        if not USER32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def _external_taskbar_windows(
        self,
        taskbar: wintypes.HWND,
        taskbar_rect: tuple[int, int, int, int],
    ) -> list[tuple[int, int, int, int]]:
        explorer_process_id = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(taskbar, ctypes.byref(explorer_process_id))
        own_handles = {
            _handle_value(window.handle)
            for window in self.windows.values()
            if window.handle is not None
        }
        occupied_rectangles: list[tuple[int, int, int, int]] = []
        child: wintypes.HWND | None = None
        while True:
            child = USER32.FindWindowExW(taskbar, child, None, None)
            if not child:
                break
            child_value = _handle_value(child)
            if child_value in own_handles or not USER32.IsWindowVisible(child):
                continue
            process_id = wintypes.DWORD()
            USER32.GetWindowThreadProcessId(child, ctypes.byref(process_id))
            if process_id.value in {0, explorer_process_id.value, os.getpid()}:
                continue
            rectangle = _read_window_rect(child)
            if rectangle is None or not _rect_intersects(rectangle, taskbar_rect):
                continue
            occupied_rectangles.append(rectangle)
        return occupied_rectangles

    @staticmethod
    def _find_available_x(
        taskbar_left: int,
        taskbar_width: int,
        notification_left: int,
        width: int,
        gap: int,
        occupied_rectangles: list[tuple[int, int, int, int]],
    ) -> int:
        relative_rectangles = [
            (left - taskbar_left, right - taskbar_left)
            for left, _, right, _ in occupied_rectangles
        ]
        right = notification_left - taskbar_left - gap
        for _ in range(len(relative_rectangles) + 1):
            left = right - width
            conflicts = [
                rectangle
                for rectangle in relative_rectangles
                if left < rectangle[1] and right > rectangle[0]
            ]
            conflict = max(conflicts, key=lambda rectangle: rectangle[0], default=None)
            if conflict is None:
                return max(0, min(left, max(0, taskbar_width - width)))
            right = conflict[0] - gap
        return max(0, min(right - width, max(0, taskbar_width - width)))

    def place_windows(self) -> None:
        if USER32 is None or not self.windows:
            return
        taskbar = USER32.FindWindowW('Shell_TrayWnd', None)
        if not taskbar:
            return
        taskbar_rect = _read_window_rect(taskbar)
        if taskbar_rect is None:
            return
        notify_area = USER32.FindWindowExW(taskbar, None, 'TrayNotifyWnd', None)
        notify_rect = _read_window_rect(notify_area)
        notify_left = notify_rect[0] if notify_rect is not None else taskbar_rect[2]
        dpi = max(96, USER32.GetDpiForWindow(taskbar) or 96)
        scale = dpi / 96
        width = max(1, int(WINDOW_WIDTH * scale))
        height = max(1, int(WINDOW_HEIGHT * scale))
        gap = max(1, int(WINDOW_GAP * scale))
        taskbar_width = max(0, taskbar_rect[2] - taskbar_rect[0])
        taskbar_height = max(0, taskbar_rect[3] - taskbar_rect[1])
        occupied_rectangles = self._external_taskbar_windows(taskbar, taskbar_rect)
        anchor_x = self._find_available_x(
            taskbar_rect[0],
            taskbar_width,
            notify_left,
            width,
            gap,
            occupied_rectangles,
        )
        for index, window in enumerate(self.windows.values()):
            if window.handle is None:
                continue
            if _handle_value(USER32.GetAncestor(window.handle, GA_PARENT)) != _handle_value(taskbar):
                USER32.SetParent(window.handle, taskbar)
                parent = USER32.GetAncestor(window.handle, GA_PARENT)
                if _handle_value(parent) != _handle_value(taskbar):
                    raise ctypes.WinError(ctypes.get_last_error())
            target_style = WS_VISIBLE | WS_HOST
            if USER32.GetWindowLongPtrW(window.handle, GWL_STYLE) != target_style:
                USER32.SetWindowLongPtrW(window.handle, GWL_STYLE, target_style)
                if USER32.GetWindowLongPtrW(window.handle, GWL_STYLE) != target_style:
                    raise RuntimeError(f"任务栏窗口样式设置失败: {window.definition['id']}")
            target_ex_style = (
                WS_EX_TOOLWINDOW
                | WS_EX_NOACTIVATE
                | WS_EX_LAYERED
                | WS_EX_CONTROLPARENT
            )
            if USER32.GetWindowLongPtrW(window.handle, GWL_EXSTYLE) != target_ex_style:
                USER32.SetWindowLongPtrW(window.handle, GWL_EXSTYLE, target_ex_style)
                if USER32.GetWindowLongPtrW(window.handle, GWL_EXSTYLE) != target_ex_style:
                    raise RuntimeError(f"任务栏窗口扩展样式设置失败: {window.definition['id']}")
            x = anchor_x - index * (width + gap)
            x = max(0, min(x, max(0, taskbar_width - width)))
            y = max(0, (taskbar_height - height) // 2)
            if not USER32.SetWindowPos(
                window.handle,
                HWND_TOP,
                x,
                y,
                width,
                height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if window.content_handle is None or not USER32.SetWindowPos(
                window.content_handle,
                HWND_TOP,
                0,
                0,
                width,
                height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            USER32.ShowWindow(window.handle, SW_SHOWNOACTIVATE)
            USER32.ShowWindow(window.content_handle, SW_SHOWNOACTIVATE)
            USER32.UpdateWindow(window.handle)
            USER32.UpdateWindow(window.content_handle)
            actual_rect = _read_window_rect(window.handle)
            parent = USER32.GetAncestor(window.handle, GA_PARENT)
            if (
                not USER32.IsWindow(window.handle)
                or not USER32.IsWindow(window.content_handle)
                or not USER32.IsWindowVisible(window.handle)
                or not USER32.IsWindowVisible(window.content_handle)
                or _handle_value(parent) != _handle_value(taskbar)
                or actual_rect is None
                or not _rect_intersects(actual_rect, taskbar_rect)
            ):
                raise RuntimeError(f"任务栏窗口附着失败: {window.definition['id']}")
            with window.lock:
                window.parent = parent
                window.attached = True
                window.taskbar_handle = _handle_value(taskbar)
                window.taskbar_rect = taskbar_rect
                window.actual_rect = actual_rect
                window.dpi = dpi
                window.visible = True
                window.placement = {'x': x, 'y': y, 'width': width, 'height': height}
            window.invalidate()
            self.write_window_state(window)

    def _run_ui(self) -> None:
        try:
            self.ui_thread_id = KERNEL32.GetCurrentThreadId()
            self._initialize_window_class()
            self.taskbar_created_message = USER32.RegisterWindowMessageW('TaskbarCreated')
            for definition in WINDOW_DEFINITIONS:
                state_path = self.artifact_directory / definition['state_file']
                window = NativeTaskbarWindow(self, definition, state_path)
                window.create()
                self.windows[definition['id']] = window
            self.place_windows()
            self.ready_event.set()
            message = wintypes.MSG()
            while not self.stop_event.is_set():
                result = USER32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                USER32.TranslateMessage(ctypes.byref(message))
                USER32.DispatchMessageW(ctypes.byref(message))
        except Exception as error:
            with self.state_lock:
                self.last_error = str(error)
            self.stop_event.set()
            self.ready_event.set()
        finally:
            for window in list(self.windows.values()):
                window.destroy()
            self.windows.clear()

    def _run_poll(self) -> None:
        quota_reader = CodexQuotaReader(self.process_registry, self.stop_event)
        token_reader = TokenTodayReader(self.stop_event)
        next_poll = 0.0
        while not self.stop_event.is_set():
            wait_seconds = next_poll - time.monotonic()
            if wait_seconds > 0 and self.stop_event.wait(wait_seconds):
                return
            started_at = time.monotonic()
            self._read_window('codex-weekly', lambda: quota_reader.read(WEEKLY_WINDOW_MINUTES))
            if self.stop_event.is_set():
                return
            self._read_window('codex-five-hour', lambda: quota_reader.read(FIVE_HOUR_WINDOW_MINUTES))
            if self.stop_event.is_set():
                return
            self._read_window('codex-tokens-today', token_reader.read)
            next_poll = started_at + self.refresh_interval_seconds

    def _read_window(self, window_id: str, reader: Any) -> None:
        window = self.windows.get(window_id)
        if window is None:
            return
        try:
            reading = reader()
            window.set_reading(reading)
            with self.state_lock:
                self.last_error = None
        except Exception as error:
            if self.stop_event.is_set():
                return
            detail = str(error)
            window.set_error(detail)
            with self.state_lock:
                self.last_error = detail

    def _initialize_window_class(self) -> None:
        global _WINDOW_PROC, _WINDOW_CLASS_REGISTERED
        if USER32 is None or GDI32 is None:
            raise RuntimeError('Windows 图形接口不可用')
        self.hinstance = KERNEL32.GetModuleHandleW(None)
        with _WINDOW_CLASS_LOCK:
            if _WINDOW_CLASS_REGISTERED:
                return
            _WINDOW_PROC = WNDPROC(_window_proc)
            window_class = WNDCLASSW(
                0,
                _WINDOW_PROC,
                0,
                0,
                self.hinstance,
                None,
                None,
                None,
                None,
                _WINDOW_CLASS_NAME,
            )
            atom = USER32.RegisterClassW(ctypes.byref(window_class))
            if not atom and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(ctypes.get_last_error())
            _WINDOW_CLASS_REGISTERED = True


class CodexQuotaReader:
    def __init__(self, process_registry: Any, stop_event: threading.Event) -> None:
        self.process_registry = process_registry
        self.stop_event = stop_event

    def read(self, target_window_minutes: int) -> MetricReading:
        result = self._read_result()
        reading = self._parse_quota(result, target_window_minutes)
        return self._to_metric_reading(reading, target_window_minutes)

    def read_windows(self, target_window_minutes: tuple[int, ...]) -> dict[int, MetricReading]:
        result = self._read_result()
        return {
            window_minutes: self._to_metric_reading(
                self._parse_quota(result, window_minutes),
                window_minutes,
            )
            for window_minutes in target_window_minutes
        }

    def _read_result(self) -> dict[str, object]:
        if self.stop_event.is_set():
            raise RuntimeError('Colpilot 正在停止')
        candidates = self._command_candidates()
        if not candidates:
            raise FileNotFoundError('没有找到 Codex App Server 可执行文件')
        last_error: Exception | None = None
        configured = bool(os.environ.get('COLMON_CODEX_COMMAND', '').strip())
        for candidate in candidates:
            if self.stop_event.is_set():
                raise RuntimeError('Colpilot 正在停止')
            for approval_policy in ('never', 'untrusted'):
                if self.stop_event.is_set():
                    raise RuntimeError('Colpilot 正在停止')
                try:
                    result = self._read_once(candidate, approval_policy)
                    return result
                except Exception as error:
                    last_error = error
                    if not (
                        approval_policy == 'never'
                        and 'approval' in str(error).lower()
                    ):
                        break
            if configured:
                break
        raise RuntimeError(f'Codex 配额读取失败：{last_error}') from last_error

    def _read_once(self, command: str, approval_policy: str) -> dict[str, object]:
        if self.stop_event.is_set():
            raise RuntimeError('Colpilot 正在停止')
        is_script = command.lower().endswith(('.cmd', '.bat'))
        arguments = ['-s', 'read-only', '-a', approval_policy, 'app-server']
        if is_script:
            command_line = ' '.join(self._quote_windows_argument(value) for value in arguments)
            executable = os.environ.get('COMSPEC', 'cmd.exe')
            process_arguments = [executable, '/d', '/s', '/c', f'"{command}" {command_line}']
        else:
            process_arguments = [command, *arguments]
        process = subprocess.Popen(
            process_arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        self.process_registry.register('colmon:codex-app-server', process)
        output_queue: queue.Queue[str | None] = queue.Queue()
        stdout_thread = threading.Thread(
            target=self._read_output,
            args=(process.stdout, output_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._drain_output,
            args=(process.stderr,),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            self._send(process, {'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'colpilot-colmon', 'title': 'Colpilot', 'version': '0.1.0'},
            }})
            self._read_response(output_queue, 1)
            self._send(process, {'method': 'initialized', 'params': {}})
            self._send(process, {'id': 2, 'method': 'account/rateLimits/read'})
            result = self._read_response(output_queue, 2)
            try:
                self._parse_quota(result, WEEKLY_WINDOW_MINUTES)
            except ValueError:
                self._send(process, {'id': 3, 'method': 'account/read', 'params': {}})
                account_result = self._read_response(output_queue, 3)
                if not self._should_retry_empty_quota(result, account_result):
                    raise
                if self.stop_event.wait(0.3):
                    raise RuntimeError('Colpilot 正在停止')
                self._send(process, {'id': 4, 'method': 'account/rateLimits/read'})
                result = self._read_response(output_queue, 4)
            return result
        finally:
            self.process_registry.stop_process(process)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

    def _read_response(self, output_queue: queue.Queue[str | None], response_id: int) -> dict[str, object]:
        deadline = time.monotonic() + CODEX_QUERY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise RuntimeError('Colpilot 正在停止')
            try:
                line = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError('Codex App Server 在返回结果前退出')
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get('id') != response_id:
                continue
            if isinstance(message.get('error'), dict):
                error = message['error']
                raise RuntimeError(str(error.get('message') or 'Codex App Server 返回了错误'))
            result = message.get('result')
            if not isinstance(result, dict):
                raise ValueError('Codex App Server 返回的数据格式无效')
            return result
        raise TimeoutError('Codex App Server 响应超时')

    @staticmethod
    def _read_output(stream: Any, output_queue: queue.Queue[str | None]) -> None:
        if stream is None:
            output_queue.put(None)
            return
        for line in iter(stream.readline, ''):
            output_queue.put(line)
        output_queue.put(None)

    @staticmethod
    def _drain_output(stream: Any) -> None:
        if stream is not None:
            for _ in iter(stream.readline, ''):
                pass

    @staticmethod
    def _send(process: subprocess.Popen[Any], message: dict[str, object]) -> None:
        if process.stdin is None:
            raise RuntimeError('Codex App Server 输入流不可用')
        process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        process.stdin.flush()

    @staticmethod
    def _quote_windows_argument(value: str) -> str:
        return '"' + value.replace('"', '\\"') + '"'

    @staticmethod
    def _command_candidates() -> list[str]:
        configured = os.environ.get('COLMON_CODEX_COMMAND', '').strip()
        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        local_app_data = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        codex_bin_root = local_app_data / 'OpenAI' / 'Codex' / 'bin'
        candidates.extend([
            str(local_app_data / 'OpenAI' / 'Codex' / 'bin' / 'codex.exe'),
            str(local_app_data / 'Programs' / 'Codex' / 'resources' / 'codex.exe'),
            str(local_app_data / 'Microsoft' / 'WindowsApps' / 'codex.exe'),
            str(Path.home() / 'AppData' / 'Roaming' / 'npm' / 'codex.cmd'),
        ])
        versioned_commands = list(codex_bin_root.glob('*/codex.exe'))
        versioned_commands.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        candidates.extend(str(path) for path in versioned_commands)
        for name in ('codex.exe', 'codex.cmd', 'codex'):
            located = shutil.which(name)
            if located:
                candidates.append(located)
        result: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            path = Path(candidate)
            if path.is_absolute() and not path.is_file():
                continue
            key = str(path).lower()
            if key not in seen:
                seen.add(key)
                result.append(candidate)
        return result

    @classmethod
    def _parse_quota(cls, result: dict[str, object], target_window_minutes: int) -> dict[str, object]:
        snapshot = cls._select_snapshot(result)
        window = cls._select_window(snapshot, target_window_minutes)
        used = cls._number(window, 'usedPercent', 'used_percent')
        if used is None:
            remaining = cls._number(window, 'remainingPercent', 'remaining_percent')
            if remaining is None:
                raise ValueError('Codex 配额数据缺少 usedPercent')
            used = 100 - remaining
        remaining = max(0.0, min(100.0, 100 - used))
        return {
            'remaining': remaining,
            'used': used,
            'reset_at': cls._text(window, 'resetsAt', 'resets_at'),
            'duration': cls._number(window, 'windowDurationMins', 'window_duration_mins') or 0,
        }

    @staticmethod
    def _select_snapshot(result: dict[str, object]) -> dict[str, object]:
        by_id = CodexQuotaReader._property(result, 'rateLimitsByLimitId', 'rate_limits_by_limit_id')
        if isinstance(by_id, dict):
            codex = by_id.get('codex')
            if isinstance(codex, dict) and CodexQuotaReader._has_windows(codex):
                return codex
        direct = CodexQuotaReader._property(result, 'rateLimits', 'rate_limits')
        if isinstance(direct, dict) and CodexQuotaReader._has_windows(direct):
            return direct
        if isinstance(by_id, dict):
            alternatives = [value for value in by_id.values() if isinstance(value, dict) and CodexQuotaReader._has_windows(value)]
            if len(alternatives) == 1:
                return alternatives[0]
        raise ValueError('Codex 配额数据没有明确的窗口')

    @staticmethod
    def _select_window(snapshot: dict[str, object], target_window_minutes: int) -> dict[str, object]:
        secondary: dict[str, object] | None = None
        for name in ('primary', 'secondary'):
            value = snapshot.get(name)
            if not isinstance(value, dict):
                continue
            if name == 'secondary':
                secondary = value
            duration = CodexQuotaReader._number(value, 'windowDurationMins', 'window_duration_mins')
            if duration is None:
                continue
            if (
                target_window_minutes == WEEKLY_WINDOW_MINUTES and duration >= WEEKLY_WINDOW_MINUTES
            ) or duration == target_window_minutes:
                return value
        if target_window_minutes == WEEKLY_WINDOW_MINUTES and secondary is not None:
            return secondary
        raise ValueError(f'Codex 配额数据没有 {target_window_minutes} 分钟窗口')

    @staticmethod
    def _has_windows(value: dict[str, object]) -> bool:
        return isinstance(value.get('primary'), dict) or isinstance(value.get('secondary'), dict)

    @staticmethod
    def _property(value: dict[str, object], *names: str) -> object:
        for name in names:
            if name in value:
                return value[name]
        return None

    @staticmethod
    def _number(value: dict[str, object], *names: str) -> float | None:
        item = CodexQuotaReader._property(value, *names)
        if isinstance(item, bool):
            return None
        if isinstance(item, int | float):
            return float(item)
        if isinstance(item, str):
            try:
                return float(item)
            except ValueError:
                return None
        return None

    @staticmethod
    def _text(value: dict[str, object], *names: str) -> str:
        item = CodexQuotaReader._property(value, *names)
        return str(item) if item is not None else ''

    @classmethod
    def _should_retry_empty_quota(cls, result: dict[str, object], account: dict[str, object]) -> bool:
        try:
            cls._select_snapshot(result)
            return False
        except ValueError:
            account_value = account.get('account')
            return isinstance(account_value, dict) and any(
                str(account_value.get(name) or '').strip()
                for name in ('id', 'email', 'planType', 'plan_type', 'type')
            )

    @staticmethod
    def _to_metric_reading(reading: dict[str, object], target_window_minutes: int) -> MetricReading:
        remaining = float(reading['remaining'])
        reset_at = str(reading.get('reset_at') or '')
        reset_text = CodexQuotaReader._format_reset(reset_at)
        window_text = '5 小时' if target_window_minutes == FIVE_HOUR_WINDOW_MINUTES else '7 天'
        return MetricReading(
            value=f'{remaining:.3g}%',
            detail=f'剩余时间：{reset_text}' if reset_text != '--' else f'{window_text}窗口暂时没有重置时间',
            display={
                'percentageText': f'{remaining:.3g}%',
                'resetRemainingText': reset_text,
                'resetAt': reset_at,
                'windowDurationMinutes': target_window_minutes,
            },
            source_text=f'{remaining:.3g}%',
        )

    @staticmethod
    def _format_reset(value: str) -> str:
        if not value or value.lower() == 'null':
            return '--'
        timestamp: datetime | None = None
        try:
            numeric = int(value)
            timestamp = datetime.fromtimestamp(
                numeric / 1000 if numeric >= 1_000_000_000_000 else numeric,
                tz=timezone.utc,
            ).astimezone()
        except ValueError:
            try:
                timestamp = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
            except ValueError:
                return '--'
        seconds = max(0, int((timestamp - datetime.now().astimezone()).total_seconds() + 59) // 60 * 60)
        total_minutes = max(1, (seconds + 59) // 60)
        days, remainder = divmod(total_minutes, 24 * 60)
        hours, minutes = divmod(remainder, 60)
        if days:
            return f'{days}d {hours}h' if hours else f'{days}d'
        if hours:
            return f'{hours}h {minutes}m' if minutes else f'{hours}h'
        return f'{minutes}m'


class TokenTodayReader:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event

    def read(self) -> MetricReading:
        codex_home = os.environ.get('CODEX_HOME', '').strip()
        root = Path(codex_home).expanduser() if codex_home else Path.home() / '.codex'
        sessions = root / 'sessions'
        if not sessions.is_dir():
            raise FileNotFoundError(f'Codex sessions 目录不存在：{sessions}')
        total = 0
        for path in sessions.rglob('*.jsonl'):
            if self.stop_event.is_set():
                raise RuntimeError('Colpilot 正在停止')
            total += self._read_file(path)
        value = str(total)
        return MetricReading(
            value=value,
            detail='Codex 当日 token 总量',
            display={'numberText': value},
            source_text=value,
        )

    def _read_file(self, path: Path) -> int:
        total = 0
        with path.open('r', encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if self.stop_event.is_set():
                    raise RuntimeError('Colpilot 正在停止')
                if 'token_count' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = self._parse_event(event)
                if value is not None:
                    total += value
        return total

    @staticmethod
    def _parse_event(event: object) -> int | None:
        if not isinstance(event, dict) or event.get('type') != 'event_msg':
            return None
        timestamp = event.get('timestamp')
        if not isinstance(timestamp, str):
            return None
        try:
            event_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone().date()
        except ValueError:
            return None
        if event_date != date.today():
            return None
        payload = event.get('payload')
        if not isinstance(payload, dict) or payload.get('type') != 'token_count':
            return None
        info = payload.get('info')
        usage = info.get('last_token_usage') if isinstance(info, dict) else None
        if not isinstance(usage, dict):
            usage = info.get('lastTokenUsage') if isinstance(info, dict) else None
        if not isinstance(usage, dict):
            return None
        total = usage.get('total_tokens', usage.get('totalTokens'))
        if isinstance(total, bool):
            return None
        if isinstance(total, int):
            return max(0, total)
        if isinstance(total, str):
            try:
                return max(0, int(total))
            except ValueError:
                return None
        return None
