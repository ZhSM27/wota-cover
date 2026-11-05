"""Deterministic, source-hue-preserving wota video and cover processing."""
import argparse
from collections import deque
import json
import math
from fractions import Fraction
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def ffmpeg():
    found = shutil.which('ffmpeg')
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args):
    subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', *args], check=True)


def source_fps(source):
    """Prefer exact probe rates; FFmpeg metadata is enough when ffprobe is absent."""
    probe = shutil.which('ffprobe')
    if probe:
        data = subprocess.check_output([probe, '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=avg_frame_rate,r_frame_rate', '-of', 'json', str(source)])
        stream = json.loads(data)['streams'][0]
        for key in ('avg_frame_rate', 'r_frame_rate'):
            try:
                rate = float(Fraction(stream[key]))
                if math.isfinite(rate) and rate > 0:
                    return rate
            except (KeyError, ValueError, ZeroDivisionError):
                pass
    result = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(source), '-map', '0:v:0',
        '-frames:v', '0', '-f', 'null', '-'], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
        check=True)
    header = result.stderr.decode('utf-8', errors='replace').split('Stream mapping:')[0]
    for line in header.splitlines():
        if 'Video:' not in line:
            continue
        match = re.search(r'(\d+(?:\.\d+)?) fps', line)
        if not match:
            match = re.search(r'(\d+(?:\.\d+)?) tbr', line)
        if match:
            rate = float(match.group(1))
            # Recover common fractional rates rounded by FFmpeg's diagnostic display.
            standards = [24000/1001, 24, 25, 30000/1001, 30, 50, 60000/1001, 60]
            nearest = min(standards, key=lambda n: abs(n-rate))
            return nearest if abs(nearest-rate) <= .015 else rate
    raise ValueError('Cannot detect source frame rate; inspect the source and specify --fps')


def reserve(paths, overwrite):
    for path in paths:
        path = Path(path)
        if path.exists() and not overwrite:
            raise FileExistsError(f'Output exists: {path}; choose a new name or use --overwrite')
        path.parent.mkdir(parents=True, exist_ok=True)


def dimensions(source):
    with tempfile.TemporaryDirectory(prefix='wota-probe-') as folder:
        path = Path(folder) / 'probe.png'
        run(['-i', str(source), '-frames:v', '1', '-y', str(path)])
        with Image.open(path) as im:
            return im.size


def decode(source, start, duration, fps, size):
    """Yield exact-sized RGB frames with bounded memory, handling short reads."""
    # Anchor to an integer second so nearby requests share the same frame grid.
    anchor = math.floor(start)
    skip = round((start - anchor) * fps)
    count = max(1, round(duration * fps))
    filt = f'fps={fps}:start_time=0,trim=start_frame={skip}:end_frame={skip+count},setpts=PTS-STARTPTS'
    cmd = [ffmpeg(), '-hide_banner', '-loglevel', 'error', '-ss', str(anchor),
           '-i', str(source), '-t', str(duration), '-an', '-vf', filt,
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    length = size[0] * size[1] * 3
    try:
        while True:
            data = bytearray()
            while len(data) < length:
                chunk = proc.stdout.read(length - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            if not data:
                break
            if len(data) != length:
                raise RuntimeError('Incomplete decoded frame')
            yield np.frombuffer(data, np.uint8).reshape(size[1], size[0], 3).copy()
        err = proc.stderr.read().decode('utf-8', errors='replace')
        if proc.wait() != 0:
            raise RuntimeError(err)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def luma(frame):
    f = frame.astype(np.float32)
    return f[:, :, 0] * .2126 + f[:, :, 1] * .7152 + f[:, :, 2] * .0722


def brightest(history):
    """Select a complete RGB triplet; ties favor the newest frame."""
    image, score = history[-1]
    image, score = image.copy(), score.copy()
    for frame, value in reversed(list(history)[:-1]):
        take = value > score
        image[take] = frame[take]
        score[take] = value[take]
    return image


def image_u8(a):
    return Image.fromarray(np.uint8(np.clip(a, 0, 1) * 255 + .5))


def blur(mask, radius):
    im = Image.fromarray(np.uint8(np.clip(mask, 0, 1) * 255 + .5))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius))).astype(np.float32) / 255


