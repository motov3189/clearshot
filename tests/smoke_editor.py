"""Run on Xvfb: exercise actual GTK canvas mapping without touching the desktop."""
import io
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, demo_document, demo_image, Gtk, Gdk, GLib, Gio
from document import annotation_bounds
from PIL import Image

demo = demo_document()
assert [annotation.tool for annotation in demo.annotations] == ['text', 'text', 'arrow']
assert not demo.dirty

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
app = App()
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
app.register(None)
window = app.window()
window.load(demo_image())


def pump(seconds=.4):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(.01)


pump()
original = window.document.image.copy()
for zoom in (.25, .625, 1.25):
    window.canvas.zoom(zoom)
    pump()
    transform = window.canvas.transform
    # A 500x300 crop must be 500x300 regardless of the on-screen preview.
    window.tool_buttons['crop'].set_active(True)
    a = SimpleNamespace(button=1, x=transform.x+100*zoom, y=transform.y+100*zoom)
    b = SimpleNamespace(button=1, x=transform.x+600*zoom, y=transform.y+400*zoom)
    window.canvas.press(window.canvas, a)
    window.canvas.motion(window.canvas, b)
    window.canvas.release(window.canvas, b)
    assert window.document.size == (500, 300), (zoom, window.document.size)
    assert window.document.image.tobytes() == original.crop((100, 100, 600, 400)).tobytes()
    window.undo()
    assert window.document.size == (3840, 2160)

window.tool_buttons['arrow'].set_active(True)
window.document.annotate('rect', [(450, 500), (2100, 1180)], width=8)
window.changed()
window.canvas.zoom()
pump()


def event(x, y, etype=Gdk.EventType.BUTTON_PRESS):
    return SimpleNamespace(button=1, x=x, y=y, type=etype,
                           state=Gdk.ModifierType(0))


def canvas_point(x, y, etype=Gdk.EventType.BUTTON_PRESS):
    transform = window.canvas.transform
    scale = window.canvas.scale
    return event(transform.x+x*scale, transform.y+y*scale, etype)


# A finished annotation can be selected, dragged, nudged, and undone.
rect = window.document.annotations[-1]
window.tool_buttons['select'].set_active(True)
window.canvas.zoom(1)
pump()
window.canvas.select(rect.id)
start = window.document.annotation(rect.id).points[0]
window.canvas.press(window.canvas, canvas_point(500, 550))
window.canvas.motion(window.canvas, canvas_point(600, 620))
window.canvas.release(window.canvas, canvas_point(600, 620))
pump()
moved = window.document.annotation(rect.id).points[0]
assert moved == (start[0]+100, start[1]+70), moved
assert window.canvas.selected_id == rect.id
window.undo()
assert window.document.annotation(rect.id).points[0] == start

# Drag the east resize handle.
window.canvas.select(rect.id)
box = window.document.annotation(rect.id).points
window.canvas.press(window.canvas, canvas_point(box[1][0], (box[0][1]+box[1][1])/2))
window.canvas.motion(window.canvas, canvas_point(box[1][0]+120, (box[0][1]+box[1][1])/2+40))
window.canvas.release(window.canvas, canvas_point(box[1][0]+120, (box[0][1]+box[1][1])/2+40))
pump()
assert window.document.annotation(rect.id).points[1][0] == box[1][0]+120

# Text keeps its vector data and can be edited/moved after it was added.
text = window.document.add_annotation('text', [(550, 1380)], (0.75, 0.12, 0.12, 1),
                                      text='再次编辑', font_size=54)
window.changed()
window.canvas.select(text.id)
window.update_status()
window.font_size.set_value(72)
pump()
assert window.document.annotation(text.id).font_size == 72

# Corner handles scale the text and keep the opposite corner anchored.
text_box = annotation_bounds(window.document.annotation(text.id))
window.canvas.press(window.canvas, canvas_point(text_box[2], text_box[3]))
window.canvas.motion(window.canvas, canvas_point(text_box[2]+80, text_box[3]+40))
window.canvas.release(window.canvas, canvas_point(text_box[2]+80, text_box[3]+40))
pump()
assert window.document.annotation(text.id).font_size > 72

window.tool_buttons['text'].set_active(True)
pump()
edits = []
window.edit_text = lambda annotation: edits.append(annotation.id)
text_box = window.document.annotation(text.id).points[0]
window.canvas.press(window.canvas, canvas_point(text_box[0]+10, text_box[1]+10))
window.canvas.release(window.canvas, canvas_point(text_box[0]+10, text_box[1]+10))
pump(1.0)
assert edits == [text.id], edits
before_drag = window.document.annotation(text.id).points[0]
window.canvas.press(window.canvas, canvas_point(text_box[0]+20, text_box[1]+20))
window.canvas.motion(window.canvas, canvas_point(text_box[0]+120, text_box[1]+90))
window.canvas.release(window.canvas, canvas_point(text_box[0]+120, text_box[1]+90))
pump()
assert window.document.annotation(text.id).points[0] != before_drag
assert edits == [text.id]

window.tool_buttons['select'].set_active(True)
window.canvas.select(rect.id)
window.canvas.zoom()
pump()
surface = window.get_window()
capture = Gdk.pixbuf_get_from_window(surface, 0, 0, window.get_allocated_width(), window.get_allocated_height())
capture.savev(str(out/'editor.png'), 'png', [], [])
window.document.save(out/'export.png')
with Image.open(out/'export.png') as exported:
    assert exported.size == (3840, 2160)
expected = window.document.image.tobytes()
window.copy()
received = []
Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).request_image(lambda clipboard, image: received.append(image))
pump()
assert received and (received[0].get_width(), received[0].get_height()) == (3840, 2160)
window.from_clipboard()
pump()
assert window.document.image.tobytes() == expected
window.pin()
pump()
assert len(window.pin_windows) == 1
window.pin_windows[0].destroy()
window.document.saved_revision = window.document.revision
window.destroy()
print('PASS: GTK editor, crop coordinates at 25%, 62.5%, 125%, undo, 4K export, '
      'annotation drag/resize/text edit, clipboard round trip, pin window')
