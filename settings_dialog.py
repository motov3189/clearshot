"""The local settings window; saving is explicit and failures stay in the dialog."""
from pathlib import Path

from preferences import (Preferences, normalize_shortcut, shortcut_label,
                         apply_preferences, Gtk, Gdk)


class SettingsDialog(Gtk.Dialog):
    def __init__(self, editor):
        super().__init__(title='清晰截图 · 设置', transient_for=editor, modal=True)
        self.editor = editor
        self.app = editor.get_application()
        self.recording = False
        self.set_default_size(600, 550)
        self.add_buttons('取消', Gtk.ResponseType.CANCEL, '保存设置', Gtk.ResponseType.APPLY)
        self.set_default_response(Gtk.ResponseType.APPLY)
        self.get_widget_for_response(Gtk.ResponseType.APPLY).get_style_context().add_class('suggested-action')
        self.connect('key-press-event', self.record_key)
        box = self.get_content_area()
        box.set_border_width(20)
        box.set_spacing(14)
        grid = Gtk.Grid(column_spacing=18, row_spacing=14)
        box.pack_start(grid, False, False, 0)
        preferences = self.app.preferences

        def row(index, label, widget):
            text = Gtk.Label(label=label, xalign=0)
            grid.attach(text, 0, index, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, index, 1, 1)

        manager = self.app.shortcut_manager
        binding = manager.current() if manager.available else preferences.capture_shortcut
        self.enabled = Gtk.CheckButton(label='启用全局截图快捷键')
        self.enabled.set_active(bool(binding))
        self.enabled.set_sensitive(manager.available)
        row(0, '全局快捷键', self.enabled)
        hotkey_box = Gtk.Box(spacing=8)
        self.hotkey_entry = Gtk.Entry()
        self.hotkey_entry.set_text(shortcut_label(binding or preferences.capture_shortcut or '<Alt>2'))
        self.hotkey_entry.set_placeholder_text('例如 Alt+2 或 Ctrl+Shift+A')
        hotkey_box.pack_start(self.hotkey_entry, True, True, 0)
        self.record_button = Gtk.ToggleButton(label='录制…')
        self.record_button.connect('toggled', self.toggle_record)
        hotkey_box.pack_start(self.record_button, False, False, 0)
        row(1, '', hotkey_box)
        hotkey_box.set_sensitive(manager.available)
        self.shortcut_help = Gtk.Label(xalign=0)
        self.shortcut_help.set_line_wrap(True)
        self.shortcut_help.set_max_width_chars(48)
        self.shortcut_help.set_text('Mac 键盘的 Option 对应 Alt。若组合键被系统拦截，可直接在输入框填写。' if manager.available else '当前桌面请在系统设置中把快捷键绑定到 clearshot --capture。')
        self.shortcut_help.get_style_context().add_class('dim-label')
        grid.attach(self.shortcut_help, 1, 2, 1, 1)
        directory_box = Gtk.Box(spacing=8)
        self.directory = Gtk.Entry()
        self.directory.set_text(preferences.save_directory)
        directory_box.pack_start(self.directory, True, True, 0)
        browse = Gtk.Button(label='选择…')
        browse.connect('clicked', self.choose_directory)
        directory_box.pack_start(browse, False, False, 0)
        row(3, '默认保存目录', directory_box)
        self.tool_combo = Gtk.ComboBoxText()
        from app import TOOLS
        for tool, label, key in TOOLS:
            self.tool_combo.append(tool, label)
        self.tool_combo.set_active_id(preferences.default_tool)
        row(4, '新图片默认工具', self.tool_combo)
        self.color_button = Gtk.ColorButton()
        color = Gdk.RGBA()
        color.parse(preferences.color)
        self.color_button.set_rgba(color)
        row(5, '默认标注颜色', self.color_button)
        self.width = Gtk.SpinButton.new_with_range(2, 30, 1)
        self.width.set_value(preferences.line_width)
        row(6, '默认线宽（像素）', self.width)
        self.font = Gtk.SpinButton.new_with_range(12, 240, 2)
        self.font.set_value(preferences.text_size)
        row(7, '默认字号（像素）', self.font)
        self.delay = Gtk.SpinButton.new_with_range(0, 10, .5)
        self.delay.set_digits(1)
        self.delay.set_value(preferences.capture_delay)
        row(8, '截图延迟（秒）', self.delay)
        self.error = Gtk.Label(xalign=0)
        self.error.set_line_wrap(True)
        self.error.set_max_width_chars(64)
        self.error.get_style_context().add_class('error')
        box.pack_start(self.error, False, False, 0)
        if self.app.preference_store.error:
            self.error.set_text('设置文件读取失败，当前使用默认值。取消可保留原文件，保存可重建设置。')
        note = Gtk.Label(label='默认样式用于新图片，已有标注保持原样。设置只保存在本机。', xalign=0)
        note.get_style_context().add_class('dim-label')
        box.pack_end(note, False, False, 0)
        self.show_all()

    def choose_directory(self, *_):
        chooser = Gtk.FileChooserDialog(title='选择默认保存目录', transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        chooser.add_buttons('取消', Gtk.ResponseType.CANCEL, '选择', Gtk.ResponseType.OK)
        current = Path(self.directory.get_text()).expanduser()
        if current.is_dir():
            chooser.set_current_folder(str(current))
        if chooser.run() == Gtk.ResponseType.OK:
            self.directory.set_text(chooser.get_filename())
        chooser.destroy()

    def toggle_record(self, button):
        self.recording = button.get_active()
        button.set_label('按下组合键…' if self.recording else '录制…')
        if self.recording:
            self.error.set_text('按下新的快捷键；Esc 取消录制。')

    def record_key(self, widget, event):
        if not self.recording:
            return False
        if event.keyval == Gdk.KEY_Escape:
            self.record_button.set_active(False)
            self.error.set_text('')
            return True
        if event.is_modifier:
            return True
        try:
            binding = normalize_shortcut(Gtk.accelerator_name(event.keyval,
                event.state & Gtk.accelerator_get_default_mod_mask()))
        except ValueError as exc:
            self.error.set_text(str(exc))
            return True
        self.hotkey_entry.set_text(shortcut_label(binding))
        self.enabled.set_active(True)
        self.record_button.set_active(False)
        self.error.set_text('')
        return True

    def save_changes(self):
        if self.recording:
            self.record_button.set_active(False)
        try:
            manager = self.app.shortcut_manager
            remembered = normalize_shortcut(self.hotkey_entry.get_text())
            binding = remembered if self.enabled.get_active() else ''
            color = self.color_button.get_rgba()
            rgb = '#'+''.join(f'{round(value*255):02x}' for value in (color.red, color.green, color.blue))
            preferences = Preferences(save_directory=self.directory.get_text(), capture_shortcut=remembered,
                default_tool=self.tool_combo.get_active_id(), color=rgb,
                line_width=self.width.get_value_as_int(), text_size=self.font.get_value_as_int(),
                capture_delay=self.delay.get_value())
            self.app.preferences = apply_preferences(self.app.preference_store, manager, preferences,
                binding if manager.available else None)
        except (ValueError, OSError) as exc:
            self.error.set_text(str(exc))
            return False
        self.editor.apply_defaults(self.app.preferences)
        self.editor.status.set_text('设置已保存' + (' · 快捷键立即生效' if manager.available else ''))
        return True
