"""Pixel-based editing. Display scale is deliberately absent from this module.

Annotations are kept as editable vector layers above the original screenshot.
Exporting composites them at full source resolution, so untouched pixels keep
their exact original values.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cairo
import gi
bridge = Path(__file__).parent / '.deps/runtime/usr/lib/python3/dist-packages/gi'
if bridge.is_dir():
    gi.__path__.append(str(bridge))
gi.require_foreign('cairo')
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo
from PIL import Image

DEFAULT_COLOR = (0.92, 0.2, 0.22, 1.0)
FONT_FAMILY = 'Sans'


@dataclass(frozen=True)
class ViewTransform:
    scale: float
    x: float = 0
    y: float = 0

    def to_image(self, x, y):
        return ((x - self.x) / self.scale, (y - self.y) / self.scale)


@dataclass
class Annotation:
    """One editable annotation. Geometry is stored in original image pixels."""
    id: int
    tool: str
    points: list
    color: tuple = DEFAULT_COLOR
    width: float = 5
    text: str = ''
    font_size: int = 28

    def clone(self):
        return Annotation(
            self.id, self.tool, [(float(x), float(y)) for x, y in self.points],
            tuple(self.color), float(self.width), self.text, int(self.font_size))


def bounds(start, end, size):
    w, h = size
    x0, x1 = sorted((round(start[0]), round(end[0])))
    y0, y1 = sorted((round(start[1]), round(end[1])))
    return max(0, min(w, x0)), max(0, min(h, y0)), max(0, min(w, x1)), max(0, min(h, y1))


def points_bounds(points):
    """Tight geometric bounds, without stroke padding."""
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


@lru_cache(maxsize=256)
def text_extent(text, font_size):
    """Pango's logical size for a text annotation, in image pixels."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    layout = _text_layout(cairo.Context(surface), text, font_size)
    _, logical = layout.get_pixel_extents()
    return max(1, logical.width), max(1, logical.height)


def annotation_bounds(annotation):
    """Selection/export bounds including the stroke width."""
    if not annotation.points:
        return None
    if annotation.tool == 'text':
        if not annotation.text:
            return None
        width, height = text_extent(annotation.text, annotation.font_size)
        x, y = annotation.points[0]
        return x, y, x + width, y + height
    box = points_bounds(annotation.points)
    if annotation.tool == 'arrow':
        pad = max(6.0, annotation.width * 1.8)
    elif annotation.tool == 'highlight':
        pad = 2.0
    else:
        pad = max(2.0, annotation.width / 2 + 1)
    return box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad


def _segment_distance(point, start, end):
    x, y = point
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    length = dx * dx + dy * dy
    if length <= 1e-9:
        return math.hypot(x - x0, y - y0)
    t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / length))
    return math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))


def annotation_hit(annotation, x, y, tolerance=0):
    """Hit testing used by the select tool and by drag-in-place edits."""
    bounds = annotation_bounds(annotation)
    if bounds is None:
        return False
    if annotation.tool in ('line', 'arrow', 'pen'):
        limit = max(annotation.width / 2, 4) + tolerance
        points = annotation.points
        if len(points) < 2:
            return False
        if any(_segment_distance((x, y), a, b) <= limit
               for a, b in zip(points, points[1:])):
            return True
        if annotation.tool == 'arrow':
            # The filled arrow head is wider than the stroke itself.
            head = max(14.0, annotation.width * 3.5)
            return math.hypot(x - points[-1][0], y - points[-1][1]) <= head + tolerance
        return False
    return (bounds[0] - tolerance <= x <= bounds[2] + tolerance
            and bounds[1] - tolerance <= y <= bounds[3] + tolerance)


def _text_layout(cr, text, font_size):
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription(FONT_FAMILY)
    font.set_absolute_size(font_size * Pango.SCALE)
    layout.set_font_description(font)
    layout.set_text(text, -1)
    return layout


