import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .taskbar import (
    CodexQuotaReader,
    FIVE_HOUR_WINDOW_MINUTES,
    MetricReading,
    TokenTodayReader,
    WEEKLY_WINDOW_MINUTES,
    WINDOW_DEFINITIONS,
)


CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)


class ManagedTaskbarWindow:
    def __init__(
        self,
        controller: 'TaskbarWindowController',
        definition: dict[str, str],
        state_path: Path,
    ) -> None:
        self.controller = controller
        self.definition = definition
        self.state_path = state_path
        self.lock = threading.RLock()
        self.title = definition['title']
        self.value = '--'
        self.detail = '等待数据'
        self.status = 'waiting'
        self.updated_at: str | None = None
        self.refresh_interval_seconds = controller.refresh_interval_seconds
        self.display: dict[str, object] = {}
        self.source_text = ''
        self.attached = False
        self.visible = False
        self.dpi = 96
        self.placement: dict[str, object] = {}
        self.handles: dict[str, object] = {}
        self.rectangles: dict[str, object] = {}

    def set_reading(self, reading: MetricReading) -> None:
        with self.lock:
            self.value = reading.value
            self.detail = reading.detail
            self.display = dict(reading.display)
            self.source_text = reading.source_text
            self.status = 'ready'
            self.updated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        self.controller.write_window_state(self)
        self.controller.send_window_state(self)

    def set_error(self, detail: str) -> None:
        with self.lock:
            self.status = 'error'
            self.detail = detail
            self.updated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        self.controller.write_window_state(self)
        self.controller.send_window_state(self)


