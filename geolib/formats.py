#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Форматы геодаты L2J и PTS: парсинг, кодировка ячеек, сводки.

L2J : блок u8 тип (0 flat: i16; 1 complex: 64×i16; 2 multi: 64×[u8 n + n×i16])
PTS : заголовок 18Б; блок u16 тип (0 flat: 2×i16 min/max; 0x40 complex: 64×i16;
      иначе multi: 64×[u16 n + n×i16]; сам тип-u16 multilayer-блока равен
      суммарному числу слоёв блока — проверено на всех реальных файлах)
Ячейка (оба формата): (height<<1)&0xFFF0 | nswe(4 бита: 8=N 4=S 2=W 1=E)
Заголовок PTS: u8 rx, u8 ry, "80 00 10 00",
      u32 неflat-ячеек ((cx+ml)*64), u32 flat+cx блоков, u32 flat блоков.
"""

import os
import re
import struct

BLOCKS = 65536
PTS_HEADER = 18


class GeoError(Exception):
    pass


def dec_h(v):
    """Кодированная ячейка → высота."""
    return struct.unpack('<h', struct.pack('<H', v & 0xFFF0))[0] >> 1


def dec_nswe(v):
    return v & 0x000F


def sniff_format(path):
    """'l2j' | 'pts' | None по имени и содержимому."""
    name = os.path.basename(path)
    if name.endswith('.l2j'):
        return 'l2j'
    if name.endswith('.dat'):
        return 'pts'
    return None


def region_of(path):
    m = re.match(r'^(\d+)_(\d+)', os.path.basename(path))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def parse_region(path, fmt=None):
    """Файл → список из 65536 блоков; блок = список из 64 ячеек;
    ячейка = список слоёв (h, nswe). Формат определяется автоматически."""
    fmt = fmt or sniff_format(path)
    data = open(path, 'rb').read()
    blocks = []
    if fmt == 'l2j':
        pos = 0
        for b in range(BLOCKS):
            t = data[pos]; pos += 1
            if t == 0:
                # ВАЖНО: все 64 ячейки разделяют один объект-список — данные
                # read-only; при мутации сначала копировать
                (h,) = struct.unpack_from('<h', data, pos); pos += 2
                blocks.append([[(h, 15)]] * 64)
            elif t == 1:
                cells = struct.unpack_from('<64H', data, pos); pos += 128
                blocks.append([[(dec_h(v), dec_nswe(v))] for v in cells])
            elif t == 2:
                cells = []
                for _ in range(64):
                    nl = data[pos]; pos += 1
                    if nl == 0 or nl > 125:
                        raise GeoError(f'блок {b}: битый счётчик слоёв {nl}')
                    ls = struct.unpack_from(f'<{nl}H', data, pos); pos += nl * 2
                    cells.append([(dec_h(v), dec_nswe(v)) for v in ls])
                blocks.append(cells)
            else:
                raise GeoError(f'блок {b}: неизвестный тип {t}')
    elif fmt == 'pts':
        pos = PTS_HEADER
        for b in range(BLOCKS):
            (t,) = struct.unpack_from('<H', data, pos); pos += 2
            if t == 0x0000:
                # разделяемый список — см. комментарий в ветке l2j
                _mn, mx = struct.unpack_from('<hh', data, pos); pos += 4
                blocks.append([[(mx, 15)]] * 64)
            elif t == 0x0040:
                cells = struct.unpack_from('<64H', data, pos); pos += 128
                blocks.append([[(dec_h(v), dec_nswe(v))] for v in cells])
            else:
                cells = []
                for _ in range(64):
                    (nl,) = struct.unpack_from('<H', data, pos); pos += 2
                    if nl == 0 or nl > 125:
                        raise GeoError(f'блок {b}: битый счётчик слоёв {nl}')
                    ls = struct.unpack_from(f'<{nl}H', data, pos); pos += nl * 2
                    cells.append([(dec_h(v), dec_nswe(v)) for v in ls])
                blocks.append(cells)
    else:
        raise GeoError(f'неизвестный формат: {path}')
    return blocks


def block_type(block):
    """0 flat / 1 complex / 2 multilayer по содержимому."""
    if any(len(cell) > 1 for cell in block):
        return 2
    hs = {cell[0] for cell in block}
    return 0 if len(hs) == 1 and block[0][0][1] == 15 else 1


def summarize(blocks):
    """Сводка по блокам для карты: тип, min/max высот (все слои),
    макс. слоёв, min высоты ПОВЕРХНОСТИ (для нормировки палитры)."""
    types, hmin, hmax, lmax, smin = [], [], [], [], []
    for blk in blocks:
        t = block_type(blk)
        mn, mx, lm, sm = 32767, -32768, 0, 32767
        for cell in blk:
            lm = max(lm, len(cell))
            top = -32768
            for h, _ in cell:
                if h < mn:
                    mn = h
                if h > mx:
                    mx = h
                if h > top:
                    top = h
            if top < sm:
                sm = top
        types.append(t); hmin.append(mn); hmax.append(mx); lmax.append(lm); smin.append(sm)
    return types, hmin, hmax, lmax, smin


def enc_cell(h, nswe):
    return ((h << 1) & 0xFFF0) | (nswe & 0xF)
