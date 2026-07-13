#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Конвертация PTS → L2J и обратно."""

import glob
import os
import re
import struct
import time

from .formats import BLOCKS, PTS_HEADER, GeoError
from .ui import bold, dim, green, progress, red, yellow


def convert_file(src, dst):
    """PTS conv.dat → l2j (потоковое перекодирование). Возвращает (flat, cx, ml)."""
    data = open(src, 'rb').read()
    out = bytearray()
    pos = PTS_HEADER
    stats = [0, 0, 0]
    try:
        for _ in range(BLOCKS):
            (t,) = struct.unpack_from('<H', data, pos); pos += 2
            if t == 0x0000:                       # flat: min/max → высота поверхности (max)
                _mn, mx = struct.unpack_from('<hh', data, pos); pos += 4
                out.append(0)
                out += struct.pack('<h', mx)
                stats[0] += 1
            elif t == 0x0040:                     # complex: кодировка ячеек совпадает
                out.append(1)
                out += data[pos:pos + 128]; pos += 128
                stats[1] += 1
            else:                                 # multilayer: u16-счётчик → u8
                out.append(2)
                for _cell in range(64):
                    (nl,) = struct.unpack_from('<H', data, pos); pos += 2
                    if nl == 0 or nl > 125:
                        raise GeoError(f'битый счётчик слоёв {nl} @0x{pos - 2:x}')
                    out.append(nl)
                    out += data[pos:pos + nl * 2]; pos += nl * 2
                stats[2] += 1
    except struct.error:
        raise GeoError(f'неожиданный конец файла @0x{pos:x}')
    if pos != len(data):
        raise GeoError(f'разобрано {pos} байт, размер файла {len(data)} — формат не PTS?')
    open(dst, 'wb').write(out)
    return stats


def validate_l2j(path):
    """Парсер, зеркалящий Region.load() движка: должен дойти ровно до EOF."""
    data = open(path, 'rb').read()
    pos = 0
    for b in range(BLOCKS):
        t = data[pos]; pos += 1
        if t == 0:
            pos += 2
        elif t == 1:
            pos += 128
        elif t == 2:
            for _cell in range(64):
                nl = data[pos]; pos += 1
                if nl == 0 or nl > 125:
                    raise GeoError(f'блок {b}: битый счётчик слоёв {nl}')
                pos += nl * 2
        else:
            raise GeoError(f'блок {b}: неизвестный тип {t}')
    if pos != len(data):
        raise GeoError(f'разобрано {pos} байт, размер файла {len(data)}')


def _confirm_overwrite(names, out_dir, assume_yes):
    existing = [n for n in names if os.path.exists(os.path.join(out_dir, n))]
    if existing and not assume_yes:
        ans = input(f'  {yellow("⚠")} {len(existing)} файлов уже существуют в {out_dir}.'
                    f' Перезаписать? [y/N] ')
        if ans.strip().lower() not in ('y', 'yes', 'д', 'да'):
            print(dim('  отменено.'))
            return False
    return True


def cmd_convert(paths, out_dir, assume_yes=False):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, '*_conv.dat')))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(red(f'  ✗ не найдено: {p}'))
    # имя сохраняется: 27_24_Classic_conv.dat → 27_24_Classic.l2j
    jobs = {}
    for f in files:
        base = os.path.basename(f)
        stem = base[:-len('_conv.dat')] if base.endswith('_conv.dat') else os.path.splitext(base)[0]
        jobs[stem + '.l2j'] = f
    if not jobs:
        print(red('  ✗ нечего конвертировать.'))
        return 1
    print(f'\n  {bold("План:")} {green(str(len(jobs)))} регионов → {out_dir}')
    if not _confirm_overwrite(jobs, out_dir, assume_yes):
        return 1
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    totals = [0, 0, 0]
    errors = []
    names = sorted(jobs)
    for i, name in enumerate(names, 1):
        dst = os.path.join(out_dir, name)
        try:
            st = convert_file(jobs[name], dst)
            validate_l2j(dst)
            for j in range(3):
                totals[j] += st[j]
        except GeoError as e:
            errors.append((name, str(e)))
            # dst может совпадать с исходником (перепутанное направление
            # конвертации + вывод в ту же папку) — исходник не трогаем
            if os.path.exists(dst) and \
                    os.path.realpath(dst) != os.path.realpath(jobs[name]):
                os.remove(dst)
        progress(i, len(names), 'конвертация ')
    print(f'\n  {bold("Итог")} за {time.time() - t0:.1f} с:')
    print(f'    {green("✓")} успешно: {green(str(len(names) - len(errors)))} файлов'
          f' (валидация парсером движка пройдена)')
    print(f'    блоки: flat {totals[0]:,} · complex {totals[1]:,} · multilayer {totals[2]:,}'.replace(',', ' '))
    if errors:
        print(f'    {red("✗")} ошибки: {red(str(len(errors)))}')
        for name, why in errors[:10]:
            print(f'      {red("✗")} {name}: {why}')
    return 2 if errors else 0


