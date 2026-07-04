#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сравнение двух наборов геодаты: сводный отчёт и детальный разбор региона."""

import glob
import os
import random
import re
import struct

from .formats import BLOCKS, PTS_HEADER, GeoError, parse_region, sniff_format
from .ui import bold, cyan, dim, green, progress, red, yellow

SAMPLE_BLOCKS = 1200   # блоков на регион в сводном режиме (детерминированная выборка)
NEAR = 16              # |Δh| ≤ NEAR считается совпадением (один шаг высоты)
FAR = 64               # |Δh| > FAR — «другой рельеф»


def region_files(d):
    """Канонические файлы папки: имя региона → путь (.l2j приоритетнее)."""
    out = {}
    for f in sorted(glob.glob(os.path.join(d, '*.l2j'))) + \
             sorted(glob.glob(os.path.join(d, '*_conv.dat'))):
        m = re.match(r'^(\d+_\d+)(\.l2j|_conv\.dat)$', os.path.basename(f))
        if m:
            out.setdefault(m.group(1), f)
    return out


def _dec_h(v):
    return struct.unpack('<h', struct.pack('<H', v & 0xFFF0))[0] >> 1


def sampled_surface(path, wanted):
    """Высоты поверхности для выбранных блоков (по 8 ячеек с блока).

    Проходит файл потоково, декодирует только нужные блоки —
    на порядок быстрее полного parse_region."""
    fmt = sniff_format(path)
    data = open(path, 'rb').read()
    pos = PTS_HEADER if fmt == 'pts' else 0
    res = {}
    for b in range(BLOCKS):
        take = b in wanted
        if fmt == 'l2j':
            t = data[pos]; pos += 1
            if t == 0:
                if take:
                    (h,) = struct.unpack_from('<h', data, pos)
                    res[b] = [h] * 8
                pos += 2
            elif t == 1:
                if take:
                    cells = struct.unpack_from('<64H', data, pos)
                    res[b] = [_dec_h(cells[c]) for c in range(0, 64, 8)]
                pos += 128
            else:
                hs = []
                for c in range(64):
                    nl = data[pos]; pos += 1
                    if take and c % 8 == 0:
                        layers = struct.unpack_from(f'<{nl}H', data, pos)
                        hs.append(max(_dec_h(l) for l in layers))
                    pos += nl * 2
                if take:
                    res[b] = hs
        else:  # pts
            (t,) = struct.unpack_from('<H', data, pos); pos += 2
            if t == 0x0000:
                if take:
                    _mn, mx = struct.unpack_from('<hh', data, pos)
                    res[b] = [mx] * 8
                pos += 4
            elif t == 0x0040:
                if take:
                    cells = struct.unpack_from('<64H', data, pos)
                    res[b] = [_dec_h(cells[c]) for c in range(0, 64, 8)]
                pos += 128
            else:
                hs = []
                for c in range(64):
                    (nl,) = struct.unpack_from('<H', data, pos); pos += 2
                    if take and c % 8 == 0:
                        layers = struct.unpack_from(f'<{nl}H', data, pos)
                        hs.append(max(_dec_h(l) for l in layers))
                    pos += nl * 2
                if take:
                    res[b] = hs
    return res


def _world(region, bx, by):
    rx, ry = map(int, region.split('_'))
    return (rx - 20) * 32768 + bx * 128, (ry - 18) * 32768 + by * 128


