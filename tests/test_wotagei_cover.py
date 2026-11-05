import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'wotagei_cover.py'
spec = importlib.util.spec_from_file_location('wotagei_cover', SCRIPT)
wota = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wota)


class PixelTests(unittest.TestCase):
    def test_selects_whole_pixel_instead_of_combining_channels(self):
        red = np.array([[[255, 20, 10]]], dtype=np.uint8)
        blue = np.array([[[5, 60, 255]]], dtype=np.uint8)
        result = wota.brightest([(red, wota.luma(red)), (blue, wota.luma(blue))])
        np.testing.assert_array_equal(result, red)
        self.assertFalse(np.array_equal(result, np.maximum(red, blue)))

    def test_neutral_grade_preserves_pixels(self):
        frame = np.random.default_rng(7).integers(0, 256, size=(24, 32, 3), dtype=np.uint8)
        result = np.asarray(wota.grade(frame, 0, 1, 1, 0, 1))
        np.testing.assert_array_equal(result, frame)

    def test_all_hues_keep_channel_ratios_and_background_is_spatial(self):
        colors = [(200, 40, 20), (20, 200, 40), (40, 20, 200),
                  (200, 180, 20), (20, 180, 200), (200, 20, 180), (180, 180, 180)]
        mask = np.zeros((24, 32), dtype=np.float32)
        mask[6:18, 8:24] = 1
        for color in colors:
            with self.subTest(color=color):
                frame = np.tile(np.array(color, np.uint8), (24, 32, 1))
                result = np.asarray(wota.grade(frame, .5, 1, 1, .4, 1, mask))
                a, b = frame[12, 16].astype(float), result[12, 16].astype(float)
                np.testing.assert_allclose(a/a.max(), b/b.max(), atol=.006)
                self.assertGreater(b.max(), a.max())
                self.assertLess(result[1, 1].max(), frame[1, 1].max())

    def test_auto_mask_is_hue_independent_and_rejects_static_bright_background(self):
        masks = []
        for color in [(240, 0, 0), (0, 240, 0), (0, 0, 240),
                      (240, 240, 0), (0, 240, 240), (240, 0, 240), (240, 240, 240)]:
            first = np.zeros((32, 40, 3), np.uint8)
            first[2:8, 2:8] = color
            last = first.copy()
            last[16:28, 20:34] = color
            mask = wota.estimate_light_mask([(first, wota.luma(first)), (last, wota.luma(last))])
            self.assertLess(mask[4, 4], .01)
            self.assertGreater(mask[22, 26], .75)
            masks.append(mask)
        for mask in masks[1:]:
            np.testing.assert_array_equal(mask, masks[0])

    def test_foreground_protection_and_explicit_whitening(self):
        frame = np.full((24, 32, 3), (180, 60, 30), np.uint8)
        empty = np.zeros((24, 32), np.float32)
        protect = empty.copy()
        protect[6:18, 8:24] = 1
        result = np.asarray(wota.grade(frame, 0, 1, 1, .5, 1, empty, protect))
        np.testing.assert_array_equal(result[12, 16], frame[12, 16])
        self.assertLess(result[0, 0].max(), frame[0, 0].max())
        light = np.ones_like(empty)
        white = np.asarray(wota.grade(frame, .8, 1, 1, 0, 1, light, core_mode='white'))
        original_ratio = float(frame[12, 16].min()) / frame[12, 16].max()
        self.assertGreater(float(white[12, 16].min()) / white[12, 16].max(), original_ratio)


class WorkflowTests(unittest.TestCase):
    def cli(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_rates_and_outputs(self):
        for label, fps_text, rate, window in [
            ('24', '24', 24, 11), ('25', '25', 25, 11),
            ('23976', '24000/1001', 24000/1001, 11)
        ]:
            with self.subTest(fps=label), tempfile.TemporaryDirectory(prefix='wota-test-') as folder:
                root = Path(folder)
                source, clip = root / 'source.mp4', root / 'clip.mp4'
                wota.run(['-y', '-f', 'lavfi', '-i', f'testsrc2=size=160x96:rate={fps_text}',
                          '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                          '-t', '3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)])
                self.cli('render', '--input', source, '--start', 1, '--end', 2, '--output', clip)
                data = json.loads(clip.with_suffix('.json').read_text(encoding='utf-8'))
                self.assertAlmostEqual(data['fps'], rate, places=3)
                self.assertEqual(data['frames'], window)
                self.assertEqual(data['written_frames'], round(rate))
                self.assertAlmostEqual(wota.source_fps(clip), rate, places=3)
                # Mapping the output audio is an observable check that it was retained.
                wota.run(['-i', str(clip), '-map', '0:a:0', '-f', 'null', '-'])
                decoded = list(wota.decode(clip, 0, 2, rate, (160, 96)))
                self.assertEqual(len(decoded), round(rate))
                self.cli('contact', '--input', clip, '--output', root / 'contact.jpg')
                cover = root / 'cover.png'
                self.cli('cover', '--input', source, '--time', 1.5, '--output', cover,
                         '--comparison', root / 'compare.jpg')
                with Image.open(cover) as image:
                    self.assertEqual(image.size, (160, 96))
                for name in ['cover.jpg', 'cover-source.png', 'cover-ungraded.png', 'cover-light-mask.png', 'cover.json', 'contact.jpg', 'compare.jpg']:
                    self.assertTrue((root / name).is_file(), name)
                if rate == 25:
                    extra = root / 'extra.mp4'
                    self.cli('render', '--input', source, '--start', 1, '--end', 2,
                             '--frames', window + 1, '--output', extra)
                    more = json.loads(extra.with_suffix('.json').read_text(encoding='utf-8'))
                    self.assertEqual(more['fps'], rate)
                    self.assertEqual(more['frames'], window + 1)
                    self.assertEqual(more['written_frames'], data['written_frames'])
                    mask_path = root / 'manual.png'
                    Image.new('L', (160, 96), 255).save(mask_path)
                    self.cli('cover', '--input', source, '--time', 1.5,
                             '--output', root / 'masked.png', '--light-mask', mask_path,
                             '--protect-mask', mask_path, '--core-mode', 'white')
                    with Image.open(root / 'masked-light-mask.png') as mask:
                        self.assertEqual(mask.getextrema(), (255, 255))


if __name__ == '__main__':
    unittest.main()