def draw_annotation(cr, tool, points, color, width, text='', font_size=28):
    """Shared by live preview, selection preview, and full-resolution export."""
    if not points:
        return
    cr.save()
    cr.set_source_rgba(*color)
    cr.set_line_width(width)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    x, y = points[0]
    xx, yy = points[-1]
    left, top, w, h = min(x, xx), min(y, yy), abs(xx-x), abs(yy-y)
    if tool in ('rect', 'highlight'):
        cr.rectangle(left, top, w, h)
        if tool == 'highlight':
            cr.set_source_rgba(*color[:3], 0.32)
            cr.fill()
        else:
            cr.stroke()
    elif tool == 'ellipse' and w > 0 and h > 0:
        cr.save()
        cr.translate(left+w/2, top+h/2)
        cr.scale(w/2, h/2)
        cr.arc(0, 0, 1, 0, math.tau)
        cr.restore()
        cr.stroke()
    elif tool in ('line', 'arrow', 'pen'):
        cr.move_to(x, y)
        for point in points[1:]:
            cr.line_to(*point)
        cr.stroke()
        if tool == 'arrow':
            angle = math.atan2(yy-y, xx-x)
            length = min(max(14, width*3.5), math.hypot(xx-x, yy-y)*0.65)
            cr.move_to(xx, yy)
            cr.line_to(xx-length*math.cos(angle-0.45), yy-length*math.sin(angle-0.45))
            cr.line_to(xx-length*math.cos(angle+0.45), yy-length*math.sin(angle+0.45))
            cr.close_path()
            cr.fill()
    elif tool == 'text':
        layout = _text_layout(cr, text, font_size)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
    cr.restore()


