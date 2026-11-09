#!/usr/bin/env python3
"""Derive walkable rectangles for rooms from each SWF's own ``block_mc`` mask.

Method (docs/bots.md "Zonas medidas"): the room engine tests a position against
``block_mc``; a filled pixel is blocked and a transparent one is floor. This
script exports that sprite with FFDec, builds a 4 px grid, erodes it by a safety
margin and keeps the largest rectangles that are entirely floor.

It reads only vendor files and writes JSON to ``--out``. It does not change the
catalog. Needs Java, FFDec (.local/tools/ffdec-*) and Pillow.

    python3 scripts/measure-room-masks.py --rooms 330,340 --out /tmp/rooms-measured.json
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'containers/bots/bots/data/rooms.json'
ROOMS_DIR = ROOT / 'vendor/legacy-media/media/play/v2/content/global/rooms'
CELL = 4
STAGE_W, STAGE_H = 760, 480


def find_ffdec():
    jars = sorted((ROOT / '.local/tools').glob('ffdec-*/ffdec-cli.jar'))
    if not jars:
        sys.exit('FFDec not found under .local/tools (see docs/bots.md)')
    return jars[-1]


def find_java():
    for candidate in (shutil.which('java'),
                      '/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin/java'):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit('java not found')


def block_mc_placement(xml_path):
    """characterId and translation (px) of the PlaceObject named block_mc."""
    text = xml_path.read_text(encoding='utf-8')
    match = re.search(r'<item type="PlaceObject\dTag" characterId="(\d+)"[^>]*name="block_mc"[^>]*>\s*'
                      r'<matrix type="MATRIX"([^>]*)/>', text)
    if not match:
        return None
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', match.group(2)))
    if attrs.get('hasScale') == 'true' or attrs.get('hasRotate') == 'true':
        return None
    return int(match.group(1)), float(attrs.get('translateX', 0)) / 20, float(attrs.get('translateY', 0)) / 20


def export_mask(java, ffdec, swf, char_id, workdir):
    out = workdir / f'sprite_{char_id}'
    subprocess.run([java, '-jar', str(ffdec), '-selectid', str(char_id), '-format', 'sprite:png',
                    '-export', 'sprite', str(out), str(swf)], check=True, capture_output=True, timeout=180)
    pngs = sorted(out.rglob('1.png'))
    if not pngs:
        raise RuntimeError('FFDec produced no PNG')
    return pngs[0]


def largest_rectangles(floor, limit=4, min_share=0.12):
    """Greedy: biggest all-floor rectangle, remove its cells, repeat."""
    rows, cols = len(floor), len(floor[0])
    free = [row[:] for row in floor]
    found, first_area = [], 0
    for _ in range(limit):
        best, heights = (0, 0, 0, 0, 0), [0] * cols
        for r in range(rows):
            for c in range(cols):
                heights[c] = heights[c] + 1 if free[r][c] else 0
            stack = []
            for c in range(cols + 1):
                h = heights[c] if c < cols else 0
                start = c
                while stack and stack[-1][1] >= h:
                    s, sh = stack.pop()
                    area = sh * (c - s)
                    if area > best[0]:
                        best = (area, s, c - 1, r - sh + 1, r)
                    start = s
                stack.append((start, h))
        area, c0, c1, r0, r1 = best
        if area == 0 or (found and area < first_area * min_share):
            break
        first_area = first_area or area
        found.append((c0, c1, r0, r1))
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                free[r][c] = False
    return found


def grow_from(point, floor):
    """All-floor rectangle grown outward from one cell (used when the spawn is in no big rectangle)."""
    rows, cols = len(floor), len(floor[0])
    c, r = point[0] // CELL, point[1] // CELL
    if not (0 <= r < rows and 0 <= c < cols and floor[r][c]):
        return None
    c0 = c1 = c
    r0 = r1 = r
    grew = True
    while grew:
        grew = False
        if c0 > 0 and all(floor[rr][c0 - 1] for rr in range(r0, r1 + 1)):
            c0 -= 1
            grew = True
        if c1 < cols - 1 and all(floor[rr][c1 + 1] for rr in range(r0, r1 + 1)):
            c1 += 1
            grew = True
        if r0 > 0 and all(floor[r0 - 1][cc] for cc in range(c0, c1 + 1)):
            r0 -= 1
            grew = True
        if r1 < rows - 1 and all(floor[r1 + 1][cc] for cc in range(c0, c1 + 1)):
            r1 += 1
            grew = True
    return c0, c1, r0, r1


def measure(room_id, swf_name, spawn, margin, java, ffdec):
    swf = ROOMS_DIR / swf_name
    if not swf.is_file():
        return {'ok': False, 'why': f'{swf_name} missing'}
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        xml = work / 'room.xml'
        subprocess.run([java, '-jar', str(ffdec), '-swf2xml', str(swf), str(xml)],
                       check=True, capture_output=True, timeout=300)
        placed = block_mc_placement(xml)
        if placed is None:
            return {'ok': False, 'why': 'no unscaled block_mc placement'}
        char_id, tx, ty = placed
        image = Image.open(export_mask(java, ffdec, swf, char_id, work)).convert('RGBA')
        width, height = image.size
        pixels = image.load()

        def blocked(x, y):  # stage coordinates
            px, py = int(round(x - tx)), int(round(y - ty))
            if not (0 <= px < width and 0 <= py < height):
                return True
            return pixels[px, py][3] > 0

        cols, rows = STAGE_W // CELL, STAGE_H // CELL
        floor = [[not any(blocked(c * CELL + dx, r * CELL + dy) for dx in (0, CELL - 1) for dy in (0, CELL - 1))
                  for c in range(cols)] for r in range(rows)]
        reach = margin // CELL
        eroded = [[floor[r][c] and all(
            floor[rr][cc] for rr in range(max(0, r - reach), min(rows, r + reach + 1))
            for cc in range(max(0, c - reach), min(cols, c + reach + 1))) for c in range(cols)]
            for r in range(rows)]
        rects = [{'min_x': c0 * CELL, 'max_x': (c1 + 1) * CELL - 1, 'min_y': r0 * CELL, 'max_y': (r1 + 1) * CELL - 1}
                 for c0, c1, r0, r1 in largest_rectangles(eroded)]
        if spawn and not any(r['min_x'] <= spawn['x'] <= r['max_x'] and r['min_y'] <= spawn['y'] <= r['max_y']
                             for r in rects):
            grown = grow_from((spawn['x'], spawn['y']), eroded) or grow_from((spawn['x'], spawn['y']), floor)
            if grown:
                c0, c1, r0, r1 = grown
                rects.append({'min_x': c0 * CELL, 'max_x': (c1 + 1) * CELL - 1,
                              'min_y': r0 * CELL, 'max_y': (r1 + 1) * CELL - 1})
        spawn_ok = bool(spawn) and not blocked(spawn['x'], spawn['y'])
        spawn_in = any(r['min_x'] <= spawn['x'] <= r['max_x'] and r['min_y'] <= spawn['y'] <= r['max_y']
                       for r in rects) if spawn else False
        return {'ok': bool(rects), 'block_mc_id': char_id, 'translate': [tx, ty], 'image': [width, height],
                'floor_share': round(sum(map(sum, floor)) / (cols * rows), 3), 'spawn_on_floor': spawn_ok,
                'spawn_in_rect': spawn_in, 'rects': rects}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--rooms', required=True, help='comma-separated room ids')
    parser.add_argument('--margin', type=int, default=12, help='safety inset in px (default 12)')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))['rooms']
    java, ffdec = find_java(), find_ffdec()
    result = {}
    for room_id in [x.strip() for x in args.rooms.split(',') if x.strip()]:
        room = catalog[room_id]
        try:
            result[room_id] = {'name': room['name'], 'swf': room['swf'],
                               **measure(int(room_id), room['swf'], room.get('spawn'), args.margin, java, ffdec)}
        except Exception as error:  # report per room, keep going
            result[room_id] = {'name': room['name'], 'ok': False, 'why': f'{type(error).__name__}: {error}'}
        print(room_id, room['name'], 'ok' if result[room_id].get('ok') else 'FAILED',
              result[room_id].get('why', ''), flush=True)
    Path(args.out).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
