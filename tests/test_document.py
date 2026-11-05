import io
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document import Document, ViewTransform, annotation_bounds, annotation_hit


class PixelEditingTests(unittest.TestCase):
    def test_4k_export_keeps_every_pixel_despite_fractional_preview(self):
        # High-frequency pattern exposes accidental downsampling immediately.
        image = Image.frombytes('RGB', (3840, 2160), bytes(range(256)) * (3840*2160*3//256))
        doc = Document(image)
        for preview in (0.32, 0.625, 1, 1.25, 2):
            transform = ViewTransform(preview, 23, 41)
            self.assertAlmostEqual(transform.to_image(23+1200*preview, 41+700*preview)[0], 1200)
            with Image.open(io.BytesIO(doc.png_bytes())) as exported:
                self.assertEqual(exported.size, (3840, 2160))
                self.assertEqual(exported.convert('RGB').tobytes(), image.tobytes())

    def test_annotate_export_and_undo_preserve_unedited_pixels(self):
        original = Image.new('RGBA', (320, 240), (25, 40, 70, 255))
        doc = Document(original)
        self.assertTrue(doc.annotate('rect', [(30, 30), (150, 100)], width=4))
        self.assertNotEqual(doc.image.tobytes(), original.tobytes())
        self.assertEqual(doc.image.crop((180, 120, 320, 240)).tobytes(), original.crop((180, 120, 320, 240)).tobytes())
        self.assertTrue(doc.dirty)
        self.assertTrue(doc.undo())
        self.assertEqual(doc.image.tobytes(), original.tobytes())
        self.assertFalse(doc.dirty)
        self.assertTrue(doc.redo())
        with tempfile.TemporaryDirectory() as folder:
            doc.save(Path(folder)/'edited.png')
            self.assertFalse(doc.dirty)
        doc.undo()
        self.assertTrue(doc.dirty)

    def test_mosaic_changes_real_pixels_only_inside_selection(self):
        image = Image.frombytes('RGB', (256, 128), bytes(range(256))*384)
        doc = Document(image)
        self.assertTrue(doc.pixelate((32, 16), (160, 96), block=16))
        self.assertEqual(doc.image.crop((0, 0, 32, 128)).tobytes(), image.convert('RGBA').crop((0, 0, 32, 128)).tobytes())
        self.assertNotEqual(doc.image.crop((32, 16, 160, 96)).tobytes(), image.convert('RGBA').crop((32, 16, 160, 96)).tobytes())
        self.assertEqual(doc.image.getpixel((35, 20)), doc.image.getpixel((45, 25)))
        doc.undo()
        self.assertEqual(doc.image.convert('RGB').tobytes(), image.tobytes())

    def test_crop_reverse_drag_and_outside_bounds(self):
        doc = Document(Image.new('RGB', (400, 300), 'white'))
        self.assertTrue(doc.crop((350, 250), (50, 40)))
        self.assertEqual(doc.size, (300, 210))
        doc.undo()
        self.assertTrue(doc.crop((-20, -10), (150, 100)))
        self.assertEqual(doc.size, (150, 100))
        self.assertFalse(doc.crop((400, 400), (800, 800)))

    def test_all_annotation_tools_render_at_source_resolution(self):
        for tool in ('rect', 'arrow', 'ellipse', 'line', 'pen', 'highlight', 'text'):
            with self.subTest(tool=tool):
                doc = Document(Image.new('RGBA', (400, 240), 'white'))
                self.assertTrue(doc.annotate(tool, [(30, 30), (180, 130)], text='中文 ABC 123', font_size=32))
                self.assertEqual(doc.size, (400, 240))
                self.assertNotEqual(doc.image.tobytes(), Image.new('RGBA', (400, 240), 'white').tobytes())

    def test_redo_cleared_after_different_edit(self):
        doc = Document(Image.new('RGB', (200, 200), 'white'))
        doc.crop((0, 0), (100, 100))
        doc.undo()
        doc.annotate('line', [(5, 5), (80, 80)])
        self.assertFalse(doc.redo())

    def test_finished_annotations_stay_editable_and_undoable(self):
        doc = Document(Image.new('RGBA', (400, 300), 'white'))
        box = doc.add_annotation('rect', [(20, 20), (120, 80)], width=4)
        text = doc.add_annotation('text', [(200, 150)], text='第一版', font_size=28)
        self.assertIsNotNone(box)
        self.assertIsNotNone(text)
        self.assertEqual([a.id for a in doc.annotations], [box.id, text.id])

        self.assertTrue(doc.update_annotation(box.id, points=[(60, 60), (160, 120)]))
        self.assertEqual(doc.annotation(box.id).points, [(60.0, 60.0), (160.0, 120.0)])
        self.assertTrue(doc.update_annotation(text.id, points=[(40, 200)], font_size=48))
        self.assertEqual(doc.annotation(text.id).points, [(40.0, 200.0)])
        self.assertEqual(doc.annotation(text.id).font_size, 48)

        edited = doc.image.copy()
        self.assertTrue(doc.remove_annotation(text.id))
        self.assertIsNone(doc.annotation(text.id))
        self.assertNotEqual(doc.image.tobytes(), edited.tobytes())

        self.assertTrue(doc.undo())              # delete undone
        self.assertIsNotNone(doc.annotation(text.id))
        self.assertTrue(doc.undo())              # font/move undone
        self.assertEqual(doc.annotation(text.id).font_size, 28)
        self.assertEqual(doc.annotation(text.id).points, [(200.0, 150.0)])
        self.assertTrue(doc.undo())              # box move undone
        self.assertEqual(doc.annotation(box.id).points, [(20.0, 20.0), (120.0, 80.0)])
        self.assertTrue(doc.redo())
        self.assertEqual(doc.annotation(box.id).points, [(60.0, 60.0), (160.0, 120.0)])

    def test_text_bounds_grow_with_font_and_hit_testing_uses_shape(self):
        doc = Document(Image.new('RGBA', (500, 320), 'white'))
        text = doc.add_annotation('text', [(40, 50)], text='可编辑文字', font_size=30)
        small = annotation_bounds(text)
        self.assertTrue(doc.update_annotation(text.id, font_size=60))
        large = annotation_bounds(doc.annotation(text.id))
        self.assertGreater(large[2] - large[0], small[2] - small[0])
        self.assertGreater(large[3] - large[1], small[3] - small[1])
        self.assertEqual(doc.annotation_at(60, 70, 2).id, text.id)
        self.assertIsNone(doc.annotation_at(400, 300, 2))

        line = doc.add_annotation('line', [(20, 20), (200, 200)], width=4)
        self.assertTrue(annotation_hit(line, 110, 110, 2))
        self.assertFalse(annotation_hit(line, 40, 200, 2))

    def test_crop_translates_annotations_and_drops_the_ones_left_outside(self):
        doc = Document(Image.new('RGBA', (400, 300), 'white'))
        box = doc.add_annotation('rect', [(50, 60), (150, 160)])
        text = doc.add_annotation('text', [(200, 200)], text='留在这里', font_size=24)
        outside = doc.add_annotation('ellipse', [(360, 250), (390, 290)])
        self.assertTrue(doc.crop((100, 100), (300, 300)))
        self.assertEqual(doc.size, (200, 200))
        self.assertEqual(doc.annotation(box.id).points, [(-50.0, -40.0), (50.0, 60.0)])
        self.assertEqual(doc.annotation(text.id).points, [(100.0, 100.0)])
        self.assertIsNone(doc.annotation(outside.id))
        self.assertTrue(doc.undo())
        self.assertEqual(doc.size, (400, 300))
        self.assertEqual(doc.annotation(box.id).points, [(50.0, 60.0), (150.0, 160.0)])
        self.assertIsNotNone(doc.annotation(outside.id))


if __name__ == '__main__':
    unittest.main()
