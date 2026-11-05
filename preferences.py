"""Local preferences and GNOME shortcut configuration (no display scaling)."""
from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gio, Gtk, Gdk

SHORTCUT_PATH = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/clearshot/'
ROOT_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys'
ITEM_SCHEMA = ROOT_SCHEMA + '.custom-keybinding'
TOOLS = ('select', 'rect', 'arrow', 'ellipse', 'line', 'pen', 'text', 'highlight', 'pixelate', 'crop')


@dataclass
class Preferences:
    save_directory: str = ''
    capture_shortcut: str = '<Alt>2'
    default_tool: str = 'select'
    color: str = '#eb3338'
    line_width: int = 5
    text_size: int = 32
    capture_delay: float = 0

    def validated(self):
        data = asdict(self)
        directory = Path(self.save_directory or str(Path.home()/'Pictures/Clearshot')).expanduser()
        if not directory.is_absolute():
            raise ValueError('保存目录需要使用绝对路径，或以 ~/ 开头。')
        if directory.exists() and not directory.is_dir():
            raise ValueError('保存位置是文件，请选择一个目录。')
        data['save_directory'] = str(directory)
        if self.default_tool not in TOOLS:
            raise ValueError('默认工具无效。')
        if not isinstance(self.color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', self.color):
            raise ValueError('默认颜色无效。')
        for key, minimum, maximum in [('line_width', 2, 30), ('text_size', 12, 240)]:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f'{key} 应在 {minimum}–{maximum} 范围内。')
        if isinstance(self.capture_delay, bool) or not isinstance(self.capture_delay, (int, float)) or not 0 <= self.capture_delay <= 10:
            raise ValueError('截图延迟应在 0–10 秒范围内。')
        data['capture_shortcut'] = normalize_shortcut(self.capture_shortcut)
        return Preferences(**data)