def estimate_light_mask(history, threshold=.55, motion_threshold=.08):
    """Hue-independent bright/moving-light heuristic, not semantic segmentation."""
    values = [frame.max(2).astype(np.float32) / 255 for frame, _ in history]
    high = np.maximum.reduce(values)
    bright = np.clip((high - threshold) / max(1 - threshold, 1e-5), 0, 1)
    if len(values) > 1:
        change = high - np.minimum.reduce(values)
        motion = np.clip((change - motion_threshold) / max(.25, motion_threshold), 0, 1)
        bright *= motion
    return blur(bright, 1)


def load_mask(path, size):
    with Image.open(path) as image:
        if image.size != size:
            raise ValueError(f'Mask {path} must match source dimensions {size}')
        return np.asarray(image.convert('L')).astype(np.float32) / 255


def grade(frame, core_strength, shadow, highlight, background, opacity,
          light_mask=None, protect_mask=None, core_mode='preserve'):
    a = frame.astype(np.float32) / 255
    v = a.max(2)
    if light_mask is None:
        light_mask = np.clip((v - .55) / .45, 0, 1)
    core = light_mask * blur(np.clip((v - .65) / .30, 0, 1), .65)
    if opacity < 1:
        # A local colored layer inferred from pixels, with no predetermined hue.
        small = image_u8(a).filter(ImageFilter.GaussianBlur(18))
        tint = np.asarray(small).astype(np.float32) / 255
        tint /= np.maximum(tint.max(2, keepdims=True), 1e-5)
        alpha = (1 - opacity) * core[:, :, None]
        a = a * (1 - alpha) + tint * alpha
    exposure = shadow + (highlight - shadow) * np.clip((v - .15) / .7, 0, 1)
    # Limit scalar gain to RGB headroom rather than clipping channels independently.
    out = a * np.minimum(exposure, 1 / np.maximum(a.max(2), 1e-5))[:, :, None]
    protected = light_mask if protect_mask is None else np.maximum(light_mask, protect_mask)
    out *= 1 - background * (1 - protected[:, :, None])
    if core_mode == 'white':
        out += (1 - out) * core_strength * core[:, :, None]
    else:
        gain = np.minimum(1 + core_strength * core, 1 / np.maximum(out.max(2), 1e-5))
        out *= gain[:, :, None]
    return image_u8(out)


def save_metadata(output, args, extra=None):
    info = {k: v for k, v in vars(args).items() if k != 'func'}
    info.update(extra or {})
    Path(output).with_suffix('.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')


