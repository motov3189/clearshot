#!/usr/bin/python3
"""Clearshot: GNOME's screenshot portal plus a native, pixel-preserving editor."""
from __future__ import annotations

import io
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import unquote, urlparse
from datetime import datetime

import cairo
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib
from PIL import Image, ImageDraw

from document import (Document, ViewTransform, annotation_bounds, annotation_hit,
                      draw_annotation, points_bounds, text_extent)
from preferences import PreferenceStore, ShortcutManager

APP_ID = 'org.alex.Clearshot'
LOG = logging.getLogger('clearshot')
TOOLS = [('select', '↖ 选择', 'V'),
         ('rect', '▭ 矩形', 'R'), ('arrow', '↗ 箭头', 'A'),
         ('ellipse', '◯ 椭圆', 'E'), ('line', '╱ 直线', 'L'),
         ('pen', '✎ 画笔', 'P'), ('text', 'T 文字', 'T'),
         ('highlight', '▰ 高亮', 'H'), ('pixelate', '▦ 马赛克', 'M'),
         ('crop', '⌗ 裁剪', 'C')]
TOOL_NAMES = {'select': '选择', 'rect': '矩形', 'arrow': '箭头', 'ellipse': '椭圆',
              'line': '直线', 'pen': '画笔', 'text': '文字',
              'highlight': '高亮', 'pixelate': '马赛克', 'crop': '裁剪'}
HANDLE_CURSORS = {'nw': 'nwse-resize', 'se': 'nwse-resize',
                  'ne': 'nesw-resize', 'sw': 'nesw-resize',
                  'n': 'ns-resize', 's': 'ns-resize',
                  'e': 'ew-resize', 'w': 'ew-resize',
                  'p0': 'move', 'p1': 'move'}


def pixbuf(image):
    rgba = image.convert('RGBA')
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(rgba.tobytes()),
        GdkPixbuf.Colorspace.RGB, True, 8, *rgba.size, rgba.width*4)


def pil_image(buffer):
    stream = io.BytesIO()
    ok, data = buffer.save_to_bufferv('png', [], [])
    if not ok:
        raise ValueError('无法读取剪贴板图片')
    with Image.open(io.BytesIO(data)) as im:
        return im.convert('RGBA')


class ScreenshotPortal:
    """Subscribe before calling Screenshot, so fast responses cannot get lost."""
    def __init__(self, done, connection=None):
        self.done = done
        self.connection = connection
        self.subscription = 0
        self.timer = 0
        self.request_path = None
        self.finished = False

    def start(self):
        try:
            if self.connection is None:
                self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            token = 'clearshot_' + uuid.uuid4().hex
            sender = self.connection.get_unique_name().lstrip(':').replace('.', '_')
            self.request_path = '/org/freedesktop/portal/desktop/request/' + sender + '/' + token
            self.subscription = self.connection.signal_subscribe(
                'org.freedesktop.portal.Desktop', 'org.freedesktop.portal.Request',
                'Response', None, None, Gio.DBusSignalFlags.NONE, self._response)
            options = {'handle_token': GLib.Variant('s', token),
                       'interactive': GLib.Variant('b', True),
                       'modal': GLib.Variant('b', False)}
            self.connection.call('org.freedesktop.portal.Desktop',
                '/org/freedesktop/portal/desktop', 'org.freedesktop.portal.Screenshot',
                'Screenshot', GLib.Variant('(sa{sv})', ('', options)),
                GLib.VariantType.new('(o)'), Gio.DBusCallFlags.NONE, 30000,
                None, self._called)
            self.timer = GLib.timeout_add_seconds(300, self._timeout)
        except Exception as exc:
            self._finish(None, str(exc))

    def _called(self, connection, result):
        try:
            path = connection.call_finish(result).unpack()[0]
            if not self.finished:
                self.request_path = path
        except GLib.Error as exc:
            self._finish(None, exc.message)

    def _response(self, connection, sender, path, interface, signal, parameters):
        if path != self.request_path or self.finished:
            return
        code, result = parameters.unpack()
        if code == 1:
            self._finish(None, None)
        elif code != 0:
            self._finish(None, '系统截图未完成，请重试或使用“打开图片”。')
        else:
            uri = result.get('uri', '')
            parsed = urlparse(uri)
            if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
                self._finish(None, '系统未返回可读取的本地截图。')
            else:
                self._finish(Path(unquote(parsed.path)), None)

    def _timeout(self):
        self.timer = 0
        self.cancel()
        return GLib.SOURCE_REMOVE

    def cancel(self):
        if self.connection and self.request_path:
            self.connection.call('org.freedesktop.portal.Desktop', self.request_path,
                'org.freedesktop.portal.Request', 'Close', None, None,
                Gio.DBusCallFlags.NONE, 3000, None, None)
        self._finish(None, None)

    def _finish(self, path, error):
        if self.finished:
            return
        self.finished = True
        if self.subscription:
            self.connection.signal_unsubscribe(self.subscription)
            self.subscription = 0
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.done(path, error)


