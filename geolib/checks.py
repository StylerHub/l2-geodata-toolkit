#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка наборов и сверка PTS ↔ L2J."""

import glob
import os
import struct

from .formats import BLOCKS, GeoError, block_type, parse_region
from .ui import bold, green, progress, red, yellow

def cmd_check(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, '*.l2j'))) + \
                     sorted(glob.glob(os.path.join(p, '*_conv.dat')))
        else:
            files.append(p)
    if not files:
        print(red('  ✗ файлы не найдены.'))
        return 1
    print(f'  {bold("Проверка")} {len(files)} файлов…\n')
    n_ok = n_stub = n_bad = 0
    stubs, bads = [], []
    for i, f in enumerate(files, 1):
        name = os.path.basename(f)
        try:
            blocks = parse_region(f)
            flat = sum(1 for b in blocks if block_type(b) == 0)
            heights = {b[0][0][0] for b in blocks if block_type(b) == 0}
            if flat == BLOCKS and len(heights) <= 2:
                stubs.append((name, f'весь регион — плоскость ({sorted(heights)})'))
                n_stub += 1
            else:
                n_ok += 1
        except GeoError as e:
            bads.append((name, str(e)))
            n_bad += 1
        except (struct.error, IndexError):
            bads.append((name, 'файл повреждён или обрезан'))
            n_bad += 1
        progress(i, len(files), 'проверка ')
    print()
    print(f'  {green("✓")} корректных: {green(str(n_ok))}')
    if stubs:
        print(f'  {yellow("⚠")} заглушек (плоских): {yellow(str(n_stub))}')
        for n, why in stubs:
            print(f'      {yellow("⚠")} {n}: {why}')
    if bads:
        print(f'  {red("✗")} битых: {red(str(n_bad))}')
        for n, why in bads:
            print(f'      {red("✗")} {n}: {why}')
    return 0 if not bads else 2


def cmd_verify(pts_dir, l2j_dir):
    """Поячеечная сверка пар XX_YY_conv.dat ↔ XX_YY.l2j.

    Направление конвертации не имеет значения (оба формата парсятся
    в одну структуру), папки можно передавать в любом порядке —
    где PTS, а где L2J, определяется по содержимому."""
    import glob as _g
    if not _g.glob(os.path.join(pts_dir, '*_conv.dat')) and \
            _g.glob(os.path.join(l2j_dir, '*_conv.dat')):
        pts_dir, l2j_dir = l2j_dir, pts_dir
    # Пары по имени: X_conv.dat ↔ X.l2j (конвертация сохраняет имя,
    # 27_24_Classic_conv.dat ↔ 27_24_Classic.l2j).
    pairs = []
    unpaired = 0
    for f in sorted(glob.glob(os.path.join(pts_dir, '*_conv.dat'))):
        stem = os.path.basename(f)[:-len('_conv.dat')]
        dst = os.path.join(l2j_dir, stem + '.l2j')
        if os.path.exists(dst):
            pairs.append((stem, f, dst))
        else:
            unpaired += 1
    if unpaired:
        print(yellow(f'  ⚠ без пары .l2j (не будут сверены): {unpaired}'))
    if not pairs:
        print(red('  ✗ пар PTS↔L2J не найдено.'))
        return 1
    print(f'  {bold("Сверка")} {len(pairs)} регионов (семантика: высоты, слои, NSWE)…\n')
    n_ok = n_diff = 0
    for i, (key, src, dst) in enumerate(pairs, 1):
        try:
            a = parse_region(src, 'pts')
            b = parse_region(dst, 'l2j')
        except GeoError as e:
            n_diff += 1
            print('\r  ' + red(f'✗ {key}: битый файл ({e})') + ' ' * 20)
            progress(i, len(pairs), 'сверка ')
            continue
        except (struct.error, IndexError, ValueError):
            n_diff += 1
            print('\r  ' + red(f'✗ {key}: битый файл (повреждён или обрезан)') + ' ' * 20)
            progress(i, len(pairs), 'сверка ')
            continue
        diffs = 0
        for blk_a, blk_b in zip(a, b):
            for cell_a, cell_b in zip(blk_a, blk_b):
                if cell_a != cell_b:
                    diffs += 1
        if diffs:
            n_diff += 1
            print(f'\r  {red("✗")} {key}: расходится {diffs} ячеек' + ' ' * 30)
        else:
            n_ok += 1
        progress(i, len(pairs), 'сверка ')
    print()
    if n_diff == 0:
        print(f'  {green("✓")} все {n_ok} регионов идентичны исходникам — конвертация без потерь.')
    else:
        print(f'  {green("✓")} совпадает: {n_ok}   {red("✗")} расходится: {n_diff}')
    return 0 if n_diff == 0 else 2