def render(args):
    if args.end <= args.start:
        raise ValueError('--end must exceed --start')
    out = Path(args.output)
    reserve([out, out.with_suffix('.json')], args.overwrite)
    size = dimensions(args.input)
    if size[0] % 2 or size[1] % 2:
        raise ValueError('H.264 yuv420p needs even dimensions; crop or pad the source first')
    # Keep the decode grid anchored at the requested output start.
    warmup = min(args.frames - 1, int(args.start * args.fps + 1e-6))
    read_start = args.start - warmup / args.fps
    target = round((args.end - args.start) * args.fps)
    if target < 1:
        raise ValueError('Selected interval contains no output frames')
    count = 0
    history = deque(maxlen=args.frames)
    encoder = subprocess.Popen([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{size[0]}x{size[1]}',
        '-r', str(args.fps), '-i', '-', '-ss', str(args.start), '-i', args.input,
        '-map', '0:v:0', '-map', '1:a:0?', '-t', str(target / args.fps),
        '-c:v', 'libx264', '-crf', str(args.crf), '-preset', 'medium',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart', str(out)], stdin=subprocess.PIPE)
    try:
        stream = decode(args.input, read_start, (warmup + target + 1) / args.fps, args.fps, size)
        try:
            for i, frame in enumerate(stream):
                history.append((frame, luma(frame)))
                if i < warmup:
                    continue
                encoder.stdin.write(brightest(history).tobytes())
                count += 1
                if count == target:
                    break
        finally:
            stream.close()
        encoder.stdin.close()
        if encoder.wait() != 0:
            raise RuntimeError('Video encode failed')
    finally:
        if encoder.poll() is None:
            encoder.kill()
        encoder.wait()
    if count != target:
        raise ValueError(f'Source ended early: wrote {count} of {target} frames')
    run(['-i', str(out), '-f', 'null', '-'])
    save_metadata(out, args, {'written_frames': count, 'actual_duration': count / args.fps,
                            'history_span_seconds': (args.frames - 1) / args.fps})
    print(f'{out.resolve()} | {count} frames, {count / args.fps:.3f}s')


def contact(args):
    out = Path(args.output)
    reserve([out], args.overwrite)
    size = dimensions(args.input)
    thumbs = []
    stream = decode(args.input, args.start, args.duration, args.fps, size)
    try:
        for i, frame in enumerate(stream):
            if i % args.step:
                continue
            im = Image.fromarray(frame)
            im.thumbnail((256, 144))
            thumbs.append((im, f'frame {i} | {args.start+i/args.fps:.3f}s'))
            if len(thumbs) == args.max_tiles:
                break
    finally:
        stream.close()
    if not thumbs:
        raise ValueError('No frames in selected interval')
    cols = min(5, len(thumbs))
    sheet = Image.new('RGB', (cols * 256, ((len(thumbs) + cols - 1) // cols) * 164), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    for i, (im, label) in enumerate(thumbs):
        x, y = i % cols * 256, i // cols * 164
        sheet.paste(im, (x, y))
        draw.text((x + 5, y + 146), label, fill='white')
    sheet.save(out)
    print(out.resolve())


def cover(args):
    out = Path(args.output)
    if out.suffix.lower() != '.png':
        raise ValueError('--output must have .png extension')
    raw_path = out.with_name(out.stem + '-source.png')
    trail_path = out.with_name(out.stem + '-ungraded.png')
    mask_path = out.with_name(out.stem + '-light-mask.png')
    paths = [out, out.with_suffix('.jpg'), out.with_suffix('.json'), raw_path, trail_path, mask_path]
    if args.comparison:
        paths.append(Path(args.comparison))
    reserve(paths, args.overwrite)
    size = dimensions(args.input)
    warmup = min(args.frames - 1, int(args.time * args.fps + 1e-6))
    history = deque(maxlen=args.frames)
    stream = decode(args.input, args.time - warmup / args.fps, (warmup + 2) / args.fps, args.fps, size)
    try:
        for frame in stream:
            history.append((frame, luma(frame)))
            if len(history) == warmup + 1:
                break
    finally:
        stream.close()
    if len(history) != warmup + 1:
        raise ValueError('Requested source time is beyond the video')
    raw = Image.fromarray(history[-1][0])
    raw.save(raw_path)
    trail = brightest(history)
    Image.fromarray(trail).save(trail_path)
    light_mask = (load_mask(args.light_mask, size) if args.light_mask else
                  estimate_light_mask(history, args.light_threshold, args.motion_threshold))
    protect_mask = load_mask(args.protect_mask, size) if args.protect_mask else None
    Image.fromarray(np.uint8(np.clip(light_mask, 0, 1) * 255 + .5)).save(mask_path)
    final = grade(trail, args.core, args.shadow, args.highlight, args.background, args.core_opacity,
                  light_mask, protect_mask, args.core_mode)
    final.save(out)
    final.save(out.with_suffix('.jpg'), quality=97, subsampling=0)
    if args.comparison:
        w = min(640, size[0])
        h = round(size[1] * w / size[0])
        sheet = Image.new('RGB', (w * 3, h + 24), (18, 18, 18))
        draw = ImageDraw.Draw(sheet)
        for i, (im, label) in enumerate([(raw, f'Original | {args.time:.3f}s'),
                (Image.fromarray(trail), 'Source-hue trails'), (final, 'Final cover')]):
            sheet.paste(im.resize((w, h), Image.Resampling.LANCZOS), (w * i, 0))
            draw.text((w * i + 8, h + 5), label, fill='white')
        sheet.save(args.comparison)
    save_metadata(out, args, {'history_span_seconds': warmup / args.fps})
    print(out.resolve())


def unit(value):
    n = float(value)
    if not 0 <= n <= 1:
        raise argparse.ArgumentTypeError('Must be between 0 and 1')
    return n


def positive(value):
    n = float(value)
    if n <= 0:
        raise argparse.ArgumentTypeError('Must be positive')
    return n


def positive_int(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError('Must be at least 1')
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('render', 'contact', 'cover'):
        p = sub.add_parser(command)
        p.add_argument('--input', required=True)
        p.add_argument('--output', required=True)
        p.add_argument('--fps', type=positive, help='Override output fps; default: source fps')
        p.add_argument('--overwrite', action='store_true')
        if command in ('render', 'cover'):
            window = p.add_mutually_exclusive_group()
            window.add_argument('--frames', type=positive_int, help='Window including the current frame')
            window.add_argument('--trail-seconds', type=positive,
                                help='Oldest trail age; default 0.4 seconds, rounded to source frames')
        if command == 'render':
            p.add_argument('--start', type=float, required=True)
            p.add_argument('--end', type=float, required=True)
            p.add_argument('--crf', type=int, default=18)
        elif command == 'contact':
            p.add_argument('--start', type=float, default=0)
            p.add_argument('--duration', type=positive, default=10)
            p.add_argument('--step', type=positive_int, default=1)
            p.add_argument('--max-tiles', type=positive_int, default=60)
        else:
            p.add_argument('--time', type=float, required=True)
            p.add_argument('--core', type=unit, default=.25)
            p.add_argument('--core-mode', choices=('preserve', 'white'), default='preserve',
                           help='Preserve source hue, or explicitly whiten light cores')
            p.add_argument('--shadow', type=positive, default=1)
            p.add_argument('--highlight', type=positive, default=1)
            p.add_argument('--background', type=unit, default=.15)
            p.add_argument('--core-opacity', type=unit, default=1)
            p.add_argument('--light-mask', help='Source-size grayscale light mask, overrides automatic mask')
            p.add_argument('--protect-mask', help='Source-size grayscale foreground protection mask')
            p.add_argument('--light-threshold', type=unit, default=.55)
            p.add_argument('--motion-threshold', type=unit, default=.08)
            p.add_argument('--comparison')
        p.set_defaults(func=globals()[command])
    args = parser.parse_args()
    if Path(args.input).resolve() == Path(args.output).resolve():
        parser.error('Output must differ from the source file')
    for attr in ('start', 'time'):
        if getattr(args, attr, 0) < 0:
            parser.error(f'--{attr} must be nonnegative')
    try:
        args.input_fps = source_fps(args.input)
    except ValueError:
        if args.fps is None:
            raise
        args.input_fps = None
    args.fps_policy = 'source' if args.fps is None else 'explicit override'
    if args.fps is None:
        args.fps = args.input_fps
    if args.command in ('render', 'cover') and args.frames is None:
        args.trail_seconds = .4 if args.trail_seconds is None else args.trail_seconds
        args.frames = max(1, round(args.trail_seconds * args.fps) + 1)
    detected = 'unknown' if args.input_fps is None else f'{args.input_fps:.6g}'
    print(f'Source {detected} fps; working {args.fps:.6g} fps ({args.fps_policy})')
    args.func(args)


if __name__ == '__main__':
    main()
