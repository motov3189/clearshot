"""Settings window integration with isolated files and an in-memory GNOME backend."""
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, Gtk, Gdk, Gio
from preferences import PreferenceStore, ShortcutManager, Preferences
from settings_dialog import SettingsDialog
from PIL import Image


def pump():
    until = time.monotonic()+.3
    while time.monotonic() < until:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(.01)


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='clearshot-settings-smoke-') as directory:
    app = App()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    app.preference_store = PreferenceStore(Path(directory)/'settings.json')
    app.preferences = app.preference_store.load()
    app.shortcut_manager = ShortcutManager(backend=Gio.memory_settings_backend_new())
    app.shortcut_manager.apply('Alt+2')
    app.register(None)
    window = app.window()
    window.load(Image.new('RGB', (800, 600), 'white'))
    window.document.annotate('rect', [(20, 20), (200, 120)])
    window.changed()
    original = window.document.image.tobytes()
    dialog = SettingsDialog(window)
    dialog.record_button.set_active(True)
    assert dialog.record_key(dialog, SimpleNamespace(keyval=Gdk.KEY_3,
        is_modifier=False, state=Gdk.ModifierType.MOD1_MASK))
    assert not dialog.recording
    dialog.directory.set_text(str(Path(directory)/'exports'))
    dialog.width.set_value(8)
    dialog.font.set_value(60)
    dialog.delay.set_value(1.5)
    dialog.tool_combo.set_active_id('arrow')
    color = Gdk.RGBA(.1, .3, .6, 1)
    dialog.color_button.set_rgba(color)
    assert dialog.save_changes(), dialog.error.get_text()
    assert app.shortcut_manager.current() == '<Alt>3'
    assert app.preference_store.load().text_size == 60
    assert window.document.image.tobytes() == original, 'Settings must not edit existing annotations'
    dialog.destroy()

    # Opening settings again shows the saved values; conflict leaves both stores unchanged.
    dialog = SettingsDialog(window)
    assert dialog.font.get_value_as_int() == 60
    assert dialog.width.get_value_as_int() == 8
    assert dialog.tool_combo.get_active_id() == 'arrow'
    assert dialog.delay.get_value() == 1.5
    before = app.preference_store.path.read_bytes()
    dialog.hotkey_entry.set_text('Alt+F4')
    assert not dialog.save_changes()
    assert dialog.error.get_text()
    assert app.shortcut_manager.current() == '<Alt>3'
    assert app.preference_store.path.read_bytes() == before
    dialog.destroy()

    dialog = SettingsDialog(window)
    dialog.enabled.set_active(False)
    assert dialog.save_changes(), dialog.error.get_text()
    assert app.shortcut_manager.current() == ''
    assert app.preferences.capture_shortcut == '<Alt>3', 'Keep the remembered key while disabled'
    dialog.destroy()
    window.load(Image.new('RGB', (800, 600), 'white'))
    assert window.tool == 'arrow'
    assert window.stroke_width == 8
    assert window.text_size == 60

    dialog = SettingsDialog(window)
    assert not dialog.enabled.get_active()
    dialog.hotkey_entry.set_text('Alt+2')
    dialog.enabled.set_active(True)
    dialog.directory.set_text('~/Pictures/Clearshot')
    pump()
    capture = Gdk.pixbuf_get_from_window(dialog.get_window(), 0, 0,
        dialog.get_allocated_width(), dialog.get_allocated_height())
    capture.savev(str(out/'settings.png'), 'png', [], [])
    dialog.destroy()
    window.destroy()
print('PASS: settings UI, recording, persistence, conflict handling, disabling, defaults; no real shortcuts changed')