class Canvas(Gtk.DrawingArea):
    def __init__(self, window):
        super().__init__()
        self.editor = window
        self.transform = ViewTransform(1)
        self.scale = 1
        self.fit = True
        self.points = []
        self.buffer = None
        self._buffer_base = None
        self.selected_id = None
        self.manipulation = None
        self._tap_timer = 0
        self._cursor_name = None
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                        Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect('draw', self.draw)
        self.connect('button-press-event', self.press)
        self.connect('motion-notify-event', self.motion)
        self.connect('button-release-event', self.release)
        self.connect('scroll-event', self.scroll)
        self.connect('leave-notify-event', self.leave)
        self.connect('focus-out-event', self.focus_out)

    def refresh(self):
        doc = self.editor.document
        if doc is None:
            self.buffer = None
            self._buffer_base = None
        elif doc.base is not self._buffer_base:
            # The base only changes on crop/pixelate, so annotation edits do not
            # allocate a new 4K pixbuf.
            self.buffer = pixbuf(doc.base)
            self._buffer_base = doc.base
        self.points = []
        self.manipulation = None
        self.cancel_tap_edit()
        self.validate_selection()
        self.relayout()

    def cancel_tap_edit(self):
        if self._tap_timer:
            GLib.source_remove(self._tap_timer)
            self._tap_timer = 0

    def schedule_tap_edit(self, annotation_id):
        self.cancel_tap_edit()
        def open_editor():
            self._tap_timer = 0
            doc = self.editor.document
            annotation = doc.annotation(annotation_id) if doc else None
            if annotation and annotation.tool == 'text':
                self.editor.edit_text(annotation)
            return GLib.SOURCE_REMOVE
        # Wait past the platform double-click interval so a double-click can
        # open the editor once, without the first release opening it too.
        interval = 250
        settings = Gtk.Settings.get_default()
        if settings is not None:
            double_click = settings.get_property('gtk-double-click-time')
            if double_click:
                interval = max(250, int(double_click) + 40)
        self._tap_timer = GLib.timeout_add(interval, open_editor)

    def validate_selection(self):
        doc = self.editor.document
        if self.selected_id is not None and (doc is None or doc.annotation(self.selected_id) is None):
            self.selected_id = None

    def selected(self):
        doc = self.editor.document
        if doc is None or self.selected_id is None:
            return None
        return doc.annotation(self.selected_id)

    def select(self, annotation_id):
        self.cancel_tap_edit()
        if self.selected_id == annotation_id:
            return
        self.selected_id = annotation_id
        self.manipulation = None
        self.editor.update_status()
        self.queue_draw()

    def relayout(self, *_):
        doc = self.editor.document
        vw, vh = self.editor.scroller.get_allocated_width(), self.editor.scroller.get_allocated_height()
        if doc and vw > 100 and vh > 100:
            if self.fit:
                self.scale = min(1, (vw-56)/doc.size[0], (vh-56)/doc.size[1])
            width = max(vw-2, math.ceil(doc.size[0]*self.scale)+56)
            height = max(vh-2, math.ceil(doc.size[1]*self.scale)+56)
            if self.get_size_request() != (width, height):
                self.set_size_request(width, height)
        self.editor.update_status()
        self.queue_draw()

    def draw(self, widget, cr):
        aw, ah = self.get_allocated_width(), self.get_allocated_height()
        cr.set_source_rgb(0.90, 0.925, 0.95)
        cr.paint()
        doc = self.editor.document
        if not doc or not self.buffer:
            return False
        w, h = doc.size
        x, y = max(28, (aw-w*self.scale)/2), max(28, (ah-h*self.scale)/2)
        self.transform = ViewTransform(self.scale, x, y)
        cr.set_source_rgba(0.1, 0.2, 0.3, 0.12)
        cr.rectangle(x+3, y+4, w*self.scale, h*self.scale)
        cr.fill()
        cr.save()
        cr.translate(x, y)
        cr.scale(self.scale, self.scale)
        cr.rectangle(0, 0, w, h)
        cr.clip()
        Gdk.cairo_set_source_pixbuf(cr, self.buffer, 0, 0)
        cr.paint()
        for annotation in self.display_annotations():
            draw_annotation(cr, annotation.tool, annotation.points, annotation.color,
                            annotation.width, annotation.text, annotation.font_size)
        if self.points:
            tool = self.editor.tool
            if tool in ('crop', 'pixelate'):
                left, top = self.points[0]
                right, bottom = self.points[-1]
                cr.set_source_rgba(0.08, 0.42, 0.83, 0.15)
                cr.rectangle(left, top, right-left, bottom-top)
                cr.fill_preserve()
                cr.set_source_rgba(0.08, 0.42, 0.83, 1)
                cr.set_line_width(1.5/self.scale)
                cr.set_dash([5/self.scale, 4/self.scale])
                cr.stroke()
            else:
                draw_annotation(cr, tool, self.points, self.editor.color,
                                self.editor.stroke.get_value())
        cr.restore()
        selected = self.selection_preview()
        if selected:
            self.draw_selection(cr, selected)
        return False

    def display_annotations(self):
        doc = self.editor.document
        if not doc:
            return []
        if not self.manipulation:
            return doc.annotations
        current = self.manipulation['current']
        return [current if annotation.id == current.id else annotation
                for annotation in doc.annotations]

    def selection_preview(self):
        if self.manipulation:
            return self.manipulation['current']
        return self.selected()

    def draw_selection(self, cr, annotation):
        box = annotation_bounds(annotation)
        if box is None:
            return
        sx, sy = self.transform.x, self.transform.y
        scale = self.scale
        left = sx + box[0]*scale
        top = sy + box[1]*scale
        width = (box[2]-box[0])*scale
        height = (box[3]-box[1])*scale
        cr.save()
        cr.set_source_rgba(0.08, 0.42, 0.83, 0.95)
        cr.set_line_width(1.3)
        cr.set_dash([5, 4])
        cr.rectangle(left, top, width, height)
        cr.stroke()
        cr.set_dash([])
        for name, (hx, hy) in self.handles(annotation).items():
            px, py = sx + hx*scale, sy + hy*scale
            cr.set_source_rgba(1, 1, 1, 1)
            if annotation.tool in ('line', 'arrow'):
                cr.arc(px, py, 5, 0, math.tau)
                cr.fill_preserve()
                cr.set_source_rgba(0.08, 0.42, 0.83, 1)
                cr.set_line_width(1.6)
                cr.stroke()
            else:
                cr.rectangle(px-4.5, py-4.5, 9, 9)
                cr.fill_preserve()
                cr.set_source_rgba(0.08, 0.42, 0.83, 1)
                cr.set_line_width(1.4)
                cr.stroke()
        cr.restore()

    @staticmethod
    def handles(annotation):
        if annotation.tool in ('line', 'arrow'):
            return {'p0': annotation.points[0], 'p1': annotation.points[-1]}
        box = annotation_bounds(annotation) if annotation.tool == 'text' else points_bounds(annotation.points)
        if box is None:
            return {}
        left, top, right, bottom = box
        if annotation.tool in ('pen', 'text'):
            return {'nw': (left, top), 'ne': (right, top),
                    'se': (right, bottom), 'sw': (left, bottom)}
        middle_x, middle_y = (left+right)/2, (top+bottom)/2
        return {'nw': (left, top), 'n': (middle_x, top), 'ne': (right, top),
                'e': (right, middle_y), 'se': (right, bottom),
                's': (middle_x, bottom), 'sw': (left, bottom), 'w': (left, middle_y)}

    def handle_at(self, point, annotation):
        if annotation is None:
            return None
        for name, (hx, hy) in self.handles(annotation).items():
            if math.hypot((hx-point[0])*self.scale, (hy-point[1])*self.scale) <= 9:
                return name
        return None

    def annotation_at(self, point, tools=None):
        doc = self.editor.document
        if not doc:
            return None
        tolerance = 7/self.scale
        for annotation in reversed(doc.annotations):
            if tools and annotation.tool not in tools:
                continue
            if annotation_hit(annotation, point[0], point[1], tolerance):
                return annotation
        return None

    def point(self, event, clamp=True):
        if self.editor.document is None:
            return None
        x, y = self.transform.to_image(event.x, event.y)
        w, h = self.editor.document.size
        if not clamp and not (0 <= x <= w and 0 <= y <= h):
            return None
        return max(0, min(w, x)), max(0, min(h, y))

    def press(self, widget, event):
        if event.button != 1 or not self.editor.document:
            return False
        point = self.point(event, False)
        if point is None:
            return False
        self.grab_focus()
        self.cancel_tap_edit()
        event_type = getattr(event, 'type', Gdk.EventType.BUTTON_PRESS)
        double = event_type in (Gdk.EventType._2BUTTON_PRESS, Gdk.EventType._3BUTTON_PRESS)
        self.manipulation = None
        selected = self.selected()
        if double:
            # Double-click edits text regardless of the active tool.
            hit = selected if selected and selected.tool == 'text' else self.annotation_at(point, ('text',))
            if hit:
                self.select(hit.id)
                self.editor.edit_text(hit)
                return True
        if self.editor.tool == 'text':
            # Clicking existing text re-opens its editor (a drag still moves it).
            hit = self.annotation_at(point, ('text',))
            if hit:
                self.select(hit.id)
                self.begin_manipulation(hit, 'move', None, point, tap_edit=True)
                return True
        if selected:
            handle = self.handle_at(point, selected)
            if handle:
                self.begin_manipulation(selected, 'resize', handle, point)
                return True
            if annotation_hit(selected, point[0], point[1], 6/self.scale):
                self.begin_manipulation(selected, 'move', None, point)
                return True
        if self.editor.tool == 'select':
            hit = self.annotation_at(point)
            if hit:
                self.select(hit.id)
                self.begin_manipulation(hit, 'move', None, point)
            else:
                self.select(None)
            return True
        if self.editor.tool == 'text':
            self.editor.add_text(point)
            return True
        self.select(None)
        self.points = [point, point]
        return True

    def begin_manipulation(self, annotation, mode, handle, point, tap_edit=False):
        self.manipulation = {
            'id': annotation.id,
            'mode': mode,
            'handle': handle,
            'start': annotation.clone(),
            'current': annotation.clone(),
            'press': point,
            'moved': False,
            'tap_edit': tap_edit,
        }
        self.update_cursor(point, pressing=True)

    def motion(self, widget, event):
        if self.editor.document is None:
            self.points = []
            self.manipulation = None
            self.update_cursor(None)
            return False
        if self.manipulation:
            point = self.point(event)
            if point is not None:
                self.manipulate(point)
            return True
        if self.points:
            point = self.point(event)
            if self.editor.tool == 'pen':
                self.points.append(point)
            else:
                self.points[-1] = point
            self.queue_draw()
            return True
        self.update_cursor(self.point(event))
        return False

    def manipulate(self, point):
        manipulation = self.manipulation
        start = manipulation['start']
        dx = point[0] - manipulation['press'][0]
        dy = point[1] - manipulation['press'][1]
        if math.hypot(dx*self.scale, dy*self.scale) > 3:
            manipulation['moved'] = True
        if manipulation['mode'] == 'move':
            current = self.moved_annotation(start, dx, dy)
        else:
            current = self.resized_annotation(start, manipulation['handle'], point)
        manipulation['current'] = current
        self.queue_draw()

    def moved_annotation(self, annotation, dx, dy):
        box = annotation_bounds(annotation)
        width, height = self.editor.document.size
        if box:
            if box[2]-box[0] <= width:
                dx = max(-box[0], min(width-box[2], dx))
            else:
                dx = 0
            if box[3]-box[1] <= height:
                dy = max(-box[1], min(height-box[3], dy))
            else:
                dy = 0
        moved = annotation.clone()
        moved.points = [(x+dx, y+dy) for x, y in annotation.points]
        return moved

    def resized_annotation(self, annotation, handle, point):
        updated = annotation.clone()
        width, height = self.editor.document.size
        x = max(0, min(width, point[0]))
        y = max(0, min(height, point[1]))
        if annotation.tool in ('line', 'arrow'):
            index = 0 if handle == 'p0' else -1
            updated.points[index] = (x, y)
            return updated
        if annotation.tool == 'text':
            box = annotation_bounds(annotation)
            if box is None:
                return updated
            left, top, right, bottom = box
            anchor_x = right if 'w' in handle else left
            anchor_y = bottom if 'n' in handle else top
            factor_x = abs(x-anchor_x) / max(1, right-left)
            factor_y = abs(y-anchor_y) / max(1, bottom-top)
            factor = max(0.15, min(8, max(factor_x, factor_y)))
            size = max(12, min(240, int(round(annotation.font_size * factor))))
            updated.font_size = size
            new_width, new_height = text_extent(annotation.text, size)
            new_left = anchor_x - new_width if 'w' in handle else anchor_x
            new_top = anchor_y - new_height if 'n' in handle else anchor_y
            updated.points = [(max(0, min(width, new_left)), max(0, min(height, new_top)))]
            return updated
        box = points_bounds(annotation.points)
        if box is None:
            return updated
        left, top, right, bottom = box
        if annotation.tool == 'pen':
            anchor_x = right if 'w' in handle else left
            anchor_y = bottom if 'n' in handle else top
            handle_x = left if 'w' in handle else right
            handle_y = top if 'n' in handle else bottom
            sx = (x-anchor_x)/(handle_x-anchor_x) if abs(handle_x-anchor_x) > 1e-6 else 1
            sy = (y-anchor_y)/(handle_y-anchor_y) if abs(handle_y-anchor_y) > 1e-6 else 1
            sx = max(0.05, min(20, sx))
            sy = max(0.05, min(20, sy))
            updated.points = [(anchor_x+(px-anchor_x)*sx, anchor_y+(py-anchor_y)*sy)
                              for px, py in annotation.points]
            return updated
        if 'w' in handle:
            left = x
        if 'e' in handle:
            right = x
        if 'n' in handle:
            top = y
        if 's' in handle:
            bottom = y
        if right-left < 2:
            if 'w' in handle:
                left = max(0, right-2)
            else:
                right = min(width, left+2)
        if bottom-top < 2:
            if 'n' in handle:
                top = max(0, bottom-2)
            else:
                bottom = min(height, top+2)
        updated.points = [(left, top), (right, bottom)]
        return updated

    def release(self, widget, event):
        if event.button != 1:
            return False
        if self.manipulation:
            manipulation = self.manipulation
            self.manipulation = None
            doc = self.editor.document
            if manipulation['moved']:
                current = manipulation['current']
                if doc.update_annotation(manipulation['id'], points=current.points,
                                         font_size=current.font_size):
                    self.editor.changed()
                else:
                    self.queue_draw()
            else:
                self.queue_draw()
                if manipulation['tap_edit']:
                    self.schedule_tap_edit(manipulation['id'])
            self.update_cursor(self.point(event))
            return True
        if not self.points:
            return False
        self.points[-1] = self.point(event)
        doc, tool = self.editor.document, self.editor.tool
        changed = False
        added = None
        if tool == 'crop':
            changed = doc.crop(self.points[0], self.points[-1])
        elif tool == 'pixelate':
            changed = doc.pixelate(self.points[0], self.points[-1], max(8, round(self.editor.stroke.get_value()*4)))
        else:
            added = doc.add_annotation(tool, self.points, self.editor.color,
                                       self.editor.stroke.get_value())
            changed = added is not None
        self.points = []
        if added is not None:
            self.select(added.id)
        elif tool in ('crop', 'pixelate') and changed:
            self.select(None)
        if changed:
            self.editor.changed()
        else:
            self.queue_draw()
        return True

    def cancel_manipulation(self):
        if not self.manipulation:
            return False
        self.manipulation = None
        self.queue_draw()
        return True

    def nudge(self, dx, dy):
        self.cancel_tap_edit()
        annotation = self.selected()
        if annotation is None:
            return False
        moved = self.moved_annotation(annotation, dx, dy)
        if moved.points == annotation.points:
            return False
        if self.editor.document.update_annotation(annotation.id, points=moved.points):
            self.editor.changed()
            return True
        return False

    def update_cursor(self, point, pressing=False):
        name = None
        if point is not None:
            selected = self.selected()
            if selected:
                handle = self.handle_at(point, selected)
                if handle:
                    name = HANDLE_CURSORS.get(handle, 'move')
                elif annotation_hit(selected, point[0], point[1], 6/self.scale):
                    name = 'move'
            if name is None and not pressing and self.editor.tool == 'select':
                if self.annotation_at(point):
                    name = 'pointer'
            if name is None and not pressing and self.editor.tool not in ('select', 'text'):
                name = 'crosshair'
        self.set_cursor_name(name)

    def set_cursor_name(self, name):
        if name == self._cursor_name:
            return
        self._cursor_name = name
        window = self.get_window()
        if window is None:
            return
        try:
            cursor = Gdk.Cursor.new_from_name(self.get_display(), name or 'default')
        except Exception:
            cursor = None
        window.set_cursor(cursor)

    def set_tool_cursor(self):
        name = 'default' if self.editor.tool in ('select', 'text') else 'crosshair'
        self._cursor_name = None
        self.set_cursor_name(name)

    def leave(self, widget, event):
        self.set_cursor_name(None)
        return False

    def focus_out(self, widget, event):
        self.cancel_tap_edit()
        return False

    def zoom(self, factor=None):
        if factor is None:
            self.fit = True
        else:
            self.fit = False
            self.scale = max(0.08, min(4, factor))
        self.relayout()

    def scroll(self, widget, event):
        self.cancel_tap_edit()
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            dy = event.delta_y if event.direction == Gdk.ScrollDirection.SMOOTH else (-1 if event.direction == Gdk.ScrollDirection.UP else 1)
            self.zoom(self.scale * (1.15 if dy < 0 else 1/1.15))
            return True
        return False