class PreferenceStore:
    def __init__(self, path=None):
        config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))
        if not config.is_absolute():
            config = Path.home()/'.config'
        self.path = Path(path) if path is not None else config/'clearshot/settings.json'
        self.error = None

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError('设置文件不是 JSON 对象。')
            known = {field.name for field in fields(Preferences)}
            preferences = Preferences(**{key: value for key, value in data.items() if key in known}).validated()
            self.error = None
            return preferences
        except FileNotFoundError:
            return Preferences().validated()
        except (OSError, ValueError, TypeError) as exc:
            self.error = str(exc)
            return Preferences().validated()

    def save(self, preferences):
        preferences = preferences.validated()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.settings-', delete=False) as stream:
                pending = Path(stream.name)
                json.dump(asdict(preferences), stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(self.path)
            self.error = None
        finally:
            if pending and pending.exists():
                pending.unlink()
        return preferences


def normalize_shortcut(text):
    if not isinstance(text, str):
        raise ValueError('快捷键格式无效。')
    text = text.strip()
    if not text:
        return ''
    if '<' not in text:
        parts = [part.strip() for part in text.split('+')]
        aliases = {'alt': 'Alt', 'option': 'Alt', '⌥': 'Alt', 'ctrl': 'Control',
                   'control': 'Control', 'shift': 'Shift', 'super': 'Super',
                   'win': 'Super', 'command': 'Super', 'cmd': 'Super'}
        try:
            text = ''.join('<'+aliases[part.lower()]+'>' for part in parts[:-1]) + parts[-1]
        except KeyError:
            raise ValueError('快捷键示例：Alt+2、Ctrl+Shift+A、Super+F2。') from None
    text = text.replace('<Primary>', '<Control>')
    key, modifiers = Gtk.accelerator_parse(text)
    modifiers &= Gtk.accelerator_get_default_mod_mask()
    if not key or not Gtk.accelerator_valid(key, modifiers):
        raise ValueError('无法识别这个快捷键。')
    key = Gdk.keyval_to_lower(key)
    required = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK
    if not modifiers & required and not (Gdk.KEY_F1 <= key <= Gdk.KEY_F35 or key in (Gdk.KEY_Print, Gdk.KEY_Pause)):
        raise ValueError('请包含 Ctrl、Alt/Option 或 Super，或使用功能键。')
    return Gtk.accelerator_name(key, modifiers)


def shortcut_label(binding):
    if not binding:
        return '未启用'
    key, modifiers = Gtk.accelerator_parse(binding)
    return Gtk.accelerator_get_label(key, modifiers)


class ShortcutManager:
    def __init__(self, backend=None):
        self.backend = backend
        source = Gio.SettingsSchemaSource.get_default()
        self.available = bool(source and source.lookup(ROOT_SCHEMA, True) and source.lookup(ITEM_SCHEMA, True))
        self.settings = self._settings(ROOT_SCHEMA) if self.available else None
        self.item = self._settings(ITEM_SCHEMA, SHORTCUT_PATH) if self.available else None

    def _settings(self, schema_id, path=None):
        schema = Gio.SettingsSchemaSource.get_default().lookup(schema_id, True)
        return Gio.Settings.new_full(schema, self.backend, path)

    def current(self):
        if not self.available or SHORTCUT_PATH not in self.settings.get_strv('custom-keybindings'):
            return ''
        return self.item.get_string('binding')

    def conflict(self, binding):
        if not binding or not self.available:
            return None
        binding = normalize_shortcut(binding)
        def matches(candidate):
            try:
                return bool(candidate) and normalize_shortcut(candidate) == binding
            except ValueError:
                return False
        for path in self.settings.get_strv('custom-keybindings'):
            if path == SHORTCUT_PATH:
                continue
            item = self._settings(ITEM_SCHEMA, path)
            if matches(item.get_string('binding')):
                return item.get_string('name') or '其他自定义快捷键'
        source = Gio.SettingsSchemaSource.get_default()
        for schema_id in [ROOT_SCHEMA, 'org.gnome.desktop.wm.keybindings', 'org.gnome.shell.keybindings',
                          'org.gnome.mutter.keybindings', 'org.gnome.mutter.wayland.keybindings']:
            schema = source.lookup(schema_id, True)
            if not schema:
                continue
            settings = self._settings(schema_id)
            for key in schema.list_keys():
                value = settings.get_value(key)
                if value.get_type_string() == 's':
                    candidates = [value.unpack()]
                elif value.get_type_string() == 'as' and key != 'custom-keybindings':
                    candidates = value.unpack()
                else:
                    continue
                if any(matches(candidate) for candidate in candidates):
                    return '系统快捷键：'+key
        return None

    def snapshot(self):
        return {'paths': list(self.settings.get_strv('custom-keybindings')),
                'item': {key: self.item.get_string(key) for key in ('name', 'command', 'binding')}}

    def restore(self, snapshot):
        for key, value in snapshot['item'].items():
            self.item.set_string(key, value)
        self.settings.set_strv('custom-keybindings', snapshot['paths'])
        Gio.Settings.sync()

    def apply(self, binding):
        if not self.available:
            raise ValueError('当前桌面不支持直接设置全局快捷键。')
        binding = normalize_shortcut(binding)
        conflict = self.conflict(binding)
        if conflict:
            raise ValueError('该快捷键已被占用：'+conflict)
        if not self.settings.is_writable('custom-keybindings') or not all(self.item.is_writable(key) for key in ('name', 'command', 'binding')):
            raise ValueError('系统禁止修改快捷键设置。')
        launcher = Path.home()/'.local/bin/clearshot'
        command = shlex.quote(str(launcher))+' --capture' if launcher.exists() else '/usr/bin/python3 '+shlex.quote(str(Path(__file__).parent/'app.py'))+' --capture'
        self.item.set_string('name', '清晰截图')
        self.item.set_string('command', command)
        self.item.set_string('binding', binding)
        paths = list(self.settings.get_strv('custom-keybindings'))
        if binding and SHORTCUT_PATH not in paths:
            paths.append(SHORTCUT_PATH)
        if not binding:
            paths = [path for path in paths if path != SHORTCUT_PATH]
        self.settings.set_strv('custom-keybindings', paths)
        Gio.Settings.sync()


def apply_preferences(store, manager, preferences, binding=None):
    preferences = preferences.validated()
    previous = manager.snapshot() if manager.available and binding is not None else None
    try:
        if previous:
            manager.apply(binding)
        return store.save(preferences)
    except Exception:
        if previous:
            manager.restore(previous)
        raise
