#!/usr/bin/python3
"""Install a user launcher/menu entry; optional GNOME shortcut is opt-in."""
import argparse
from pathlib import Path
import shlex
import shutil
import subprocess


def desktop_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'


def main():
    parser = argparse.ArgumentParser(description='安装清晰截图到当前用户的应用菜单')
    parser.add_argument('--prefix', type=Path, default=Path.home()/'.local', help='安装前缀，默认 ~/.local')
    parser.add_argument('--shortcut', action='store_true', help='注册 GNOME 全局快捷键 Alt/Option + 2')
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    prefix = args.prefix.expanduser().resolve()
    launcher = prefix/'bin/clearshot'
    desktop = prefix/'share/applications/org.alex.Clearshot.desktop'
    icon = prefix/'share/icons/hicolor/scalable/apps/org.alex.Clearshot.svg'
    if launcher.exists():
        old = launcher.read_text(errors='replace')
        if '# Clearshot user launcher' not in old and str(project/'app.py') not in old:
            parser.error(f'{launcher} 已存在且不属于此项目，请先检查该文件。')
    shortcut_settings = None
    if args.shortcut:
        if prefix != (Path.home()/'.local').resolve():
            parser.error('--shortcut 仅适用于默认用户安装目录')
        from gi.repository import Gio
        settings = Gio.Settings.new('org.gnome.settings-daemon.plugins.media-keys')
        paths = list(settings.get_strv('custom-keybindings'))
        path = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/clearshot/'
        schema = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding'
        for existing in paths:
            item = Gio.Settings.new_with_path(schema, existing)
            if existing != path and item.get_string('binding').lower() == '<alt>2':
                parser.error('Alt + 2 已被其他全局快捷键使用，未修改快捷键。')
        shortcut_settings = (Gio, settings, paths, path, schema)
    for destination in (launcher, desktop, icon):
        destination.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\n# Clearshot user launcher\nexec /usr/bin/python3 ' + shlex.quote(str(project/'app.py')) + ' "$@"\n')
    launcher.chmod(0o755)
    shutil.copyfile(project/'icon.svg', icon)
    desktop.write_text(
        '[Desktop Entry]\nType=Application\nName=清晰截图\nName[en]=Clearshot\n'
        'Comment=系统截图与原图标注\nExec=' + desktop_quote(launcher) + '\n'
        'Icon=' + str(icon) + '\nTerminal=false\nStartupNotify=true\nCategories=Graphics;\n'
        'Actions=Capture;Clipboard;\n\n[Desktop Action Capture]\nName=新截图\nExec=' + desktop_quote(launcher) + ' --capture\n'
        '\n[Desktop Action Clipboard]\nName=编辑剪贴板图片\nExec=' + desktop_quote(launcher) + ' --clipboard\n')
    if shortcut_settings:
        Gio, settings, paths, path, schema = shortcut_settings
        item = Gio.Settings.new_with_path(schema, path)
        item.set_string('name', '清晰截图')
        item.set_string('command', shlex.quote(str(launcher)) + ' --capture')
        item.set_string('binding', '<Alt>2')
        if path not in paths:
            settings.set_strv('custom-keybindings', paths+[path])
        Gio.Settings.sync()
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(desktop.parent)], check=False)
    print('已安装：', desktop)
    print('源码保留在：', project, '（移动源码目录后请重新运行安装脚本）')
    if args.shortcut:
        print('快捷键：Alt / Option + 2')


if __name__ == '__main__':
    main()
