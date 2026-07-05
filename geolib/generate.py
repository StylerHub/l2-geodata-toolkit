#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генерация геодаты из файлов клиента L2 (этап 1: рельеф).

Источники: Maps/XX_YY.unr (TerrainInfo: позиция, масштаб, дыры) и
Textures/T_XX_YY.utx (G16 heightmap 256×256, 128 юнитов на тексель).

Высота вершины рельефа (UE2): z = Location.z + (h16 − 32768) · Scale.z / 256.
Ячейка геодаты 16 юнитов → 8×8 ячеек на тексель, высота — билинейная
интерполяция углов текселя.
"""

import math
import os
import struct
import sys

from .formats import BLOCKS, GeoError
from .unreal import Package, Reader, read_properties

CELLS = 2048                       # ячеек на регион по оси
# Калибровка конвенции высот: чистая формула UE2 даёт поверхность на ~43 юнита
# ниже, чем у типовых генераторов (и чем ожидает серверная экосистема).
# Эмпирика: океан клиента (h16=0x4040, Scale.Z=76, Loc.Z=160.65) → UE2 −4684.35,
# типовой генератор −4641. Подтверждено ретейл-спавнами (7.6k точек, 15 регионов).
Z_CALIBRATION = 43.35
# Асимметричные пороги шага (калибровка по-ячеечным сравнением с эталонной 
# паком, 568k ячеек: 90.4% точного совпадения NSWE):
# подъём > UP_STEP закрыт, спуск > DOWN_STEP закрыт (падать можно глубже,
# чем запрыгивать — как в клиенте).
UP_STEP = 16
DOWN_STEP = 96


def _step_ok(dz_up, up=UP_STEP, down=DOWN_STEP):
    """Можно ли шагнуть при перепаде dz_up = h_куда − h_откуда."""
    return -down <= dz_up <= up

HM = 256                           # текселей heightmap по оси
TEX_UNITS = 128.0                  # юнитов на тексель
REGION_UNITS = 32768


def _find_file(directory, names):
    """Первый существующий файл из names (регистронезависимо)."""
    listing = {f.lower(): f for f in os.listdir(directory)}
    for n in names:
        hit = listing.get(n.lower())
        if hit:
            return os.path.join(directory, hit)
    return None


def map_path(maps_dir, map_name):
    """Путь к карте по её имени в Maps как есть: XX_YY(.unr) или XX_YY_Classic."""
    p = _find_file(maps_dir, [f'{map_name}.unr'])
    if not p:
        raise GeoError(f'нет файла карты {map_name}.unr')
    return p


def _region_id(map_name):
    """XX_YY (координаты в мире) из имени карты: '20_25_Classic' → '20_25'."""
    import re
    m = re.match(r'(\d+_\d+)', map_name)
    return m.group(1) if m else map_name


def load_terrain(maps_dir, tex_dir, map_name):
    """TerrainInfo + G16 → (heights[HM][HM] в мировых Z, holes set[(qx,qy)], loc)."""
    unr = map_path(maps_dir, map_name)
    region = _region_id(map_name)
    pkg = Package(unr)
    tis = pkg.find_exports('TerrainInfo')
    if not tis:
        raise GeoError(f'{region}: в карте нет TerrainInfo (регион без рельефа?)')
    # у некоторых карт несколько TerrainInfo — берём тот, чей heightmap
    # принадлежит этому региону
    props = None
    for cand in tis:
        pr = read_properties(pkg, cand)
        tm = pr.get('TerrainMap')
        if isinstance(tm, int) and pkg.obj_name(tm) == region:
            props = pr
            break
    if props is None:
        props = read_properties(pkg, tis[0])
    loc = props.get('Location', (0.0, 0.0, 0.0))
    scale = props.get('TerrainScale', (128.0, 128.0, 64.0))
    if abs(scale[0] - TEX_UNITS) > 0.01 or abs(scale[1] - TEX_UNITS) > 0.01:
        raise GeoError(f'{region}: TerrainScale {scale[:2]} ≠ 128 — не поддерживается')
    tm_ref = props.get('TerrainMap')
    tm_name = pkg.obj_name(tm_ref) if isinstance(tm_ref, int) else region

    # utx-пакет, где реально лежит heightmap: карта указывает его в импорте
    # TerrainMap (classic-зоны часто переиспользуют heightmap main/другого
    # региона). Он идёт первым, затем — обычные кандидаты по имени региона.
    is_classic = '_classic' in os.path.basename(unr).lower()
    imp_pkg = pkg.import_package(tm_ref)
    utx_names = []
    if imp_pkg:
        utx_names.append(imp_pkg + '.utx')
    utx_names += ([f'T_{region}_Classic.utx', f'T_{region}.utx'] if is_classic
                  else [f'T_{region}.utx'])
    # ищем первый utx, где есть нужная G16-текстура heightmap
    tpkg = tex = None
    for name in utx_names:
        cand = _find_file(tex_dir, [name])
        if not cand:
            continue
        cp = Package(cand)
        hit = [e for e in cp.find_exports('Texture')
               if e.name == tm_name
               and read_properties(cp, e).get('Format') == 10]
        if hit:
            tpkg, tex = cp, hit
            break
    if not tex:
        raise GeoError(f'нет heightmap {tm_name} '
                       f'(искал в {", ".join(utx_names)})')
    e = tex[0]
    tprops = read_properties(tpkg, e)
    usize, vsize = tprops.get('USize', HM), tprops.get('VSize', HM)
    if (usize, vsize) != (HM, HM):
        raise GeoError(f'{region}: heightmap {usize}x{vsize}, ожидалась {HM}x{HM}')
    obj_end = e.offset + e.size
    size = usize * vsize * 2
    data_start = obj_end - 10 - size
    # сигнатура compact-index размера мипа прямо перед данными («40 80 10»)
    r = Reader(tpkg.data, data_start - 3)
    if r.ci() != size:
        raise GeoError(f'{region}: не найдена сигнатура мипа heightmap')
    hm16 = struct.unpack_from(f'<{usize * vsize}H', tpkg.data, data_start)

    lz, sz = loc[2] + Z_CALIBRATION, scale[2]
    heights = [[lz + (hm16[ty * usize + tx] - 32768) * sz / 256.0
                for tx in range(usize)] for ty in range(vsize)]

    holes = set()
    qvb = props.get('QuadVisibilityBitmap')
    words = None
    if isinstance(qvb, (bytes, bytearray)) and len(qvb) > 4:
        # сырой ArrayProperty: CI-счётчик слов + n×u32
        rq = Reader(bytes(qvb))
        n_words = rq.ci()
        if 0 < n_words <= (len(qvb) - rq.p) // 4:
            words = struct.unpack_from(f'<{n_words}I', qvb, rq.p)
    elif isinstance(qvb, dict):
        n_words = max(qvb) + 1 if qvb else 0
        words = [int.from_bytes(w[:4], 'little') if isinstance(w, bytes) else w
                 for w in (qvb.get(i, 0xFFFFFFFF) for i in range(n_words))]
    if words:
        # бит=0 → квад невидим → дыра в рельефе (вход в подземелье)
        for idx in range(min(usize * vsize, len(words) * 32)):
            if not (words[idx // 32] >> (idx % 32)) & 1:
                holes.add((idx % usize, idx // usize))
    return heights, holes, loc


def terrain_cells(region, heights, holes, loc):
    """Высоты ячеек 2048×2048 (int) + маска дыр. Индексация [cy][cx]."""
    rx, ry = map(int, region.split('_'))
    west = (rx - 20) * REGION_UNITS
    north = (ry - 18) * REGION_UNITS
    # мировая позиция вершины (tx,ty): loc.x + (tx - HM/2)*128
    ox = loc[0] - HM / 2 * TEX_UNITS              # мир X вершины tx=0
    oy = loc[1] - HM / 2 * TEX_UNITS
    hcell = [[0] * CELLS for _ in range(CELLS)]
    hole_cell = [[False] * CELLS for _ in range(CELLS)]
    for cy in range(CELLS):
        wy = north + cy * 16 + 8                  # центр ячейки
        fy = (wy - oy) / TEX_UNITS
        ty = int(fy)
        ry_ = fy - ty
        ty0 = min(max(ty, 0), HM - 1)
        ty1 = min(ty0 + 1, HM - 1)
        row0, row1 = heights[ty0], heights[ty1]
        hrow, mrow = hcell[cy], hole_cell[cy]
        for cx in range(CELLS):
            wx = west + cx * 16 + 8
            fx = (wx - ox) / TEX_UNITS
            tx = int(fx)
            rx_ = fx - tx
            tx0 = min(max(tx, 0), HM - 1)
            tx1 = min(tx0 + 1, HM - 1)
            h = (row0[tx0] * (1 - rx_) * (1 - ry_) + row0[tx1] * rx_ * (1 - ry_) +
                 row1[tx0] * (1 - rx_) * ry_ + row1[tx1] * rx_ * ry_)
            hrow[cx] = int(round(h))
            if (tx0, ty0) in holes:
                mrow[cx] = True
    return hcell, hole_cell


def build_l2j(hcell, hole_cell, max_step=UP_STEP):
    """Ячейки → l2j-байты. NSWE: направление закрыто, если перепад к соседу
    больше max_step (порог крутизны склона)."""
    out = bytearray()
    enc = lambda h, n: ((h << 1) & 0xFFF0) | n
    for bx in range(256):
        for by in range(256):
            cells = []
            hs = set()
            all_open = True
            for cx in range(8):
                gx = bx * 8 + cx
                col = None
                for cy in range(8):
                    gy = by * 8 + cy
                    h = hcell[gy][gx]
                    nswe = 15
                    if hole_cell[gy][gx]:
                        nswe = 0
                    else:
                        # N: -y, S: +y, W: -x, E: +x (подъём/спуск асимметричны)
                        if gy > 0 and not _step_ok(hcell[gy - 1][gx] - h, max_step):
                            nswe &= ~8
                        if gy < CELLS - 1 and not _step_ok(hcell[gy + 1][gx] - h, max_step):
                            nswe &= ~4
                        if gx > 0 and not _step_ok(hcell[gy][gx - 1] - h, max_step):
                            nswe &= ~2
                        if gx < CELLS - 1 and not _step_ok(hcell[gy][gx + 1] - h, max_step):
                            nswe &= ~1
                    if nswe != 15:
                        all_open = False
                    cells.append((h, nswe))
                    hs.add(h & 0xFFF8)
            if all_open and len(hs) == 1:
                out.append(0)
                out += struct.pack('<h', max(-16384, min(16376, cells[0][0])))
            else:
                out.append(1)
                for h, n in cells:
                    out += struct.pack('<H', enc(h, n))
    return bytes(out)


def generate_region(client_dir, map_name, max_step=UP_STEP):
    """Клиент → l2j-байты карты по её имени в Maps (этап 1: только рельеф)."""
    maps_dir = os.path.join(client_dir, 'Maps')
    tex_dir = os.path.join(client_dir, 'Textures')
    if not os.path.isdir(maps_dir):
        maps_dir = os.path.join(client_dir, 'MAPS')
    heights, holes, loc = load_terrain(maps_dir, tex_dir, map_name)
    hcell, hole_cell = terrain_cells(_region_id(map_name), heights, holes, loc)
    return build_l2j(hcell, hole_cell, max_step)


# ─────────────────────────── этап 2: статик-меши ───────────────────────────

# ─────────────────────── рейкаст-ядро (воксельный XYZ) ───────────────────────
# Вместо растеризации треугольников — лучи в ячейках: Z-луч вниз даёт полы/потолки
# (стопку поверхностей), X/Y-луч на уровне корпуса даёт стены. Полы/потолки — по
# углу нормали грани, стены — по факту пересечения
# горизонтального луча (свод над головой не мешает — луч идёт на уровне груди).
WALK_NZ = 0.766        # nz/|n| ≥ cos(40°) И nz>0 → пол (грань вверх, ≤40°); нормаль
                       # вниз → потолок/дно (порог наклона пола 40°)
LAYER_MERGE = 24       # полы ближе 24ю — один слой (артефакт округления высот)
AGENT_HEIGHT = 128     # клиренс: над полом нужно ≥ этого пустоты до ближайшей
                       # поверхности сверху, иначе там не встать (отсекает «дно» мешей)
BODY_LO = 8            # X/Y-луч стены проверяется по вертикали корпуса над полом:
BODY_HI = 48           # грань, пересекающая (пол+BODY_LO … пол+BODY_HI) на пути к
                       # соседу — стена. Ниже 8ю (бортик) переступаешь, выше 48ю
                       # (свод/арка над головой) — проходишь под ней. Один луч заменяет
                       # прежние пороги EDGE_MIN/WALL_MIN_H/CROSS и снимает «тени».
BIG_PLANE = 4096       # горизонтальная грань с XY-габаритом больше этого — зонная/
                       # водная плоскость уровня (в BSP они размером с регион), не пол


def _prep_geometry(tris, west, north):
    """Один проход по треугольникам: классификация + предпосчёт. Возвращает
    (fc, fc_grid, walls, w_grid):
      fc[i]    — грань-пол/потолок с предпосчитанным барицентриком для Z-луча;
      fc_grid  — индекс (cell → индексы fc), покрытие по XY-bbox;
      walls[i] — крутая грань с предпосчитанными рёбрами e1,e2 для segment-теста;
      w_grid   — индекс (cell → индексы walls).
    Нормали, наклон, барицентрик и рёбра считаются здесь ОДИН раз, а не в каждом
    луче — результат лучей идентичен, меняется только скорость."""
    fc = []
    walls = []
    fc_grid = {}
    w_grid = {}
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in tris:
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        ln2 = nx * nx + ny * ny + nz * nz
        if ln2 <= 0:
            continue
        gx0 = max(0, int((min(ax, bx, cx) - west) // 16))
        gx1 = min(CELLS - 1, int((max(ax, bx, cx) - west) // 16))
        gy0 = max(0, int((min(ay, by, cy) - north) // 16))
        gy1 = min(CELLS - 1, int((max(ay, by, cy) - north) // 16))
        if gx1 < gx0 or gy1 < gy0:
            continue
        if abs(nz) < WALK_NZ * math.sqrt(ln2):        # крутая → стена
            wi = len(walls)
            walls.append((ax, ay, az, ux, uy, uz, vx, vy, vz))  # e1=(u), e2=(v)
            grid = w_grid
            idx = wi
        else:                                          # пологая → пол/потолок
            if (max(ax, bx, cx) - min(ax, bx, cx) > BIG_PLANE or
                    max(ay, by, cy) - min(ay, by, cy) > BIG_PLANE):
                continue                               # зонная/водная плоскость
            d00 = vx * vx + vy * vy
            d01 = vx * ux + vy * uy
            d11 = ux * ux + uy * uy
            den = d00 * d11 - d01 * d01
            if abs(den) < 1e-9:
                continue
            fi = len(fc)
            fc.append((nz > 0, ax, ay, az, ux, uy, uz, vx, vy, vz,
                       d00, d01, d11, 1.0 / den))
            grid = fc_grid
            idx = fi
        for gy in range(gy0, gy1 + 1):
            row = gy * CELLS
            for gx in range(gx0, gx1 + 1):
                grid.setdefault(row + gx, []).append(idx)
    return fc, fc_grid, walls, w_grid


def _column(px, py, fc_idx, fc):
    """Z-луч вниз в (px, py): пересечения вертикали с предпосчитанными гранями-
    полами/потолками ячейки → (floors, ceils). Барицентрик уже готов в fc[i]."""
    floors = []
    ceils = []
    for fi in fc_idx:
        (is_floor, ax, ay, az, ux, uy, uz, vx, vy, vz,
         d00, d01, d11, inv) = fc[fi]
        pxr = px - ax
        pyr = py - ay
        d02 = vx * pxr + vy * pyr
        d12 = ux * pxr + uy * pyr
        u = (d11 * d02 - d01 * d12) * inv
        v = (d00 * d12 - d01 * d02) * inv
        if u < -0.02 or v < -0.02 or u + v > 1.02:
            continue
        z = az + u * vz + v * uz
        if not (-16384 <= z <= 16376):
            continue
        (floors if is_floor else ceils).append(int(z))
    return floors, ceils


def _wall_between(px, py, npx, npy, z, w_idx, walls):
    """X/Y-луч: проход закрыт, только если стена — СПЛОШНАЯ преграда по корпусу:
    крутая грань(и) пересекают горизонтальный отрезок [центр→сосед] И на нижней
    высоте (пол+BODY_LO, щиколотка), И на верхней (пол+BODY_HI, грудь). Так
    ступенька (низ есть, верха нет — переступаешь) и нависающий выступ/карниз
    (верх есть, низа нет — проходишь под) стеной НЕ считаются. Рёбра e1,e2
    предпосчитаны; Möller–Trumbore для сегмента."""
    dx = npx - px
    dy = npy - py
    hit_lo = False
    hit_hi = False
    for wi in w_idx:
        ax, ay, az, e1x, e1y, e1z, e2x, e2y, e2z = walls[wi]
        # h = d × e2, d = (dx, dy, 0)  (отрезок горизонтальный → dz = 0)
        hx = dy * e2z
        hy = -dx * e2z
        hz = dx * e2y - dy * e2x
        a = e1x * hx + e1y * hy + e1z * hz
        if -1e-9 < a < 1e-9:
            continue
        f = 1.0 / a
        sx, sy = px - ax, py - ay
        for lo in (True, False):                     # низ (щиколотка) и верх (грудь)
            if (hit_lo and lo) or (hit_hi and not lo):
                continue
            zc = z + (BODY_LO if lo else BODY_HI)
            sz = zc - az
            uu = f * (sx * hx + sy * hy + sz * hz)
            if uu < 0.0 or uu > 1.0:
                continue
            qx = sy * e1z - sz * e1y
            qy = sz * e1x - sx * e1z
            qz = sx * e1y - sy * e1x
            vv = f * (dx * qx + dy * qy)
            if vv < 0.0 or uu + vv > 1.0:
                continue
            t = f * (e2x * qx + e2y * qy + e2z * qz)
            if 0.0 <= t <= 1.0:
                if lo:
                    hit_lo = True
                else:
                    hit_hi = True
        if hit_lo and hit_hi:
            return True
    return hit_lo and hit_hi


def _span_layers(floor_zs, ceil_zs, base):
    """Спаны ячейки → проходимые слои (сверху вниз), с проверкой клиренса.

    Кандидаты пола — floor-спаны мешей плюс рельеф (base). Клиренс проверяется
    ТОЛЬКО для мешевых полов: слой-меш проходим, если над ним до ближайшей
    поверхности (потолок-спан или следующий пол выше) есть зазор ≥ AGENT_HEIGHT —
    иначе под нависающей геометрией не встать (так отсекается «дно» объёмного
    меша: оно — потолок-спан, полом не становится, а меш-полка под ним гасится).
    Рельеф (base) НЕ гасится клиренсом и остаётся всегда: земля под навесом/аркой
    проходима, и реальная геодата почти не имеет глухих ячеек. Близкие полы
    (<LAYER_MERGE) сливаются — артефакт округления высот."""
    terrain = base[0] if base else None
    cand = sorted(set(list(floor_zs) + list(base)), reverse=True)
    ceils = sorted(set(ceil_zs))
    out = []
    for z in cand:
        if out and out[-1] - z < LAYER_MERGE:     # слишком близко к принятому — слив
            continue
        if z != terrain:                          # рельеф проходим всегда, без клиренса
            # ближайшая поверхность СТРОГО выше z: потолок или другой пол-кандидат
            above = None
            for c in ceils:                        # ceils по возрастанию
                if c > z + LAYER_MERGE:
                    above = c
                    break
            for f in reversed(cand):               # cand по убыванию → reversed возр.
                if f > z + LAYER_MERGE:
                    if above is None or f < above:
                        above = f
                    break
            if above is not None and (above - z) < AGENT_HEIGHT:
                continue                           # под нависшей поверхностью не встать
        out.append(z)
    return out[:120]




MIN_ISLAND = 1200      # недостижимые с рельефа компоненты (крыши, навесы,
                       # кроны) удаляются; сохраняются только огромные летающие
                       # локации масштаба Superion (>1200 ячеек)


def _prune_unreachable(cell_layers, hcell, hole_cell, max_step):
    """Удаляет мешевые слои, недостижимые с рельефа, если их связная
    компонента меньше MIN_ISLAND ячеек (кроны деревьев, верхушки валунов).
    Большие изолированные зоны (летающие острова) сохраняются."""
    # узел = (cell, layer_index); достижимость от терраиновых слоёв
    visited = set()
    from collections import deque
    q = deque()
    for cell, ls in cell_layers.items():
        gy, gx = divmod(cell, CELLS)
        own_t = None if hole_cell[gy][gx] else hcell[gy][gx]
        for li, z in enumerate(ls):
            root = own_t is not None and _step_ok(z - own_t, max_step)
            if not root:
                # шаг с чистого соседнего рельефа (ячейки без полов)
                for ngx, ngy in ((gx, gy - 1), (gx, gy + 1), (gx - 1, gy), (gx + 1, gy)):
                    if not (0 <= ngx < CELLS and 0 <= ngy < CELLS):
                        continue
                    ncell = ngy * CELLS + ngx
                    if ncell in cell_layers or hole_cell[ngy][ngx]:
                        continue
                    if _step_ok(z - hcell[ngy][ngx], max_step):
                        root = True
                        break
            if root:
                visited.add((cell, li))
                q.append((cell, li, z))
    while q:
        cell, li, z = q.popleft()
        gy, gx = divmod(cell, CELLS)
        for ngx, ngy in ((gx, gy - 1), (gx, gy + 1), (gx - 1, gy), (gx + 1, gy)):
            if not (0 <= ngx < CELLS and 0 <= ngy < CELLS):
                continue
            ncell = ngy * CELLS + ngx
            nls = cell_layers.get(ncell)
            if nls is None:
                continue                            # чистый терраин — уже корень
            for nli, nz in enumerate(nls):
                if (ncell, nli) not in visited and _step_ok(nz - z, max_step):
                    visited.add((ncell, nli))
                    q.append((ncell, nli, nz))
    # недостижимые узлы → компоненты; маленькие удаляем
    unreachable = {}
    for cell, ls in cell_layers.items():
        for li, z in enumerate(ls):
            if (cell, li) not in visited:
                unreachable[(cell, li)] = z
    seen = set()
    drop = set()
    for node in unreachable:
        if node in seen:
            continue
        comp = [node]
        seen.add(node)
        qq = deque([node])
        while qq:
            cell, li = qq.popleft()
            z = unreachable[(cell, li)]
            gy, gx = divmod(cell, CELLS)
            for ngx, ngy in ((gx, gy - 1), (gx, gy + 1), (gx - 1, gy), (gx + 1, gy)):
                if not (0 <= ngx < CELLS and 0 <= ngy < CELLS):
                    continue
                ncell = ngy * CELLS + ngx
                nls = cell_layers.get(ncell)
                if nls is None:
                    continue
                for nli, nz in enumerate(nls):
                    nn = (ncell, nli)
                    if nn in unreachable and nn not in seen and abs(nz - z) <= DOWN_STEP:
                        seen.add(nn)
                        comp.append(nn)
                        qq.append(nn)
        if len(comp) < MIN_ISLAND:
            drop.update(comp)
    if drop:
        for cell in {c for c, _ in drop}:
            ls = cell_layers[cell]
            keep = [z for li, z in enumerate(ls) if (cell, li) not in drop]
            if keep:
                cell_layers[cell] = keep
            else:
                del cell_layers[cell]
    return len(drop)


def _blocking_edges(blocking, west, north):
    """BlockingVolume-грани → множество закрытых границ ячеек (заградительные
    стены уровня: край играбельной зоны, перекрытые дороги). Возвращает set
    ключей: ('x', gx, gy) — граница между (gx-1,gy) и (gx,gy); ('y', gx, gy) —
    между (gx,gy-1) и (gx,gy). Крупные грани (>BIG_PLANE) — заградительный объём
    ВОКРУГ зоны/локации, а не внутренняя стена: их растеризация заперла бы всю
    зону изнутри (лавина глухих), поэтому пропускаем."""
    blocked = set()
    for tri in blocking:
        xs = (tri[0][0], tri[1][0], tri[2][0])
        ys = (tri[0][1], tri[1][1], tri[2][1])
        if max(xs) - min(xs) > BIG_PLANE or max(ys) - min(ys) > BIG_PLANE:
            continue                               # граница локации, не стена
        for (x0, y0, _z0), (x1, y1, _z1) in ((tri[0], tri[1]), (tri[1], tri[2]),
                                             (tri[2], tri[0])):
            dx, dy = x1 - x0, y1 - y0
            steps = max(1, int(max(abs(dx), abs(dy)) / 8))
            prev = None
            for s in range(steps + 1):
                t = s / steps
                gx = int((x0 + dx * t - west) // 16)
                gy = int((y0 + dy * t - north) // 16)
                if prev is not None and (gx, gy) != prev:
                    pgx, pgy = prev
                    if gx != pgx and gy != pgy:        # диагональ — метим обе границы
                        blocked.add(('x', max(gx, pgx), gy))
                        blocked.add(('y', gx, max(gy, pgy)))
                    elif gx != pgx and gy == pgy:
                        blocked.add(('x', max(gx, pgx), gy))
                    elif gy != pgy and gx == pgx:
                        blocked.add(('y', gx, max(gy, pgy)))
                prev = (gx, gy)
    return blocked


def _raycast_backend(fc, fc_grid, walls, w_grid, hcell, hole_cell, west, north,
                     max_step, blocked):
    """Полный конвейер лучей+сборки NSWE на GPU/CPU через Taichi, если он
    установлен и работает: Z-луч (слои) → отсечение недостижимого → X/Y-луч
    (стены) → NSWE всех слоёв грида. Возвращает (cell_layers, nswe_flat, l_off) —
    готовые для упаковки в build_l2j_ray, либо (None, None, None) → build посчитает
    на чистом Python. Результат байт-в-байт совпадает с Python (f64); любой сбой
    Taichi — тихий откат."""
    if not fc_grid:                                    # регион без геометрии —
        return None, None, None                        # рельеф сам, Taichi не нужен
    try:
        from . import raycast_ti as RT
        if not RT.available() or RT.init() is None:    # init внутри try: сбой
            return None, None, None                    # Taichi/LLVM → тихий откат
    except Exception:
        return None, None, None
    try:
        cells = list(fc_grid)
        oz, ofl, ocnt = RT.zray_columns(fc, fc_grid, west, north, cells, WALK_NZ)
        cell_layers = {}
        for i, cell in enumerate(cells):
            gy, gx = divmod(cell, CELLS)
            fl = [int(oz[i, k]) for k in range(ocnt[i]) if ofl[i, k] == 1]
            ce = [int(oz[i, k]) for k in range(ocnt[i]) if ofl[i, k] == 0]
            ls = _span_layers(fl, ce, [hcell[gy][gx]])
            if ls:
                cell_layers[cell] = ls
        _prune_unreachable(cell_layers, hcell, hole_cell, max_step)   # ДО NSWE
        # NSWE всех слоёв грида на устройстве (Z-луч слои готовы; стены X/Y-лучом
        # и height/blocked/anti-глухая — всё в одном ядре)
        nswe_flat, l_off = RT.nswe_grid(cell_layers, hcell, walls, w_grid, blocked,
                                        west, north, max_step, DOWN_STEP,
                                        BODY_LO, BODY_HI)
        return cell_layers, nswe_flat, l_off
    except Exception:
        return None, None, None                        # сбой → чистый Python


def _pack_layers(hcell, cell_layers, nswe_flat, l_off, progress_cb=None):
    """Упаковка готовых слоёв+NSWE (посчитанных на устройстве) в байты L2J.
    nswe_flat[l_off[cell]+j] — nswe j-го слоя ячейки; порядок слоёв совпадает с ls
    (рельеф — слой 0, либо cell_layers). Сериализация идентична build_l2j_ray."""
    def enc(h, n):
        h = max(-16384, min(16376, h))
        return ((h << 1) & 0xFFF0) | n
    out = bytearray()
    for bx in range(256):
        if progress_cb:
            progress_cb(bx, 256)
        for by in range(256):
            cells = []
            multi = False
            flat_ok = True
            hs = set()
            for cx in range(8):
                gx = bx * 8 + cx
                for cy in range(8):
                    gy = by * 8 + cy
                    cell = gy * CELLS + gx
                    ls = cell_layers.get(cell)
                    if ls is None:
                        ls = (hcell[gy][gx],)
                    o = int(l_off[cell])
                    lay = [(z, int(nswe_flat[o + j])) for j, z in enumerate(ls)]
                    cells.append(lay)
                    if len(lay) > 1:
                        multi = True
                    if lay[0][1] != 15:
                        flat_ok = False
                    hs.add(lay[0][0] & 0xFFF8)
            if multi:
                out.append(2)
                for lay in cells:
                    out.append(len(lay))
                    for z, n in lay:
                        out += struct.pack('<H', enc(z, n))
            elif flat_ok and len(hs) == 1:
                out.append(0)
                out += struct.pack('<h', max(-16384, min(16376, cells[0][0][0])))
            else:
                out.append(1)
                for lay in cells:
                    out += struct.pack('<H', enc(*lay[0]))
    if progress_cb:
        progress_cb(256, 256)
    return bytes(out)


def build_l2j_ray(hcell, hole_cell, fc, fc_grid, walls, w_grid, west, north,
                  max_step=UP_STEP, blocked=(), progress_cb=None,
                  cell_layers=None, nswe_flat=None, l_off=None):
    """Рельеф + рейкаст мешей → l2j с multilayer. Слои — Z-лучом в центре ячейки
    (полы/потолки/клиренс); NSWE — перепадом высот соседей И X/Y-лучом на уровне
    корпуса (стены). Два режима: (1) готовый NSWE из Taichi-бэкенда (nswe_flat/
    l_off) — только упаковка байт; (2) чистый Python — Z-луч + отсечение + NSWE +
    упаковка на лету, байт-в-байт идентично. Рейкаст только в ячейках с геометрией;
    открытое поле и дыры — рельеф напрямую."""
    blocked = blocked or set()
    # ── режим 1: NSWE посчитан на устройстве, осталась лишь упаковка ──
    if nswe_flat is not None:
        return _pack_layers(hcell, cell_layers, nswe_flat, l_off, progress_cb)

    # ── режим 2: всё на чистом Python ──
    if cell_layers is None:                             # чистый Python Z-луч
        cell_layers = {}
        for cell, fc_idx in fc_grid.items():
            gy, gx = divmod(cell, CELLS)
            px = west + gx * 16 + 8
            py = north + gy * 16 + 8
            fl, ce = _column(px, py, fc_idx, fc)
            ls = _span_layers(fl, ce, [hcell[gy][gx]])
            if ls:
                cell_layers[cell] = ls
    _prune_unreachable(cell_layers, hcell, hole_cell, max_step)

    def layers_at(gx, gy):
        ls = cell_layers.get(gy * CELLS + gx)
        return ls if ls is not None else (hcell[gy][gx],)

    def enc(h, n):
        h = max(-16384, min(16376, h))
        return ((h << 1) & 0xFFF0) | n
    out = bytearray()
    for bx in range(256):
        if progress_cb:                              # прогресс на каждый блок (256 строк) —
            progress_cb(bx, 256)                     # частые тики, чтобы не казалось «висит»
        for by in range(256):
            cells = []
            multi = False
            flat_ok = True
            hs = set()
            for cx in range(8):
                gx = bx * 8 + cx
                for cy in range(8):
                    gy = by * 8 + cy
                    cell = gy * CELLS + gx
                    ls = layers_at(gx, gy) or (hcell[gy][gx],)
                    px = west + gx * 16 + 8
                    py = north + gy * 16 + 8
                    wti = w_grid.get(cell)              # стены-грани ячейки
                    lay = []
                    for z in ls:
                        nswe = 15
                        wall_bits = 0                   # закрытые стеной/декором
                        block_bits = 0                  # закрытые BlockingVolume
                        for di, (bit, ngx, ngy, bkey) in enumerate((
                                (8, gx, gy - 1, ('y', gx, gy)),
                                (4, gx, gy + 1, ('y', gx, gy + 1)),
                                (2, gx - 1, gy, ('x', gx, gy)),
                                (1, gx + 1, gy, ('x', gx + 1, gy)))):
                            if not (0 <= ngx < CELLS and 0 <= ngy < CELLS):
                                continue
                            nls = layers_at(ngx, ngy)
                            height_ok = any(_step_ok(nz - z, max_step) for nz in nls)
                            if bkey in blocked:          # BlockingVolume
                                nswe &= ~bit
                                if height_ok:
                                    block_bits |= bit
                                continue
                            if not height_ok:
                                nswe &= ~bit             # перепад высот (склон/обрыв)
                                continue
                            # X/Y-луч: стена между полами на уровне корпуса
                            nwti = w_grid.get(ngy * CELLS + ngx)
                            is_wall = False
                            if wti or nwti:
                                seg_w = (wti or []) + (nwti or [])
                                npx = west + ngx * 16 + 8
                                npy = north + ngy * 16 + 8
                                is_wall = _wall_between(px, py, npx, npy, z, seg_w, walls)
                            if is_wall:
                                nswe &= ~bit
                                wall_bits |= bit
                        # anti-глухая: тонкий объект (ствол/столб) не запирает
                        # проходимую клетку со всех сторон — возвращаем сторону
                        # обхода. НО сквозную преграду BlockingVolume (обе стороны
                        # оси N-S=12 или W-E=3 закрыты объёмом) держим закрытой,
                        # иначе заграждение «протекает» в узком проходе 1 ячейки.
                        if nswe == 0 and (wall_bits or block_bits):
                            give = wall_bits | block_bits
                            if (block_bits & 12) == 12:   # сквозная ось N-S
                                give &= ~12
                            if (block_bits & 3) == 3:     # сквозная ось W-E
                                give &= ~3
                            nswe = give
                        lay.append((z, nswe))
                    cells.append(lay)
                    if len(lay) > 1:
                        multi = True
                    if lay[0][1] != 15:
                        flat_ok = False
                    hs.add(lay[0][0] & 0xFFF8)
            if multi:
                out.append(2)
                for lay in cells:
                    out.append(len(lay))
                    for z, n in lay:
                        out += struct.pack('<H', enc(z, n))
            elif flat_ok and len(hs) == 1:
                out.append(0)
                out += struct.pack('<h', max(-16384, min(16376, cells[0][0][0])))
            else:
                out.append(1)
                for lay in cells:
                    out += struct.pack('<H', enc(*lay[0]))
    if progress_cb:
        progress_cb(256, 256)
    return bytes(out)


def generate_region_full(client_dir, map_name, max_step=UP_STEP, progress_cb=None):
    """Клиент → l2j по имени карты в Maps: рельеф + статик-меши и BSP-модель
    (полы/потолки в спаны) + BlockingVolume (заградительные рёбра NSWE).
    Heightfield-ядро, NSWE по высоте."""
    from .meshes import region_mesh_triangles
    maps_dir = os.path.join(client_dir, 'Maps')
    if not os.path.isdir(maps_dir):
        maps_dir = os.path.join(client_dir, 'MAPS')
    tex_dir = os.path.join(client_dir, 'Textures')
    usx_dir = os.path.join(client_dir, 'StaticMeshes')
    region = _region_id(map_name)
    heights, holes, loc = load_terrain(maps_dir, tex_dir, map_name)
    hcell, hole_cell = terrain_cells(region, heights, holes, loc)
    rx, ry = map(int, region.split('_'))
    west, north = (rx - 20) * REGION_UNITS, (ry - 18) * REGION_UNITS
    from .unreal import Package
    unr = map_path(maps_dir, map_name)
    tris, skipped = region_mesh_triangles(unr, usx_dir)   # парсинг мешей (быстрый)
    from .meshes import level_extra_triangles
    solid, blocking = level_extra_triangles(Package(unr))
    # конвенция высот едина для всего: меши и BSP сдвигаются так же, как рельеф
    zc = Z_CALIBRATION
    allt = [tuple((x, y, z + zc) for x, y, z in t) for t in (tris + solid)]
    # Рейкаст-ядро (воксельный XYZ): пространственный индекс
    # мешей + BSP-модели, Z-луч даёт полы/потолки, X/Y-луч на уровне корпуса — стены.
    # Рельеф (heightmap) — базовый пол везде, лучи только в ячейках с геометрией.
    fc, fc_grid, walls, w_grid = _prep_geometry(allt, west, north)
    blocked = _blocking_edges(blocking, west, north)
    # GPU/CPU-бэкенд Taichi для лучей (если установлен), иначе чистый Python.
    # Результат байт-в-байт идентичен (f64): устройство без f64 (Metal) Taichi
    # сам отбрасывает и считает на CPU. Слои/стены готовим здесь, сборку L2J —
    # в build_l2j_ray.
    cell_layers, nswe_flat, l_off = _raycast_backend(
        fc, fc_grid, walls, w_grid, hcell, hole_cell, west, north, max_step, blocked)
    data = build_l2j_ray(hcell, hole_cell, fc, fc_grid, walls, w_grid, west, north,
                         max_step, blocked, progress_cb, cell_layers, nswe_flat, l_off)
    return data, len(allt), skipped


# ─────────────────────────── команда generate ───────────────────────────

_PROGRESS_Q = None    # очередь тиков прогресса воркера → главный процесс


def _drain_progress(pq, active):
    """Вычерпать все тики из очереди в active: map_name → (done,total);
    'done' удаляет карту из активных."""
    import queue as _q
    while True:
        try:
            name, d = pq.get_nowait()[:2]
        except (_q.Empty, Exception):
            return
        if d == 'done':
            active.pop(name, None)
        else:
            active[name] = (d, 256)


def _init_worker(q):
    """Инициализатор процесса пула: запоминает очередь прогресса."""
    global _PROGRESS_Q
    _PROGRESS_Q = q


def _worker(job):
    """Одна карта (для пула процессов).

    Атомарная запись: сперва во временный файл, валидация, затем
    os.replace → каноничный .l2j появляется только целым и проверенным.
    Прерывание (Ctrl+C/terminate) в момент записи оставит максимум .tmp,
    но никогда — обрезанный или невалидный .l2j.
    Прогресс рейкаста шлётся в _PROGRESS_Q тиками (map_name, done, total),
    завершение — (map_name, 'done')."""
    client_dir, map_name, out_path, max_step, terrain_only = job
    tmp = out_path + '.tmp'
    q = _PROGRESS_Q
    cb = (lambda d, t: q.put((map_name, d, t))) if q is not None else None
    try:
        if terrain_only:
            data = generate_region(client_dir, map_name, max_step)
        else:
            data, _n, _s = generate_region_full(client_dir, map_name, max_step, cb)
        with open(tmp, 'wb') as f:
            f.write(data)
        from .convert import validate_l2j
        validate_l2j(tmp)
        os.replace(tmp, out_path)                 # атомарная подмена
        if q is not None:
            q.put((map_name, 'done'))
        return (map_name, None)
    except BaseException as e:                     # incl. KeyboardInterrupt/SystemExit
        if os.path.exists(tmp):
            os.remove(tmp)
        if q is not None:
            q.put((map_name, 'done'))
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return (map_name, f'{type(e).__name__}: {e}')



WORKER_RAM_GB = 2.5    # оценка пиковой памяти воркера на городском регионе


def _total_ram_gb():
    """Всего ОЗУ (ГБ), кроссплатформенно, без зависимостей. None если не узнать."""
    try:
        return os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') / 1e9
    except (ValueError, AttributeError, OSError):
        pass
    try:
        import ctypes                                       # Windows
        class MS(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return ms.ullTotalPhys / 1e9
    except Exception:
        return None


def _auto_jobs():
    """Авто-параллелизм ≈ половина ресурсов: половина ядер, но не больше,
    чем влезает в половину ОЗУ (по WORKER_RAM_GB на воркер)."""
    cores = os.cpu_count() or 4
    half_cores = max(1, cores // 2)
    total = _total_ram_gb()
    if total:
        mem_jobs = max(1, int(total * 0.5 / WORKER_RAM_GB))
        return min(half_cores, mem_jobs)
    return half_cores


def cmd_generate(client_dir, out_dir, maps=None, max_step=UP_STEP,
                 terrain_only=False, jobs=None):
    """Генерация из клиента: каждая карта Maps как есть → out/<имя>.l2j.

    Имя квадрата = имя файла карты: XX_YY (из XX_YY.unr) и XX_YY_Classic
    (из XX_YY_Classic.unr) — отдельные квадраты. Генерится ровно выбранное;
    maps — список имён (None → все карты вида XX_YY*.unr)."""
    import multiprocessing as mp
    import re as _re
    import time as _t
    from .ui import bold, dim, green, progress, red, yellow
    maps_dir = os.path.join(client_dir, 'Maps')
    if not os.path.isdir(maps_dir):
        maps_dir = os.path.join(client_dir, 'MAPS')
    if not os.path.isdir(maps_dir):
        print(red(f'  ✗ не найдена папка Maps в {client_dir}'))
        return 1
    listing = os.listdir(maps_dir)
    all_maps = sorted(m.group(1) for f in listing
                      for m in [_re.match(r'^(\d+_\d+.*?)\.unr$', f, _re.IGNORECASE)] if m)
    names = [n for n in all_maps if n in maps] if maps else all_maps
    tasks = [(client_dir, n, os.path.join(out_dir, n + '.l2j'), max_step, terrain_only)
             for n in names]
    if not tasks:
        print(red('  ✗ нет подходящих карт (XX_YY*.unr) в Maps'))
        return 1
    os.makedirs(out_dir, exist_ok=True)
    label_w = max((len(n) for n in names), default=8) + 2   # выравнивание баров
    nproc = jobs or _auto_jobs()
    ram = _total_ram_gb()
    how = 'задано -j' if jobs else (f'авто: половина ресурсов'
          + (f' от {ram:.0f} ГБ ОЗУ / {os.cpu_count() or "?"} потоков' if ram else ''))
    print(f'  {bold("Генерация")}: {len(names)} карт'
          f' → {out_dir} · {nproc} процессов ({how})'
          f'{" · только рельеф" if terrain_only else ""}\n')
    print(dim('  (Ctrl+C — прервать; готовые файлы сохранятся)\n'))
    t0 = _t.time()
    ok, errors, done = 0, [], 0
    total = len(tasks)
    aborted = False
    if total <= 3:
        # мало квадратов → последовательно, с прогрессом-ETA ВНУТРИ карты (по
        # блокам): видно, сколько осталось до конца квадрата, а не только 0→100%.
        from .convert import validate_l2j
        try:
            for i, (cl, name, out_path, ms, terr) in enumerate(tasks, 1):
                def cb(d, t, _n=name):
                    # метка фикс. ширины → бары строго друг под другом
                    progress(d, t, f'  {_n}'.ljust(label_w) + ' ')
                tmp = out_path + '.tmp'
                try:
                    if terr:
                        data = generate_region(cl, name, ms)
                        cb(256, 256)
                    else:
                        data, _n, _s = generate_region_full(cl, name, ms, cb)
                    with open(tmp, 'wb') as f:
                        f.write(data)
                    validate_l2j(tmp)
                    os.replace(tmp, out_path)
                    ok += 1
                except KeyboardInterrupt:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    raise
                except BaseException as e:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    errors.append((name, f'{type(e).__name__}: {e}'))
                done = i
        except KeyboardInterrupt:
            aborted = True
            print(dim(f'\n  прервано на {done}/{total} — готовые {ok} сохранены.'))
    else:
        # воркеры игнорируют SIGINT — прерывание ловит только главный процесс,
        # чтобы Ctrl+C не сыпал трейсбеками из пула. Прогресс воркеров идёт через
        # очередь: главный процесс рисует общий счётчик файлов + активные регионы
        # с их процентами (видно движение внутри каждого, а не только N/total).
        import signal
        import time as _tt
        import queue as _queue
        from .ui import fmt_dur, cyan as _cyan
        mgr = mp.Manager()
        pq = mgr.Queue()
        orig = signal.signal(signal.SIGINT, signal.SIG_IGN)
        pool = mp.Pool(nproc, _init_worker, (pq,))
        signal.signal(signal.SIGINT, orig)
        asyncs = [pool.apply_async(_worker, (job,)) for job in tasks]
        active = {}                                  # map_name → (done, total)
        t0b = _tt.monotonic()
        try:
            while True:
                _drain_progress(pq, active)          # вычерпать тики прогресса
                completed = sum(1 for a in asyncs if a.ready())
                # отрисовка: общий бар + активные карты
                w = 24
                frac = completed / total if total else 1.0
                bar = '█' * int(w * frac) + '░' * (w - int(w * frac))
                el = _tt.monotonic() - t0b
                eta = ('~' + fmt_dur(el / completed * (total - completed))
                       if completed and el > 1 else '')
                act = ' · '.join(
                    f'{n} {min(99, d * 100 // t)}%'
                    for n, (d, t) in sorted(active.items())[:5] if t)
                line = (f'\r  генерация {_cyan(bar)} {frac:5.1%} ({completed}/{total})'
                        f' {eta}' + (f'  │ {act}' if act else ''))
                sys.stdout.write(line[:220] + ' ' * 6)
                sys.stdout.flush()
                if completed >= total:
                    break
                _tt.sleep(0.25)
            sys.stdout.write('\n')
            for a in asyncs:
                name, err = a.get()
                done += 1
                if err is None:
                    ok += 1
                else:
                    errors.append((name, err))
        except KeyboardInterrupt:
            aborted = True
            pool.terminate()
            print(dim(f'\n  прервано на {done}/{total} — готовые {ok} файлов сохранены.'))
        else:
            pool.close()
        finally:
            pool.join()
            # подчистить .tmp-огрызки прерванных записей (SIGTERM мог убить до уборки)
            import glob as _g2
            for t in _g2.glob(os.path.join(out_dir, '*.l2j.tmp')):
                try:
                    os.remove(t)
                except OSError:
                    pass
    if aborted:
        return 130
    print(f'\n  {bold("Итог")} за {(_t.time() - t0) / 60:.1f} мин:'
          f' {green(f"✓ {ok}")}' + (f' · {red(f"✗ {len(errors)}")}' if errors else ''))
    for name, why in errors[:10]:
        print(f'    {red("✗")} {name}: {why}')
    if errors:
        print(dim('    (пропущены карты без heightmap в клиенте — там нечего'
                  ' генерировать)'))
    if not terrain_only:
        print(f'\n  {bold("Что дальше:")}')
        print(f'    • {out_dir}/ — по файлу <имя>.l2j на карту. Имя = имя карты'
              ' в Maps (XX_YY и XX_YY_Classic — отдельные файлы).')
        print(dim('      Для classic-сервера квадрат из XX_YY_Classic.l2j кладётся'
                  ' под именем XX_YY.l2j (заменяет базовый).'))
        print(f'\n  {yellow("Проверь перед установкой:")}')
        print(f'    geotool.py view {out_dir}   — глянуть карту/города/слои в браузере')
        print('    geotool.py diff <твоя_геодата> ' + out_dir
              + '   — сравнить с текущей (высоты и проходимость)')
        print(dim('    …и обязательно пройди ключевые зоны в игре — генерация'
                  ' не проверяет их сама.'))
    return 0 if not errors else 2
