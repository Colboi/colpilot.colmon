import json
import threading
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

from . import PLUGIN_ID
from .native_host import TaskbarWindowController, WINDOW_DEFINITIONS


MIN_REFRESH_INTERVAL_SECONDS = 10
MAX_REFRESH_INTERVAL_SECONDS = 3600
DEFAULT_REFRESH_INTERVAL_SECONDS = 60
WINDOWS = WINDOW_DEFINITIONS


class ColmonPlugin:
    plugin_id = PLUGIN_ID

    def __init__(
        self,
        plugin_root: Path,
        config_store: object,
        process_registry: Any,
    ) -> None:
        self.root = plugin_root
        self.config_store = config_store
        self.process_registry = process_registry
        self.legacy_settings_path = self.root / 'settings.json'
        self._migrate_legacy_settings()
        self.data_root = self.root / 'data'
        self.artifact_directory = self.data_root / 'artifacts'
        self.controller: TaskbarWindowController | None = None
        self.lock = threading.RLock()

    def start(self) -> None:
        with self.lock:
            self._start_locked()

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def runtime_status(self) -> dict[str, object]:
        with self.lock:
            controller = self.controller
            if controller is None:
                return {
                    'status': 'stopped',
                    'detail': 'colmon 插件未运行',
                    'exit_code': None,
                }
            return controller.runtime_status()

    def update_settings(self, settings: dict[str, object], enabled: bool) -> None:
        supported_keys = {'refresh_interval_seconds'}
        unknown_keys = sorted(set(settings) - supported_keys)
        if unknown_keys:
            raise ValueError(f'unsupported settings: {", ".join(unknown_keys)}')
        if not settings:
            raise ValueError('settings must not be empty')

        with self.lock:
            current = self._read_settings_locked()
            updated = dict(current)
            if 'refresh_interval_seconds' in settings:
                updated['refresh_interval_seconds'] = self._validate_refresh_interval(
                    settings['refresh_interval_seconds']
                )
            if updated == current:
                return

            self._write_settings_locked(updated)
            self._prepare_runtime_locked()
            self._stop_locked()
            if enabled:
                self._start_locked()

    def status(self, enabled: bool) -> dict[str, object]:
        with self.lock:
            settings = self._read_settings_locked()
            refresh_interval_seconds = settings['refresh_interval_seconds']
            runtime = self.runtime_status()
            controller = self.controller
            process_id = controller.helper_pid if controller is not None else None

        return {
            'runtime': runtime,
            'windows': [
                self._read_window_status(
                    definition,
                    enabled,
                    runtime,
                    refresh_interval_seconds,
                    process_id,
                    controller,
                )
                for definition in WINDOWS
            ],
            'window_count': len(WINDOWS),
            'managed_in_process': False,
            'managed_by_plugin': True,
            'refresh_interval_seconds': refresh_interval_seconds,
            'minimum_refresh_interval_seconds': MIN_REFRESH_INTERVAL_SECONDS,
            'maximum_refresh_interval_seconds': MAX_REFRESH_INTERVAL_SECONDS,
            'process_id': process_id,
            'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        }

    def _start_locked(self) -> None:
        if self.controller is not None:
            return

        settings = self._read_settings_locked()
        self._prepare_runtime_locked()
        controller = TaskbarWindowController(
            plugin_root=self.root,
            artifact_directory=self.artifact_directory,
            refresh_interval_seconds=settings['refresh_interval_seconds'],
            process_registry=self.process_registry,
        )
        controller.start()
        self.controller = controller

    def _stop_locked(self) -> None:
        if self.controller is None:
            return
        controller = self.controller
        self.controller = None
        controller.stop()

    def _read_settings_locked(self) -> dict[str, int]:
        data = self.config_store.read_section(self.plugin_id)
        unknown_keys = sorted(set(data) - {'refresh_interval_seconds'})
        if unknown_keys:
            raise ValueError(f'unsupported saved settings: {", ".join(unknown_keys)}')
        return {
            'refresh_interval_seconds': self._validate_refresh_interval(
                self._parse_integer(
                    data.get(
                        'refresh_interval_seconds',
                        str(DEFAULT_REFRESH_INTERVAL_SECONDS),
                    )
                )
            )
        }

    def _write_settings_locked(self, settings: dict[str, int]) -> None:
        self.config_store.update_section(
            self.plugin_id,
            settings,
        )

    def _migrate_legacy_settings(self) -> None:
        self.config_store.migrate_json_section(
            self.plugin_id,
            self.legacy_settings_path,
            {'refresh_interval_seconds'},
        )

    def _prepare_runtime_locked(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.artifact_directory.mkdir(parents=True, exist_ok=True)

    def _read_window_status(
        self,
        definition: dict[str, str],
        enabled: bool,
        runtime: dict[str, object],
        refresh_interval_seconds: int,
        process_id: int | None,
        controller: TaskbarWindowController | None,
    ) -> dict[str, object]:
        base = {
            'id': definition['id'],
            'title': definition['title'],
            'status': 'waiting',
            'value': '--',
            'detail': '等待 colmon 插件写入状态',
            'updated_at': None,
            'age_seconds': None,
            'refresh_interval_seconds': refresh_interval_seconds,
            'display': None,
            'attached': False,
            'process_id': process_id,
        }
        if not enabled:
            return {**base, 'status': 'stopped', 'detail': '插件已停用'}
        if runtime['status'] != 'running':
            return {**base, 'status': runtime['status'], 'detail': runtime['detail']}

        state_path = self.artifact_directory / definition['state_file']
        if not state_path.is_file():
            return base

        state_file_lock = controller.state_file_lock if controller is not None else nullcontext()
        try:
            with state_file_lock:
                state = json.loads(state_path.read_text(encoding='utf-8'))
                file_age_seconds = max(
                    0,
                    int(datetime.now().timestamp() - state_path.stat().st_mtime),
                )
        except (OSError, json.JSONDecodeError) as error:
            return {
                **base,
                'status': 'error',
                'detail': f'任务栏状态暂时无法读取：{error}',
            }
        if not isinstance(state, dict):
            return {**base, 'status': 'error', 'detail': '任务栏状态格式无效'}

        display = state.get('display')
        if not isinstance(display, dict):
            return {**base, 'status': 'error', 'detail': '任务栏显示数据格式无效'}

        attached = state.get('attached') is True
        stale = file_age_seconds > max(refresh_interval_seconds * 3, 180)
        state_status = state.get('status')
        if state_status == 'error':
            status = 'error'
            detail = str(state.get('detail') or '任务栏数据读取失败')
        elif state_status != 'ready':
            status = 'waiting'
            detail = str(state.get('detail') or '等待任务栏数据')
        elif not attached:
            status = 'error'
            detail = '任务栏窗口没有正确附着'
        elif stale:
            status = 'stale'
            detail = self._display_detail(definition['kind'], display, True)
        else:
            status = 'ready'
            detail = self._display_detail(definition['kind'], display, False)
        value = self._display_value(definition['kind'], display)
        updated_at = state.get('timestamp') if isinstance(state.get('timestamp'), str) else None
        return {
            **base,
            'title': str(display.get('title') or definition['title']),
            'status': status,
            'value': value,
            'detail': detail,
            'updated_at': updated_at,
            'age_seconds': file_age_seconds,
            'refresh_interval_seconds': display.get(
                'refreshIntervalSeconds', refresh_interval_seconds
            ),
            'display': display,
            'attached': attached,
            'process_id': state.get('processId', process_id),
            'placement': state.get('placement'),
        }

    @staticmethod
    def _display_value(kind: str, display: dict[str, object]) -> str:
        if kind == 'count':
            return str(display.get('numberText') or '--')
        return str(display.get('percentageText') or '--%')

    @staticmethod
    def _display_detail(kind: str, display: dict[str, object], stale: bool) -> str:
        if stale:
            return '任务栏状态超过更新期限'
        if kind == 'count':
            return 'Codex 当日 token 总量'
        reset_text = str(display.get('resetRemainingText') or '--')
        return f'剩余时间：{reset_text}'

    @staticmethod
    def _validate_refresh_interval(value: object) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < MIN_REFRESH_INTERVAL_SECONDS
            or value > MAX_REFRESH_INTERVAL_SECONDS
        ):
            raise ValueError(
                f'refresh_interval_seconds 必须是 {MIN_REFRESH_INTERVAL_SECONDS} 到 '
                f'{MAX_REFRESH_INTERVAL_SECONDS} 之间的整数'
            )
        if isinstance(value, float) and not value.is_integer():
            raise ValueError('refresh_interval_seconds 必须是整数')
        return min(int(value), MAX_REFRESH_INTERVAL_SECONDS)

    @staticmethod
    def _parse_integer(value: str) -> int:
        try:
            return int(value.strip())
        except ValueError as error:
            raise ValueError('refresh_interval_seconds 必须是整数') from error


def create_plugin(
    plugin_root: Path,
    config_store: object,
    process_registry: Any,
) -> ColmonPlugin:
    return ColmonPlugin(plugin_root, config_store, process_registry)