class Document:
    HISTORY_LIMIT = 30
    HISTORY_BYTES = 128 * 1024 * 1024

    def __init__(self, image):
        self.base = image.convert('RGBA')
        self.annotations = []
        self.undo_stack = []
        self.redo_stack = []
        self.revision = 0
        self.saved_revision = 0
        self._next_revision = 1
        self._next_id = 1
        self._cache_revision = None
        self._cache_image = None

    @classmethod
    def open(cls, path):
        with Image.open(path) as im:
            im.load()
            return cls(im)

    @property
    def size(self):
        return self.base.size

    @property
    def dirty(self):
        return self.revision != self.saved_revision

    @property
    def image(self):
        """Flattened view used for export, clipboard, and pin windows."""
        if not self.annotations:
            return self.base
        if self._cache_revision != self.revision or self._cache_image is None:
            overlay = self._render_overlay()
            self._cache_image = Image.alpha_composite(self.base, overlay)
            self._cache_revision = self.revision
        return self._cache_image

    def _render_overlay(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, *self.size)
        cr = cairo.Context(surface)
        for annotation in self.annotations:
            draw_annotation(cr, annotation.tool, annotation.points, annotation.color,
                            annotation.width, annotation.text, annotation.font_size)
        data = io.BytesIO()
        surface.write_to_png(data)
        data.seek(0)
        with Image.open(data) as overlay:
            return overlay.convert('RGBA')

    def png_bytes(self):
        data = io.BytesIO()
        self.image.save(data, format='PNG')
        return data.getvalue()

    def _snapshot(self):
        # The base image is never mutated in place, so snapshots can share it.
        return (self.revision, self.base, [annotation.clone() for annotation in self.annotations])

    def _history_bytes(self):
        sizes = {}
        for _, base, _ in self.undo_stack + self.redo_stack:
            sizes[id(base)] = base.width * base.height * 4
        sizes[id(self.base)] = self.base.width * self.base.height * 4
        return sum(sizes.values())

    def _prune(self):
        while len(self.undo_stack) > self.HISTORY_LIMIT:
            self.undo_stack.pop(0)
        while len(self.undo_stack) > 1 and self._history_bytes() > self.HISTORY_BYTES:
            self.undo_stack.pop(0)

    def _commit(self, base, annotations):
        self.undo_stack.append(self._snapshot())
        self._prune()
        self.redo_stack.clear()
        self.base = base
        self.annotations = [annotation.clone() for annotation in annotations]
        self.revision = self._next_revision
        self._next_revision += 1
        self._cache_revision = None
        self._cache_image = None

    def annotation(self, annotation_id):
        for annotation in self.annotations:
            if annotation.id == annotation_id:
                return annotation
        return None

    def annotation_at(self, x, y, tolerance=0):
        for annotation in reversed(self.annotations):
            if annotation_hit(annotation, x, y, tolerance):
                return annotation
        return None

    def add_annotation(self, tool, points, color=DEFAULT_COLOR, width=5, text='', font_size=28):
        if tool in ('crop', 'pixelate'):
            return None
        points = [(float(x), float(y)) for x, y in points]
        if tool == 'text':
            if not text.strip() or not points:
                return None
        else:
            if len(points) < 2:
                return None
            if tool != 'pen' and math.dist(points[0], points[-1]) < 1:
                return None
        annotation = Annotation(self._next_id, tool, points, tuple(color),
                                float(width), text, int(font_size))
        self._next_id += 1
        self._commit(self.base, self.annotations + [annotation])
        return self.annotation(annotation.id)

    def update_annotation(self, annotation_id, **changes):
        current = self.annotation(annotation_id)
        if current is None:
            return False
        updated = current.clone()
        for name, value in changes.items():
            if name == 'points':
                updated.points = [(float(x), float(y)) for x, y in value]
            elif name == 'color':
                updated.color = tuple(value)
            elif name == 'width':
                updated.width = float(value)
            elif name == 'text':
                updated.text = str(value)
            elif name == 'font_size':
                updated.font_size = int(value)
            elif name == 'tool':
                updated.tool = str(value)
            else:
                raise KeyError(name)
        if updated == current:
            return False
        annotations = [annotation.clone() for annotation in self.annotations]
        for index, annotation in enumerate(annotations):
            if annotation.id == annotation_id:
                annotations[index] = updated
                break
        self._commit(self.base, annotations)
        return True

    def remove_annotation(self, annotation_id):
        if self.annotation(annotation_id) is None:
            return False
        self._commit(self.base,
                     [annotation for annotation in self.annotations
                      if annotation.id != annotation_id])
        return True

    def annotate(self, tool, points, color=DEFAULT_COLOR, width=5, text='', font_size=28):
        return self.add_annotation(tool, points, color, width, text, font_size) is not None

    def crop(self, start, end):
        box = bounds(start, end, self.size)
        if box[2]-box[0] < 2 or box[3]-box[1] < 2 or box == (0, 0, *self.size):
            return False
        base = self.base.crop(box)
        dx, dy = box[0], box[1]
        kept = []
        for annotation in self.annotations:
            moved = annotation.clone()
            moved.points = [(x - dx, y - dy) for x, y in moved.points]
            outline = annotation_bounds(moved)
            if outline and outline[2] > 0 and outline[0] < base.width \
                    and outline[3] > 0 and outline[1] < base.height:
                kept.append(moved)
        self._commit(base, kept)
        return True

    def pixelate(self, start, end, block=16):
        box = bounds(start, end, self.size)
        w, h = box[2]-box[0], box[3]-box[1]
        if w < 2 or h < 2:
            return False
        region = self.base.crop(box)
        region = region.resize((max(1, math.ceil(w/block)), max(1, math.ceil(h/block))), Image.Resampling.BOX)
        region = region.resize((w, h), Image.Resampling.NEAREST)
        result = self.base.copy()
        result.paste(region, box)
        self._commit(result, self.annotations)
        return True

    def _restore(self, origin, destination):
        if not origin:
            return False
        destination.append(self._snapshot())
        self.revision, self.base, self.annotations = origin.pop()
        self.annotations = [annotation.clone() for annotation in self.annotations]
        self._cache_revision = None
        self._cache_image = None
        return True

    def undo(self):
        return self._restore(self.undo_stack, self.redo_stack)

    def redo(self):
        return self._restore(self.redo_stack, self.undo_stack)

    def save(self, path):
        """PNG only: no preview-size parameter can accidentally resize an export."""
        self.image.save(path, format='PNG')
        self.saved_revision = self.revision
