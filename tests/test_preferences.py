from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preferences import (Preferences, PreferenceStore, ShortcutManager,
                         normalize_shortcut, apply_preferences, Gio, Gtk, Gdk, ITEM_SCHEMA)


class PreferenceTests(unittest.TestCase):
    def test_settings_round_trip_and_private_atomic_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PreferenceStore(Path(directory)/'config/settings.json')
            preferences = Preferences(save_directory=directory, capture_shortcut='Option+3',
                line_width=9, text_size=60, default_tool='arrow', capture_delay=1.5)
            store.save(preferences)
            loaded = PreferenceStore(store.path).load()
            self.assertEqual(loaded.capture_shortcut, '<Alt>3')
            self.assertEqual(loaded.line_width, 9)
            self.assertEqual(loaded.default_tool, 'arrow')
            self.assertEqual(loaded.capture_delay, 1.5)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in store.path.parent.iterdir()], ['settings.json'])

    def test_corrupt_settings_fall_back_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            path.write_text('{broken')
            store = PreferenceStore(path)
            self.assertEqual(store.load().line_width, 5)
            self.assertTrue(store.error)
            self.assertEqual(path.read_text(), '{broken')

    def test_invalid_values_do_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PreferenceStore(Path(directory)/'settings.json')
            store.save(Preferences())
            original = store.path.read_bytes()
            for changes in [{'line_width': 0}, {'text_size': 500}, {'color': 'bad'},
                            {'capture_delay': float('nan')}, {'default_tool': 'unknown'},
                            {'save_directory': 'relative/folder'}]:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    store.save(replace(Preferences(), **changes))
                self.assertEqual(store.path.read_bytes(), original)

    def test_shortcut_aliases_and_invalid_plain_keys(self):
        self.assertEqual(normalize_shortcut('Option+3'), normalize_shortcut('<Alt>3'))
        key, modifiers = Gtk.accelerator_parse(normalize_shortcut('Ctrl+Shift+A'))
        self.assertEqual(key, Gdk.KEY_a)
        self.assertEqual(modifiers, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertEqual(normalize_shortcut('F8'), 'F8')
        for invalid in ['a', 'Shift+A', 'Strange+3', 'Ctrl+NotAKey', None]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_shortcut(invalid)


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        # These objects never use dconf or mutate the real desktop.
        self.backend = Gio.memory_settings_backend_new()
        self.manager = ShortcutManager(backend=self.backend)
        if not self.manager.available:
            self.skipTest('GNOME shortcut schemas are unavailable')
        self.other_path = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/fixture/'
        self.other = self.manager._settings(ITEM_SCHEMA, self.other_path)
        self.other.set_string('name', '测试中的其他应用')
        self.other.set_string('binding', '<Alt>8')
        self.manager.settings.set_strv('custom-keybindings', [self.other_path])

    def test_change_and_disable_preserve_other_shortcuts(self):
        self.manager.apply('Option+9')
        self.assertEqual(self.manager.current(), '<Alt>9')
        self.assertIn(self.other_path, self.manager.settings.get_strv('custom-keybindings'))
        self.manager.apply('')
        self.assertEqual(self.manager.current(), '')
        self.assertEqual(self.manager.settings.get_strv('custom-keybindings'), [self.other_path])

    def test_conflicting_shortcut_is_rejected_without_mutation(self):
        before = self.manager.snapshot()
        with self.assertRaises(ValueError):
            self.manager.apply('Option+8')
        self.assertEqual(self.manager.snapshot(), before)
        self.assertTrue(self.manager.conflict('Alt+F4'))

    def test_failed_config_write_rolls_back_system_shortcut(self):
        self.manager.apply('Alt+9')
        before = self.manager.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            store = PreferenceStore(Path(directory)/'settings.json')
            with patch.object(store, 'save', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    apply_preferences(store, self.manager, Preferences(capture_shortcut='Alt+7'), 'Alt+7')
        self.assertEqual(self.manager.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