class TaskbarWindowController:
    def __init__(
        self,
        plugin_root: Path,
        artifact_directory: Path,
        refresh_interval_seconds: int,
        process_registry: Any,
    ) -> None:
        self.plugin_root = plugin_root
        self.artifact_directory = artifact_directory
        self.refresh_interval_seconds = refresh_interval_seconds
        self.process_registry = process_registry
        self.owner_prefix = 'colmon:'
        self.stop_event = threading.Event()
        self.state_lock = threading.RLock()
        self.state_file_lock = threading.RLock()
        self.command_lock = threading.RLock()
        self.ready_event = threading.Event()
        self.windows_ready_event = threading.Event()
        self.helper_ready = False
        self.helper_process: subprocess.Popen[str] | None = None
        self.helper_stdout_thread: threading.Thread | None = None
        self.helper_stderr_thread: threading.Thread | None = None
        self.poll_thread: threading.Thread | None = None
        self.windows: dict[str, ManagedTaskbarWindow] = {}
        self.last_error: str | None = None

        for definition in WINDOW_DEFINITIONS:
            state_path = self.artifact_directory / definition['state_file']
            self.windows[definition['id']] = ManagedTaskbarWindow(self, definition, state_path)

    @property
    def helper_pid(self) -> int | None:
        with self.state_lock:
            process = self.helper_process
        return process.pid if process is not None else None

    def start(self) -> None:
        if os.name != 'nt':
            raise RuntimeError('colmon 任务栏窗口只支持 Windows')
        with self.state_lock:
            if self.is_running():
                return
            self.stop_event.clear()
            self.ready_event.clear()
            self.windows_ready_event.clear()
            self.helper_ready = False
            self.last_error = None

        command = self._helper_command()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        self.process_registry.register('colmon:window-host', process)
        with self.state_lock:
            self.helper_process = process

        self.helper_stdout_thread = threading.Thread(
            target=self._read_helper_output,
            args=(process,),
            name='colmon-native-window-output',
        )
        self.helper_stderr_thread = threading.Thread(
            target=self._drain_helper_error,
            args=(process,),
            name='colmon-native-window-error',
        )
        self.helper_stdout_thread.start()
        self.helper_stderr_thread.start()

        if not self.ready_event.wait(timeout=15):
            self.stop()
            raise RuntimeError('colmon 原生窗口宿主启动超时')

        with self.state_lock:
            last_error = self.last_error
            helper_ready = self.helper_ready
            process_alive = process.poll() is None
        if last_error is not None or not helper_ready or not process_alive:
            self.stop()
            raise RuntimeError(last_error or 'colmon 原生窗口宿主启动失败')
        if not self.windows_ready_event.wait(timeout=5):
            self.stop()
            raise RuntimeError('colmon 原生窗口宿主的任务栏窗口状态同步超时')

        self._send_all_states()
        self.poll_thread = threading.Thread(
            target=self._run_poll,
            name='colmon-taskbar-poll',
        )
        self.poll_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.process_registry.stop_owner('colmon:codex-app-server', timeout_seconds=2.0)
        with self.state_lock:
            process = self.helper_process
            poll_thread = self.poll_thread
            stdout_thread = self.helper_stdout_thread
            stderr_thread = self.helper_stderr_thread

        if process is not None:
            self._send_command({'type': 'shutdown'})
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process_registry.stop_process(process, timeout_seconds=1.0)
            else:
                self.process_registry.unregister(process)

        current_thread = threading.current_thread()
        join_deadline = time.monotonic() + 2.0
        for thread in (poll_thread, stdout_thread, stderr_thread):
            if thread is not None and thread is not current_thread:
                thread.join(timeout=max(0.0, join_deadline - time.monotonic()))

        with self.state_lock:
            self.helper_process = None
            self.helper_stdout_thread = None
            self.helper_stderr_thread = None
            self.poll_thread = None
            self.helper_ready = False

        for window in self.windows.values():
            with window.lock:
                window.attached = False
                window.visible = False
            self.write_window_state(window)

    def is_running(self) -> bool:
        with self.state_lock:
            process = self.helper_process
            poll_thread = self.poll_thread
            return (
                process is not None
                and process.poll() is None
                and self.helper_ready
                and poll_thread is not None
                and poll_thread.is_alive()
                and not self.stop_event.is_set()
            )

    def runtime_status(self) -> dict[str, object]:
        with self.state_lock:
            last_error = self.last_error
            helper_ready = self.helper_ready
            process = self.helper_process
        if last_error is not None:
            return {'status': 'error', 'detail': last_error, 'exit_code': None}
        if process is not None and process.poll() is not None:
            return {
                'status': 'error',
                'detail': f'colmon 原生窗口宿主已退出，退出代码：{process.returncode}',
                'exit_code': process.returncode,
            }
        if process is not None and process.poll() is None and helper_ready:
            invalid_windows = [
                window.definition['id']
                for window in self.windows.values()
                if not window.attached or not window.visible
            ]
            if invalid_windows:
                return {
                    'status': 'error',
                    'detail': f"任务栏窗口状态异常：{', '.join(invalid_windows)}",
                    'exit_code': None,
                }
            return {
                'status': 'running',
                'detail': 'Colpilot 插件正在管理三个任务栏窗口',
                'exit_code': None,
            }
        return {'status': 'stopped', 'detail': '任务栏窗口已经停止', 'exit_code': None}

    def write_window_state(self, window: ManagedTaskbarWindow) -> None:
        with self.state_file_lock:
            with window.lock:
                timestamp = window.updated_at or datetime.now().astimezone().isoformat(timespec='seconds')
                payload = {
                    'timestamp': timestamp,
                    'reason': 'data-change',
                    'attached': window.attached,
                    'processId': self.helper_pid,
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
                    'handles': dict(window.handles),
                    'visible': window.visible,
                    'dpi': window.dpi,
                    'rectangles': dict(window.rectangles),
                }
            self.artifact_directory.mkdir(parents=True, exist_ok=True)
            temporary_path = window.state_path.with_suffix(window.state_path.suffix + '.tmp')
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
            temporary_path.replace(window.state_path)

    def send_window_state(self, window: ManagedTaskbarWindow) -> None:
        with window.lock:
            state = {
                'windowId': window.definition['id'],
                'title': window.title,
                'value': window.value,
                'detail': window.detail,
                'status': window.status,
                'sourceText': window.source_text,
                'display': {
                    'title': window.title,
                    **window.display,
                },
            }
        self._send_command({'type': 'state', 'state': state})

    def _send_all_states(self) -> None:
        for window in self.windows.values():
            self.send_window_state(window)

    def _send_command(self, message: dict[str, object]) -> None:
        with self.command_lock:
            with self.state_lock:
                process = self.helper_process
            if process is None or process.poll() is not None or process.stdin is None:
                return
            try:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                if not self.stop_event.is_set():
                    with self.state_lock:
                        self.last_error = f'colmon 原生窗口宿主通信失败：{error}'
                        self.ready_event.set()

    def _read_helper_output(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            with self.state_lock:
                self.last_error = 'colmon 原生窗口宿主输出流不可用'
                self.ready_event.set()
            return
        try:
            for line in iter(stream.readline, ''):
                if not line.strip():
                    continue
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise RuntimeError('colmon 原生窗口宿主返回的数据格式无效')
                self._handle_helper_message(message)
        except (OSError, json.JSONDecodeError, RuntimeError) as error:
            if not self.stop_event.is_set():
                with self.state_lock:
                    self.last_error = f'colmon 原生窗口宿主返回数据失败：{error}'
                    self.ready_event.set()
        finally:
            if not self.stop_event.is_set() and process.poll() is not None:
                with self.state_lock:
                    if self.last_error is None:
                        self.last_error = f'colmon 原生窗口宿主已退出，退出代码：{process.returncode}'
                    self.ready_event.set()

    def _drain_helper_error(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is not None:
            for _ in iter(stream.readline, ''):
                pass

    def _handle_helper_message(self, message: dict[str, object]) -> None:
        message_type = message.get('type')
        if message_type == 'ready':
            with self.state_lock:
                self.helper_ready = True
                self.ready_event.set()
            return
        if message_type == 'window':
            self._update_window_metadata(message)
            return
        if message_type == 'error':
            detail = str(message.get('detail') or 'colmon 原生窗口宿主报告了错误')
            with self.state_lock:
                self.last_error = detail
                self.ready_event.set()
            return
        raise RuntimeError(f'colmon 原生窗口宿主返回了未知消息：{message_type}')

    def _update_window_metadata(self, message: dict[str, object]) -> None:
        window_id = message.get('windowId')
        window = self.windows.get(str(window_id))
        if window is None:
            raise RuntimeError(f'colmon 原生窗口宿主返回了未知窗口：{window_id}')
        with window.lock:
            window.attached = message.get('attached') is True
            window.visible = message.get('visible') is True
            dpi = message.get('dpi')
            if isinstance(dpi, int | float) and not isinstance(dpi, bool):
                window.dpi = int(dpi)
            placement = message.get('placement')
            if isinstance(placement, dict):
                window.placement = dict(placement)
            handles = message.get('handles')
            if isinstance(handles, dict):
                window.handles = dict(handles)
            rectangles = message.get('rectangles')
            if isinstance(rectangles, dict):
                window.rectangles = dict(rectangles)
        self.write_window_state(window)
        if all(item.attached and item.visible for item in self.windows.values()):
            self.windows_ready_event.set()

    def _run_poll(self) -> None:
        quota_reader = CodexQuotaReader(self.process_registry, self.stop_event)
        token_reader = TokenTodayReader(self.stop_event)
        next_poll = 0.0
        while not self.stop_event.is_set():
            wait_seconds = next_poll - time.monotonic()
            if wait_seconds > 0 and self.stop_event.wait(wait_seconds):
                return
            started_at = time.monotonic()
            self._read_quota_windows(quota_reader)
            if self.stop_event.is_set():
                return
            self._read_window('codex-tokens-today', token_reader.read)
            next_poll = started_at + self.refresh_interval_seconds

    def _read_quota_windows(self, quota_reader: CodexQuotaReader) -> None:
        window_targets = {
            'codex-weekly': WEEKLY_WINDOW_MINUTES,
            'codex-five-hour': FIVE_HOUR_WINDOW_MINUTES,
        }
        try:
            readings = quota_reader.read_windows(tuple(window_targets.values()))
        except Exception as error:
            for window_id in window_targets:
                self._set_window_error(window_id, error)
            return

        for window_id, target_window_minutes in window_targets.items():
            self._read_window(
                window_id,
                lambda target_window_minutes=target_window_minutes: readings[target_window_minutes],
            )

    def _read_window(self, window_id: str, reader: Any) -> None:
        window = self.windows.get(window_id)
        if window is None:
            return
        try:
            reading = reader()
            window.set_reading(reading)
        except Exception as error:
            if self.stop_event.is_set():
                return
            self._set_window_error(window_id, error)

    def _set_window_error(self, window_id: str, error: Exception) -> None:
        window = self.windows.get(window_id)
        if window is None or self.stop_event.is_set():
            return
        detail = str(error)
        window.set_error(detail)

    def _helper_command(self) -> list[str]:
        native_root = self.plugin_root / 'native'
        executable_names = [
            native_root / 'ColpilotColmonWindows.exe',
            native_root / 'publish' / 'ColpilotColmonWindows.exe',
            native_root / 'bin' / 'Release' / 'net10.0-windows' / 'win-x64' / 'publish' / 'ColpilotColmonWindows.exe',
            native_root / 'bin' / 'Debug' / 'net10.0-windows' / 'win-x64' / 'publish' / 'ColpilotColmonWindows.exe',
        ]
        for path in executable_names:
            if path.is_file():
                return [str(path)]

        dll_names = [
            native_root / 'bin' / 'Release' / 'net10.0-windows' / 'ColpilotColmonWindows.dll',
            native_root / 'bin' / 'Debug' / 'net10.0-windows' / 'ColpilotColmonWindows.dll',
        ]
        dotnet = shutil.which('dotnet')
        if dotnet is not None:
            for path in dll_names:
                if path.is_file():
                    return [dotnet, str(path)]
        raise FileNotFoundError(
            '没有找到 colmon 原生窗口宿主，请先构建 plugins/colmon/native/ColpilotColmonWindows.csproj'
        )