def _detail(region, fa, fb, name_a, name_b):
    """Полный поблочный разбор одного региона + ASCII-карта расхождений."""
    print(f'  {bold(region)}: полный разбор (все 4,2 млн ячеек)…')
    try:
        a = parse_region(fa)
    except (GeoError, struct.error, IndexError, ValueError) as e:
        print(red(f'  ✗ битый файл в A ({os.path.basename(fa)}): {e}'))
        return 1
    try:
        b = parse_region(fb)
    except (GeoError, struct.error, IndexError, ValueError) as e:
        print(red(f'  ✗ битый файл в B ({os.path.basename(fb)}): {e}'))
        return 1
    surf = lambda cell: max(h for h, _ in cell)
    grid = [0] * BLOCKS           # максимальный |Δh| поверхности по блоку
    n_cells = n_same = n_near = 0
    for i in range(BLOCKS):
        m = 0
        for ca, cb in zip(a[i], b[i]):
            dh = surf(ca) - surf(cb)
            n_cells += 1
            if dh == 0:
                n_same += 1
            if abs(dh) <= NEAR:
                n_near += 1
            if abs(dh) > abs(m):
                m = dh
        grid[i] = m
    n_far_blocks = sum(1 for m in grid if abs(m) > FAR)
    print(f'\n  ячеек: {n_cells:,} · точное совпадение {n_same / n_cells:.1%}'
          f' · в пределах ±{NEAR}: {n_near / n_cells:.1%}'.replace(',', ' '))
    print(f'  блоков с |Δh| > {FAR}: {n_far_blocks} ({n_far_blocks / 655.36:.1f}%)')

    # кластеры: суперблоки 16×16 с наибольшим числом расходящихся блоков
    sup = {}
    for bx in range(256):
        for by in range(256):
            if abs(grid[bx * 256 + by]) > FAR:
                sup[(bx // 16, by // 16)] = sup.get((bx // 16, by // 16), 0) + 1
    if sup:
        print(f'\n  {bold("Очаги расхождений")} (мировые координаты центров):')
        for (sx, sy), cnt in sorted(sup.items(), key=lambda kv: -kv[1])[:5]:
            wx, wy = _world(region, sx * 16 + 8, sy * 16 + 8)
            print(f'    ~({wx}, {wy}) — {cnt}/256 блоков ({cnt / 2.56:.0f}%)')

    print(f'\n  Карта (32×32, `#` |Δh|>{FAR}, `+` |Δh|>{NEAR}, `·` совпадает);'
          f' X → восток, Y ↓ юг:')
    for sy in range(0, 256, 8):
        row = '  '
        for sx in range(0, 256, 8):
            worst = max((abs(grid[(sx + dx) * 256 + sy + dy])
                         for dx in range(8) for dy in range(8)))
            row += '#' if worst > FAR else ('+' if worst > NEAR else '·')
        print(cyan(row))
    print(f'\n  {dim(f"A = {name_a}, B = {name_b}; Δh = A − B (поверхность)")}')
    return 0


def cmd_diff(dir_a, dir_b, region=None):
    fa_all, fb_all = region_files(dir_a), region_files(dir_b)
    if not fa_all or not fb_all:
        print(red('  ✗ в одной из папок нет файлов геодаты (канон XX_YY.l2j / XX_YY_conv.dat).'))
        return 1
    name_a, name_b = (os.path.basename(os.path.normpath(d)) for d in (dir_a, dir_b))
    common = sorted(set(fa_all) & set(fb_all))
    only_a = sorted(set(fa_all) - set(fb_all))
    only_b = sorted(set(fb_all) - set(fa_all))

    if region:
        if region not in common:
            print(red(f'  ✗ регион {region} отсутствует в одной из папок.'))
            return 1
        return _detail(region, fa_all[region], fb_all[region], name_a, name_b)

    print(f'  {bold("Сравнение:")} A = {name_a} ({len(fa_all)}) · B = {name_b} ({len(fb_all)})'
          f' · общих регионов: {len(common)}\n')
    rnd = random.Random(42)
    sample = set(rnd.sample(range(BLOCKS), SAMPLE_BLOCKS))
    rows = []
    for i, r in enumerate(common, 1):
        try:
            ha = sampled_surface(fa_all[r], sample)
        except (GeoError, struct.error, IndexError, ValueError):
            rows.append((r, -1.0, 'A'))
            progress(i, len(common), 'сравнение ')
            continue
        try:
            hb = sampled_surface(fb_all[r], sample)
        except (GeoError, struct.error, IndexError, ValueError):
            rows.append((r, -1.0, 'B'))
            progress(i, len(common), 'сравнение ')
            continue
        n = ok = 0
        worst = 0
        for blk in sample:
            for x, y in zip(ha[blk], hb[blk]):
                n += 1
                d = abs(x - y)
                if d <= NEAR:
                    ok += 1
                if d > worst:
                    worst = d
        rows.append((r, ok / n if n else 0.0, worst))
        progress(i, len(common), 'сравнение ')

    rows.sort(key=lambda x: x[1])
    print(f'\n  {bold("Регионы по степени расхождения")} (совпадение = |Δh| ≤ {NEAR}):')
    print(f'  {"регион":8} {"совпад.":>8} {"макс|Δh|":>9}')
    shown = 0
    for r, pct, worst in rows:
        if pct < 0:
            side = name_a if worst == 'A' else name_b
            print('  ' + red(f'{r:8} битый файл в папке {worst} ({side})'))
            continue
        if pct >= 0.99 and shown >= 15:
            continue  # хвост идентичных не печатаем целиком
        mark = green('✓') if pct >= 0.99 else (yellow('~') if pct >= 0.90 else red('✗'))
        print(f'  {r:8} {pct:>8.1%} {worst:>9} {mark}')
        shown += 1
    n_id = sum(1 for _, p, _ in rows if p >= 0.99)
    n_near_ = sum(1 for _, p, _ in rows if 0.90 <= p < 0.99)
    n_far_ = sum(1 for _, p, _ in rows if 0 <= p < 0.90)
    n_bad = sum(1 for _, p, _ in rows if p < 0)
    print(f'\n  {bold("Итог:")} {green(f"✓ идентичных: {n_id}")} · '
          f'{yellow(f"~ близких: {n_near_}")} · {red(f"✗ расходятся: {n_far_}")}'
          + (f' · {red(f"битых файлов: {n_bad}")}' if n_bad else ''))
    if only_a:
        print(f'  только в A ({name_a}): {len(only_a)} — {" ".join(only_a[:12])}'
              + (' …' if len(only_a) > 12 else ''))
    if only_b:
        print(f'  только в B ({name_b}): {len(only_b)} — {" ".join(only_b[:12])}'
              + (' …' if len(only_b) > 12 else ''))
    print(dim(f'\n  детальный разбор региона: geotool.py diff <A> <B> --region XX_YY'))
    return 2 if n_bad else 0