class Editor(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='清晰截图')
        self.set_default_size(1160, 800)
        self.document = None
        self.filename = None
        self.tool = app.preferences.default_tool
        rgba = Gdk.RGBA()
        rgba.parse(app.preferences.color)
        self.color = (rgba.red, rgba.green, rgba.blue, 1)
        self.stroke_width = app.preferences.line_width
        self.text_size = app.preferences.text_size
        self._syncing = False
        self.pin_windows = []
        self.settings_dialog = None
        self.connect('delete-event', self.closing)
        self.connect('destroy', self._destroyed)
        self.connect('key-press-event', self.key)
        header = Gtk.HeaderBar(title='清晰截图', subtitle='截图与编辑，保留原始像素')
        header.set_show_close_button(True)
        self.set_titlebar(header)
        for label, callback in [('新截图', lambda *_: app.capture()), ('打开', self.open_file), ('从剪贴板', self.from_clipboard)]:
            header.pack_start(self.button(label, callback))
        self.save_button = self.button('保存 PNG', self.save)
        self.save_button.get_style_context().add_class('suggested-action')
        header.pack_end(self.save_button)
        header.pack_end(self.button('复制', self.copy))
        header.pack_end(self.button('贴图', self.pin))
        self.settings_button = self.button('⚙ 设置', self.open_settings)
        header.pack_end(self.settings_button)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        toolbar = Gtk.Box(spacing=4)
        toolbar.set_border_width(10)
        toolbar.get_style_context().add_class('tools')
        root.pack_start(toolbar, False, False, 0)
        self.tool_buttons = {}
        group = None
        for tool, label, shortcut in TOOLS:
            b = Gtk.RadioButton.new_with_label_from_widget(group, label)
            group = b
            b.set_mode(False)
            b.set_tooltip_text(f'{label}  ·  {shortcut}')
            b.connect('toggled', self.select_tool, tool)
            toolbar.pack_start(b, False, False, 0)
            self.tool_buttons[tool] = b
        self.tool_buttons[app.preferences.default_tool].set_active(True)
        toolbar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)
        color = Gdk.RGBA(*self.color)
        self.color_button = Gtk.ColorButton.new_with_rgba(color)
        self.color_button.set_title('标注颜色')
        self.color_button.connect('color-set', self.set_color)
        toolbar.pack_start(self.color_button, False, False, 0)
        self.stroke = Gtk.SpinButton.new_with_range(2, 30, 1)
        self.stroke.set_value(self.stroke_width)
        self.stroke.set_tooltip_text('线宽；选中标注时修改其线宽；马赛克工具中控制块大小')
        self.stroke.connect('value-changed', self.stroke_changed)
        toolbar.pack_start(self.stroke, False, False, 0)
        actions = Gtk.Box(spacing=6)
        actions.set_border_width(8)
        root.pack_start(actions, False, False, 0)
        self.undo_button = self.button('↶ 撤销', self.undo)
        self.redo_button = self.button('↷ 重做', self.redo)
        actions.pack_start(self.undo_button, False, False, 0)
        actions.pack_start(self.redo_button, False, False, 0)
        actions.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)
        self.font_label = Gtk.Label(label='字号')
        actions.pack_start(self.font_label, False, False, 0)
        self.font_size = Gtk.SpinButton.new_with_range(12, 240, 2)
        self.font_size.set_value(self.text_size)
        self.font_size.set_tooltip_text('选中文字时调整字号；未选中时作为新文字的默认字号')
        self.font_size.connect('value-changed', self.font_changed)
        actions.pack_start(self.font_size, False, False, 0)
        self.edit_text_button = self.button('编辑文字…', self.edit_selected_text)
        self.edit_text_button.set_tooltip_text('双击已添加的文字也可以编辑')
        actions.pack_start(self.edit_text_button, False, False, 0)
        self.delete_button = self.button('删除标注', self.delete_selected)
        self.delete_button.set_tooltip_text('Delete')
        actions.pack_start(self.delete_button, False, False, 0)
        hint = Gtk.Label(label='拖动移动 · 双击文字编辑 · Delete 删除标注')
        hint.get_style_context().add_class('dim-label')
        actions.pack_start(hint, True, True, 0)
        for label, callback in [('＋', lambda *_: self.canvas.zoom(self.canvas.scale*1.25)),
                                ('100%', lambda *_: self.canvas.zoom(1)),
                                ('适应窗口', lambda *_: self.canvas.zoom()),
                                ('−', lambda *_: self.canvas.zoom(self.canvas.scale/1.25))]:
            actions.pack_end(self.button(label, callback), False, False, 0)
        self.stack = Gtk.Stack()
        root.pack_start(self.stack, True, True, 0)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.canvas = Canvas(self)
        self.scroller.add(self.canvas)
        self.scroller.connect('size-allocate', self.canvas.relayout)
        self.stack.add_named(self.scroller, 'canvas')
        welcome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        welcome.set_valign(Gtk.Align.CENTER)
        welcome.set_halign(Gtk.Align.CENTER)
        title = Gtk.Label()
        title.set_markup('<span size="24000" weight="bold">截得清楚，标得明白。</span>')
        welcome.pack_start(title, False, False, 0)
        welcome.pack_start(Gtk.Label(label='截取屏幕，或打开已有图片开始编辑。'), False, False, 0)
        start = self.button('开始系统截图', lambda *_: app.capture())
        start.get_style_context().add_class('suggested-action')
        welcome.pack_start(start, False, False, 0)
        self.stack.add_named(welcome, 'welcome')
        self.status = Gtk.Label(xalign=0)
        self.status.set_margin_start(14)
        self.status.set_margin_end(14)
        self.status.set_margin_top(9)
        self.status.set_margin_bottom(9)
        root.pack_end(self.status, False, False, 0)
        self.update_status()
        self.stack.set_visible_child_name('welcome')

    @staticmethod
    def button(label, callback):
        b = Gtk.Button.new_with_label(label)
        b.connect('clicked', callback)
        return b

    def open_settings(self, *_):
        from settings_dialog import SettingsDialog
        if self.settings_dialog:
            self.settings_dialog.present()
            return
        self.canvas.cancel_tap_edit()
        dialog = SettingsDialog(self)
        self.settings_dialog = dialog
        try:
            while dialog.run() == Gtk.ResponseType.APPLY:
                if dialog.save_changes():
                    break
        finally:
            self.settings_dialog = None
            dialog.destroy()

    def apply_defaults(self, preferences):
        rgba = Gdk.RGBA()
        rgba.parse(preferences.color)
        self.color = (rgba.red, rgba.green, rgba.blue, 1)
        self.stroke_width = preferences.line_width
        self.text_size = preferences.text_size
        self._syncing = True
        try:
            self.color_button.set_rgba(rgba)
            self.stroke.set_value(self.stroke_width)
            self.font_size.set_value(self.text_size)
        finally:
            self._syncing = False
        self.update_status()

    def select_tool(self, button, tool):
        if not button.get_active():
            return
        self.tool = tool
        if hasattr(self, 'canvas'):
            self.canvas.points = []
            self.canvas.manipulation = None
            self.canvas.cancel_tap_edit()
            if tool == 'select':
                # Keep the current annotation selected so V immediately exposes
                # its handles; drawing tools start from a clean selection.
                self.canvas.validate_selection()
            else:
                self.canvas.selected_id = None
            self.canvas.set_tool_cursor()
            self.canvas.queue_draw()
            if hasattr(self, 'status'):
                self.update_status()

    def set_color(self, button):
        c = button.get_rgba()
        color = (c.red, c.green, c.blue, 1)
        self.color = color
        if not self._syncing:
            annotation = self.canvas.selected() if hasattr(self, 'canvas') else None
            if annotation and tuple(annotation.color) != color:
                if self.document.update_annotation(annotation.id, color=color):
                    self.changed()

    def stroke_changed(self, spin):
        if self._syncing:
            return
        value = int(round(spin.get_value()))
        self.stroke_width = value
        if not self.document:
            return
        annotation = self.canvas.selected() if hasattr(self, 'canvas') else None
        if annotation and annotation.tool != 'text' and int(annotation.width) != value:
            if self.document.update_annotation(annotation.id, width=float(value)):
                self.changed()

    def font_changed(self, spin):
        if self._syncing:
            return
        value = int(round(spin.get_value()))
        self.text_size = value
        if not self.document:
            return
        annotation = self.canvas.selected() if hasattr(self, 'canvas') else None
        if annotation and annotation.tool == 'text' and annotation.font_size != value:
            if self.document.update_annotation(annotation.id, font_size=value):
                self.changed()

    def edit_selected_text(self, *_):
        annotation = self.canvas.selected() if hasattr(self, 'canvas') else None
        if annotation and annotation.tool == 'text':
            self.edit_text(annotation)

    def delete_selected(self, *_):
        annotation = self.canvas.selected() if hasattr(self, 'canvas') else None
        if annotation and self.document.remove_annotation(annotation.id):
            self.canvas.select(None)
            self.changed()

    def message(self, title, detail='', error=False):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
            message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE, text=title)
        if detail:
            dialog.format_secondary_text(detail)
        dialog.run()
        dialog.destroy()

    def can_replace(self):
        if hasattr(self, 'canvas'):
            self.canvas.cancel_tap_edit()
        if not self.document or not self.document.dirty:
            return True
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
            text='当前标注尚未保存')
        dialog.format_secondary_text('保存后再继续，或放弃当前标注。')
        dialog.add_button('取消', Gtk.ResponseType.CANCEL)
        dialog.add_button('放弃标注', Gtk.ResponseType.REJECT)
        dialog.add_button('保存', Gtk.ResponseType.ACCEPT)
        answer = dialog.run()
        dialog.destroy()
        return answer == Gtk.ResponseType.REJECT or answer == Gtk.ResponseType.ACCEPT and self.save()

    def load(self, image, filename=None):
        self.document = image if isinstance(image, Document) else Document(image)
        self.filename = Path(filename) if filename else None
        self.canvas.fit = True
        self.canvas.selected_id = None
        self.canvas.manipulation = None
        self.apply_defaults(self.get_application().preferences)
        self.tool_buttons[self.get_application().preferences.default_tool].set_active(True)
        self.canvas.refresh()
        self.canvas.set_tool_cursor()
        self.stack.set_visible_child_name('canvas')
        self.show_all()
        self.present()
        GLib.idle_add(self._fit)
        LOG.info('Opened image %sx%s', *self.document.size)

    def _fit(self):
        self.canvas.relayout()
        return GLib.SOURCE_REMOVE

    def open_path(self, path):
        try:
            with Image.open(path) as im:
                im.load()
                self.load(im, path)
        except Exception as exc:
            self.message('无法打开图片', str(exc), True)

    def open_file(self, *_):
        if not self.can_replace():
            return
        dialog = Gtk.FileChooserDialog(title='打开图片', transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons('取消', Gtk.ResponseType.CANCEL, '打开', Gtk.ResponseType.OK)
        f = Gtk.FileFilter()
        f.set_name('图片')
        f.add_pixbuf_formats()
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            dialog.destroy()
            self.open_path(path)
        else:
            dialog.destroy()

    def from_clipboard(self, *_):
        if not self.can_replace():
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.request_image(self._clipboard_received)

    def _clipboard_received(self, clipboard, buffer, *_):
        if buffer is None:
            self.message('剪贴板里没有图片', '请先用系统截图，或复制一张图片。')
        else:
            self.load(pil_image(buffer))

    def changed(self):
        self.canvas.refresh()
        self.update_status()

    def update_status(self):
        if not hasattr(self, 'status'):
            return
        doc = self.document
        annotation = self.canvas.selected() if hasattr(self, 'canvas') else None
        self.undo_button.set_sensitive(bool(doc and doc.undo_stack))
        self.redo_button.set_sensitive(bool(doc and doc.redo_stack))
        self.save_button.set_sensitive(bool(doc))
        self.delete_button.set_sensitive(bool(annotation))
        if doc:
            parts = [f'原图 {doc.size[0]} × {doc.size[1]} 像素',
                     f'预览 {self.canvas.scale:.0%}', '保存保留原始像素']
            if annotation:
                parts.append(f'已选 {TOOL_NAMES.get(annotation.tool, annotation.tool)}')
                if annotation.tool == 'text':
                    parts.append('拖动移动 · 拖角缩放字号 · 双击编辑 · Delete 删除')
                else:
                    parts.append('拖动移动 · 拖动手柄调整 · Delete 删除')
            elif doc.dirty:
                parts.append('有未保存标注')
            self.status.set_text('   ·   '.join(parts))
        else:
            self.status.set_text('准备就绪 · 支持打开图片、读取剪贴板和系统截图')
        self.update_inspector()
        self.set_title(('● ' if doc and doc.dirty else '') + '清晰截图')

    def update_inspector(self):
        if not hasattr(self, 'font_size') or not hasattr(self, 'canvas'):
            return
        annotation = self.canvas.selected()
        self._syncing = True
        try:
            is_text = bool(annotation and annotation.tool == 'text')
            font_enabled = is_text or annotation is None
            self.font_size.set_sensitive(font_enabled)
            self.font_label.set_sensitive(font_enabled)
            self.stroke.set_sensitive(not is_text)
            self.edit_text_button.set_sensitive(is_text)
            self.delete_button.set_sensitive(bool(annotation))
            if is_text:
                if int(round(self.font_size.get_value())) != annotation.font_size:
                    self.font_size.set_value(annotation.font_size)
            elif int(round(self.font_size.get_value())) != self.text_size:
                self.font_size.set_value(self.text_size)
            if annotation:
                color = tuple(annotation.color)
                if color != self.color:
                    self.color = color
                    self.color_button.set_rgba(Gdk.RGBA(*color))
                if annotation.tool != 'text':
                    if int(round(self.stroke.get_value())) != int(annotation.width):
                        self.stroke.set_value(annotation.width)
                elif int(round(self.stroke.get_value())) != self.stroke_width:
                    self.stroke.set_value(self.stroke_width)
            elif int(round(self.stroke.get_value())) != self.stroke_width:
                self.stroke.set_value(self.stroke_width)
        finally:
            self._syncing = False

    def undo(self, *_):
        self.canvas.cancel_tap_edit()
        self.canvas.cancel_manipulation()
        if self.document and self.document.undo():
            self.changed()

    def redo(self, *_):
        self.canvas.cancel_tap_edit()
        self.canvas.cancel_manipulation()
        if self.document and self.document.redo():
            self.changed()

    def text_dialog(self, content, font_size, title, accept):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons('取消', Gtk.ResponseType.CANCEL, accept, Gtk.ResponseType.OK)
        dialog.set_default_size(430, 260)
        box = dialog.get_content_area()
        box.set_border_width(14)
        box.set_spacing(10)
        text = Gtk.TextView()
        text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(110)
        scroll.add(text)
        box.pack_start(scroll, True, True, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label='字号（原图像素）'), False, False, 0)
        size = Gtk.SpinButton.new_with_range(12, 240, 2)
        size.set_value(max(12, min(240, int(font_size))))
        row.pack_start(size, False, False, 0)
        box.pack_start(row, False, False, 0)
        buffer = text.get_buffer()
        buffer.set_text(content)
        dialog.show_all()
        buffer.place_cursor(buffer.get_end_iter())
        text.grab_focus()
        result = None
        if dialog.run() == Gtk.ResponseType.OK:
            result = (buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
                      int(size.get_value()))
        dialog.destroy()
        return result

    def add_text(self, point):
        initial_size = int(round(self.font_size.get_value()))
        result = self.text_dialog('', initial_size, '添加文字', '添加')
        if result is None:
            return
        content, size = result
        if not content.strip():
            return
        self.text_size = size
        annotation = self.document.add_annotation('text', [point], self.color,
                                                  self.stroke.get_value(), text=content,
                                                  font_size=size)
        if annotation:
            self.canvas.select(annotation.id)
            self.changed()

    def edit_text(self, annotation):
        if hasattr(self, 'canvas'):
            self.canvas.cancel_tap_edit()
        result = self.text_dialog(annotation.text, annotation.font_size, '编辑文字', '保存')
        if result is None:
            return
        content, size = result
        if not content.strip():
            self.document.remove_annotation(annotation.id)
            self.canvas.select(None)
        else:
            self.text_size = size
            self.document.update_annotation(annotation.id, text=content, font_size=size)
        self.changed()

    def save(self, *_):
        if not self.document:
            return False
        dialog = Gtk.FileChooserDialog(title='保存 PNG（原始像素）', transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons('取消', Gtk.ResponseType.CANCEL, '保存', Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        folder = Path(self.get_application().preferences.save_directory)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            dialog.destroy()
            self.message('无法使用默认保存目录', str(exc), True)
            return False
        dialog.set_current_folder(str(folder))
        dialog.set_current_name('截图_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '.png')
        result = False
        if dialog.run() == Gtk.ResponseType.OK:
            path = Path(dialog.get_filename())
            if path.suffix.lower() != '.png':
                path = Path(str(path)+'.png')
            # Check the actual PNG target, including an extension added above.
            if path.exists() and str(path) != dialog.get_filename():
                overwrite = Gtk.MessageDialog(transient_for=dialog, modal=True,
                    message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                    text=f'覆盖已有文件 {path.name}？')
                allowed = overwrite.run() == Gtk.ResponseType.YES
                overwrite.destroy()
                if not allowed:
                    dialog.destroy()
                    return False
            try:
                self.document.save(path)
                self.filename = path
                self.status.set_text(f'已保存 {path.name}   ·   {self.document.size[0]} × {self.document.size[1]} 像素')
                result = True
            except Exception as exc:
                self.message('保存失败', str(exc), True)
        dialog.destroy()
        if result:
            self.update_status()
        return result

    def copy(self, *_):
        if not self.document:
            return
        try:
            # wl-copy becomes the clipboard owner and survives closing the editor.
            if Gdk.Display.get_default().get_name().startswith('wayland'):
                subprocess.run(['wl-copy', '--type', 'image/png'], input=self.document.png_bytes(), check=True, timeout=8)
            else:
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                clipboard.set_image(pixbuf(self.document.image))
                clipboard.store()
            self.status.set_text(f'已复制当前编辑结果   ·   {self.document.size[0]} × {self.document.size[1]} 像素')
        except Exception as exc:
            self.message('复制失败', str(exc), True)

    def pin(self, *_):
        if not self.document:
            return
        image = self.document.image.copy()
        window = Gtk.Window(title='清晰截图 · 贴图')
        window.set_application(self.get_application())
        window.set_keep_above(True)
        window.set_default_size(min(640, image.width), min(480, image.height))
        area = Gtk.DrawingArea()
        buffer = pixbuf(image)
        def draw(widget, cr):
            scale = min(widget.get_allocated_width()/image.width, widget.get_allocated_height()/image.height)
            cr.set_source_rgb(.12, .14, .17)
            cr.paint()
            cr.translate((widget.get_allocated_width()-image.width*scale)/2,
                         (widget.get_allocated_height()-image.height*scale)/2)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, buffer, 0, 0)
            cr.paint()
        area.connect('draw', draw)
        window.add(area)
        window.connect('key-press-event', lambda w, e: w.destroy() if e.keyval == Gdk.KEY_Escape else False)
        self.pin_windows.append(window)
        window.connect('destroy', lambda w: self.pin_windows.remove(w) if w in self.pin_windows else None)
        window.show_all()
        self.status.set_text('贴图已打开 · Esc 关闭 · GNOME 下可右键标题栏选择“总在最上层”')

    def closing(self, *_):
        if hasattr(self, 'canvas'):
            self.canvas.cancel_tap_edit()
        return not self.can_replace()

    def _destroyed(self, *_):
        if hasattr(self, 'canvas'):
            self.canvas.cancel_tap_edit()

    def key(self, widget, event):
        if hasattr(self, 'canvas'):
            self.canvas.cancel_tap_edit()
        control = event.state & Gdk.ModifierType.CONTROL_MASK
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        key = Gdk.keyval_name(event.keyval).lower()
        if control:
            action = {'s': self.save, 'c': self.copy, 'o': self.open_file,
                      'v': self.from_clipboard, 'n': lambda: self.get_application().capture(),
                      'z': self.redo if shift else self.undo, 'y': self.redo}.get(key)
            if action:
                action()
                return True
        elif key == 'escape':
            if self.canvas.cancel_manipulation():
                return True
            if self.canvas.selected_id is not None:
                self.canvas.select(None)
                return True
            self.canvas.points = []
            self.canvas.queue_draw()
            return True
        elif key in ('delete', 'backspace'):
            if self.canvas.selected_id is not None:
                self.delete_selected()
                return True
        elif key in ('return', 'kp_enter', 'f2'):
            if self.canvas.selected_id is not None:
                self.edit_selected_text()
                return True
        elif key in ('left', 'right', 'up', 'down') and self.canvas.selected_id is not None:
            step = 10 if shift else 1
            dx = {'left': -step, 'right': step}.get(key, 0)
            dy = {'up': -step, 'down': step}.get(key, 0)
            self.canvas.nudge(dx, dy)
            return True
        elif not event.state & (Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK):
            for tool, label, shortcut in TOOLS:
                if key == shortcut.lower():
                    self.tool_buttons[tool].set_active(True)
                    return True
        return False


def demo_document():
    image = Image.new('RGB', (3840, 2160), '#f4f7fb')
    d = ImageDraw.Draw(image)
    for x in range(0, image.width, 32):
        d.line((x, 0, x, image.height), fill='#dfe8f1')
    for y in range(0, image.height, 32):
        d.line((0, y, image.width, y), fill='#dfe8f1')
    d.rounded_rectangle((360, 360, 3480, 1800), 60, fill='white', outline='#b9ccdf', width=3)
    doc = Document(image)
    doc.annotate('text', [(540, 550)], (.06, .17, .26, 1), text='清晰截图 / Clearshot', font_size=112)
    doc.annotate('text', [(550, 760)], (.25, .36, .45, 1), text='原图 3840 × 2160\n箭头、文字、马赛克、裁剪。\n预览可以缩放，保存保留原始像素。', font_size=58)
    doc.annotate('arrow', [(630, 1330), (1870, 1330)], (.05, .55, .52, 1), width=14)
    doc.saved_revision = doc.revision
    return doc


def demo_image():
    return demo_document().image


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.editor = None
        self.portal = None
        self.capture_pending = False
        self.preference_store = PreferenceStore()
        self.preferences = self.preference_store.load()
        self.shortcut_manager = ShortcutManager()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        GLib.set_application_name('清晰截图')
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_data(b'.tools { background: #f8fafc; border-bottom: 1px solid #dce3ec; } .tools button { padding: 7px 8px; } .tools button:checked { background: #dcedfb; color: #125c9c; }')
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def window(self):
        if not self.editor:
            self.editor = Editor(self)
            self.editor.connect('destroy', self._editor_destroyed)
        return self.editor

    def _editor_destroyed(self, window):
        self.editor = None

    def do_command_line(self, command):
        args = command.get_arguments()[1:]
        if '--quit' in args:
            if self.editor and not self.editor.can_replace():
                return 1
            if self.portal:
                self.portal.cancel()
            self.quit()
            return 0
        if '--settings' in args:
            self.window().show_all()
            self.window().present()
            self.window().open_settings()
        elif '--capture' in args:
            self.capture()
        elif '--clipboard' in args:
            self.window().show_all()
            self.window().from_clipboard()
        elif '--demo' in args:
            if self.window().can_replace():
                self.window().load(demo_document())
        elif args:
            if self.window().can_replace():
                argument = args[0]
                path = unquote(urlparse(argument).path) if argument.startswith('file:') else argument
                path = str(Path(path) if Path(path).is_absolute() else Path(os.fsdecode(command.get_cwd())) / path)
                self.window().show_all()
                self.window().open_path(path)
        else:
            self.window().show_all()
            self.window().present()
        return 0

    def capture(self):
        if self.editor and self.editor.settings_dialog:
            self.editor.settings_dialog.present()
            return
        if self.portal or self.capture_pending:
            return
        if self.editor and not self.editor.can_replace():
            return
        if self.editor:
            self.editor.canvas.cancel_tap_edit()
            self.editor.hide()
            for pin in self.editor.pin_windows:
                pin.hide()
        self.hold()
        self.capture_pending = True
        # Let the compositor remove this app before requesting a screenshot.
        GLib.timeout_add(max(250, round(self.preferences.capture_delay*1000)), self._start_capture)

    def _start_capture(self):
        self.capture_pending = False
        self.portal = ScreenshotPortal(self._captured)
        self.portal.start()
        return GLib.SOURCE_REMOVE

    def _captured(self, path, error):
        self.portal = None
        if path:
            try:
                with Image.open(path) as image:
                    image.load()
                    self.window().load(image)
            except Exception as exc:
                error = str(exc)
        if self.editor:
            self.editor.show_all()
            self.editor.present()
            for pin in self.editor.pin_windows:
                pin.show_all()
        if error:
            LOG.error('Screenshot failed: %s', error)
            self.window().show_all()
            self.window().message('无法获取系统截图', error, True)
        self.release()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    raise SystemExit(App().run(sys.argv))
