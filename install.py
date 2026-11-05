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
    parser.add_argument('--shortcut', action='store_true', help='注册保存的全局截图快捷键（默认 Alt/Option + 2）')
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
        from preferences import PreferenceStore, ShortcutManager
        manager = ShortcutManager()
        if not manager.available:
            parser.error('当前桌面不支持配置 GNOME 全局快捷键。')
        binding = manager.current() or PreferenceStore().load().capture_shortcut or '<Alt>2'
        conflict = manager.conflict(binding)
        if conflict:
            parser.error('快捷键已被占用：'+conflict)
        shortcut_settings = (manager, binding)
    for destination in (launcher, desktop, icon):
        destination.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\n# Clearshot user launcher\nexec /usr/bin/python3 ' + shlex.quote(str(project/'app.py')) + ' "$@"\n')
    launcher.chmod(0o755)
    shutil.copyfile(project/'icon.svg', icon)
    desktop.write_text(
        '[Desktop Entry]\nType=Application\nName=清晰截图\nName[en]=Clearshot\n'
        'Comment=系统截图与原图标注\nExec=' + desktop_quote(launcher) + '\n'
        'Icon=' + str(icon) + '\nTerminal=false\nStartupNotify=true\nCategories=Graphics;\n'
        'Actions=Capture;Clipboard;Settings;\n\n[Desktop Action Capture]\nName=新截图\nExec=' + desktop_quote(launcher) + ' --capture\n'
        '\n[Desktop Action Clipboard]\nName=编辑剪贴板图片\nExec=' + desktop_quote(launcher) + ' --clipboard\n'
        '\n[Desktop Action Settings]\nName=设置\nExec=' + desktop_quote(launcher) + ' --settings\n')
    if shortcut_settings:
        manager, binding = shortcut_settings
        manager.apply(binding)
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(desktop.parent)], check=False)
    print('已安装：', desktop)
    print('源码保留在：', project, '（移动源码目录后请重新运行安装脚本）')
    if args.shortcut:
        print('快捷键：', binding)


if __name__ == '__main__':
    main()