def l2j2pts_file(src, dst, rx, ry):
    """l2j → PTS потоково, с сохранением типа каждого блока исходника.

    Маркер multilayer-блока в PTS — суммарное число слоёв блока.
    Вырожденный multilayer, где все 64 ячейки одно-слойные, даёт
    сумму 64 == 0x40 и был бы неотличим от complex — такой блок кодируется
    как complex (семантика идентична; в реальной геодате таких блоков нет).
    Возвращает (flat, cx, ml).
    """
    data = open(src, 'rb').read()
    body = bytearray()
    pos = 0
    n_flat = n_cx = n_ml = 0
    try:
        for b in range(BLOCKS):
            t = data[pos]; pos += 1
            if t == 0:
                (h,) = struct.unpack_from('<h', data, pos); pos += 2
                body += struct.pack('<Hhh', 0x0000, h, h)
                n_flat += 1
            elif t == 1:
                body += struct.pack('<H', 0x0040)
                body += data[pos:pos + 128]; pos += 128
                n_cx += 1
            elif t == 2:
                cells = bytearray()
                total = 0
                single = bytearray()  # ячейки как complex, если все одно-слойные
                for _cell in range(64):
                    nl = data[pos]; pos += 1
                    if nl == 0 or nl > 125:
                        raise GeoError(f'блок {b}: битый счётчик слоёв {nl}')
                    cells += struct.pack('<H', nl)
                    cells += data[pos:pos + nl * 2]
                    if nl == 1:
                        single += data[pos:pos + 2]
                    total += nl
                    pos += nl * 2
                if total == 64:
                    # вырожденный multilayer → complex (см. докстринг)
                    body += struct.pack('<H', 0x0040) + single
                    n_cx += 1
                else:
                    body += struct.pack('<H', total) + cells
                    n_ml += 1
            else:
                raise GeoError(f'блок {b}: неизвестный тип {t}')
    except (struct.error, IndexError):
        raise GeoError(f'неожиданный конец файла @0x{pos:x}')
    if pos != len(data):
        raise GeoError(f'разобрано {pos} байт, размер файла {len(data)} — формат не l2j?')
    hdr = struct.pack('<BBBBBBIII', rx, ry, 0x80, 0x00, 0x10, 0x00,
                      (n_cx + n_ml) * 64, n_flat + n_cx, n_flat)
    open(dst, 'wb').write(hdr + body)
    return n_flat, n_cx, n_ml


def cmd_l2j2pts(paths, out_dir, assume_yes=False):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, '*.l2j')))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(red(f'  ✗ не найдено: {p}'))
    # имя сохраняется: 27_24_Classic.l2j → 27_24_Classic_conv.dat;
    # координаты региона (заголовок PTS) — из первых двух чисел имени
    jobs, skipped = {}, []
    for f in files:
        base = os.path.basename(f)
        m = re.match(r'^(\d+)_(\d+)', base)
        # заголовок PTS кодирует координаты как u8 — числа >255 не координаты
        if not m or int(m.group(1)) > 255 or int(m.group(2)) > 255:
            skipped.append(f)
            continue
        stem = base[:-len('.l2j')] if base.endswith('.l2j') else os.path.splitext(base)[0]
        jobs[stem + '_conv.dat'] = (f, int(m.group(1)), int(m.group(2)))
    if skipped:
        print(yellow(f'  ⚠ ПРОПУЩЕНО {len(skipped)} файлов — имя не начинается с XX_YY,'
                     f' координаты региона не извлечь:'))
        for f in skipped[:20]:
            print(yellow(f'      {os.path.basename(f)}'))
    if not jobs:
        print(red('  ✗ файлы .l2j не найдены.'))
        return 1
    print(f'  {bold("Обратная конвертация")} {len(jobs)} файлов → {out_dir}')
    if not _confirm_overwrite(jobs, out_dir, assume_yes):
        return 1
    os.makedirs(out_dir, exist_ok=True)
    errors = []
    names = sorted(jobs)
    for i, name in enumerate(names, 1):
        src, rx, ry = jobs[name]
        try:
            l2j2pts_file(src, os.path.join(out_dir, name), rx, ry)
        except GeoError as e:
            errors.append((name, str(e)))
        progress(i, len(names), 'конвертация ')
    print(f'\n  {green("✓")} готово: {len(names) - len(errors)} файлов.'
          f' Заголовки PTS восстановлены (счётчики блоков).')
    if errors:
        print(f'  {red("✗")} ошибки: {len(errors)}')
        for name, why in errors[:10]:
            print(f'      {red("✗")} {name}: {why}')
    if errors:
        return 2
    return 3 if skipped else 0
