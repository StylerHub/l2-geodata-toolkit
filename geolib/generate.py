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

from .formats import BLOCKS, GeoError
from .unreal import Package, Reader, read_properties

CELLS = 2048                       # ячеек на регион по оси
# Калибровка конвенции высот: чистая формула UE2 даёт поверхность на ~43 юнита
# ниже, чем в эталонных паках (и чем ожидает серверная экосистема).
# Эмпирика: океан клиента (h16=0x4040, Scale.Z=76, Loc.Z=160.65) → UE2 −4684.35,
# в эталоне −4641. Подтверждено ретейл-спавнами (7.6k точек, 15 регионов).
Z_CALIBRATION = 43.35
# Асимметричные пороги шага (калибровка по-ячеечным сравнением с эталонным
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


def map_path(maps_dir, region, variant='main'):
    """Путь к карте региона: main → XX_YY.unr, classic → XX_YY_Classic.unr."""
    name = f'{region}_Classic.unr' if variant == 'classic' else f'{region}.unr'
    p = _find_file(maps_dir, [name])
    if not p:
        raise GeoError(f'нет файла карты {name}')
    return p


def load_terrain(maps_dir, tex_dir, region, variant='main'):
    """TerrainInfo + G16 → (heights[HM][HM] в мировых Z, holes set[(qx,qy)], loc)."""
    unr = map_path(maps_dir, region, variant)
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
    больше max_step."""
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
                out += struct.pack('<h', cells[0][0])
            else:
                out.append(1)
                for h, n in cells:
                    out += struct.pack('<H', enc(h, n))
    return bytes(out)


def generate_region(client_dir, region, max_step=UP_STEP, variant='main'):
    """Клиент → l2j-байты региона (этап 1: только рельеф)."""
    maps_dir = os.path.join(client_dir, 'Maps')
    tex_dir = os.path.join(client_dir, 'Textures')
    if not os.path.isdir(maps_dir):
        maps_dir = os.path.join(client_dir, 'MAPS')
    heights, holes, loc = load_terrain(maps_dir, tex_dir, region, variant)
    hcell, hole_cell = terrain_cells(region, heights, holes, loc)
    return build_l2j(hcell, hole_cell, max_step)


# ─────────────────────────── этап 2: статик-меши ───────────────────────────

# ─── пороги heightfield-ядра (пол ≤40°, стена ≥75°) ───
WALK_NZ = 0.766        # nz/|n| ≥ cos(40°) И nz>0 → floor-спан (грань-пол, вверх);
WALL_STEEP = 0.26      # |nz|/|n| < cos(75°) → стена-грань (почти вертикаль) даёт
                       # рёберную блокировку NSWE; склоны 40–75° (валуны/скалы) —
                       # не стены (иначе природа плодит ложные блокировки)
LAYER_MERGE = 24       # floor-спаны ближе 24ю — один слой (артефакт округления
                       # высот на 8-basis)
AGENT_HEIGHT = 128     # клиренс: над floor-спаном нужно ≥ этого пустоты до ближайшей
                       # поверхности сверху (потолок/пол), иначе там не встать —
                       # это конструктивно отсекает «дно» мешей и низкие полости
BIG_PLANE = 4096       # floor/ceiling-грань с XY-габаритом больше этого — не пол, а
                       # зонная/водная/задняя плоскость уровня (в BSP они размером с
                       # регион; настоящие полы интерьеров мелкие).


def _accumulate_spans(tris, west, north):
    """Растеризация геометрии в heightfield-спаны floor/ceiling (Recast-style).

    Каждый треугольник по нормали раскидывается по накрытым ячейкам с
    вертикальным пересечением в центре ячейки:
      floors[cell] → list[z] — грань-пол (пологая, нормаль ВВЕРХ, наклон ≤40°);
      ceils[cell]  → list[z] — потолок/дно (пологая, нормаль ВНИЗ) — не пол, но
                     ограничивает клиренс полов под ним (_span_layers).
    Крутые грани (стены зданий/оград) → рёберные блокировки NSWE: закрывают
    КОНКРЕТНУЮ границу ячейки (одно направление), а не глушат клетку целиком —
    как в реальной геодате. Огромная горизонталь
    ИЛИ вертикаль (>BIG_PLANE) — зонная/водная плоскость уровня, не пол и не
    стена (иначе граница зоны дала бы лавину ложных блокировок в поле)."""
    floors = {}
    ceils = {}
    edges_x = {}
    edges_y = {}
    for tri in tris:
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        ln2 = nx * nx + ny * ny + nz * nz
        if ln2 <= 0:
            continue
        x0, x1 = min(ax, bx, cx), max(ax, bx, cx)
        y0, y1 = min(ay, by, cy), max(ay, by, cy)
        if x1 - x0 > BIG_PLANE or y1 - y0 > BIG_PLANE:  # зонная/водная плоскость
            continue
        slope = abs(nz) / math.sqrt(ln2)
        if slope < WALK_NZ:                       # круче 40° — не пол/потолок
            if slope < WALL_STEEP:                # почти вертикаль (≥75°) — стена
                _edge_cross(edges_x, edges_y, (ax, ay, az), (bx, by, bz),
                            (cx, cy, cz), west, north)
            # 40°–75° — крутой склон (валун/скала): ни пол, ни стена; закрытость
            # такого места даёт перепад высот соседей, а не рёберная блокировка
            continue
        gx0 = max(0, int((x0 - west) // 16))
        gx1 = min(CELLS - 1, int((x1 - west) // 16))
        gy0 = max(0, int((y0 - north) // 16))
        gy1 = min(CELLS - 1, int((y1 - north) // 16))
        if gx1 < gx0 or gy1 < gy0:
            continue
        d00 = vx * vx + vy * vy
        d01 = vx * ux + vy * uy
        d11 = ux * ux + uy * uy
        den = d00 * d11 - d01 * d01
        if abs(den) < 1e-9:
            continue
        inv = 1.0 / den
        bucket = floors if nz > 0 else ceils     # вверх — пол, вниз — потолок/дно
        for gy in range(gy0, gy1 + 1):
            py = north + gy * 16 + 8 - ay
            for gx in range(gx0, gx1 + 1):
                px = west + gx * 16 + 8 - ax
                d02 = vx * px + vy * py
                d12 = ux * px + uy * py
                u = (d11 * d02 - d01 * d12) * inv
                v = (d00 * d12 - d01 * d02) * inv
                if u >= -0.02 and v >= -0.02 and u + v <= 1.02:
                    z = az + u * vz + v * uz      # bary: P = A + u*AC + v*AB
                    if -16384 <= z <= 16376:      # плоскости мирового куба — мимо
                        bucket.setdefault(gy * CELLS + gx, []).append(int(z))
    return floors, ceils, edges_x, edges_y


def _edge_cross(edges_x, edges_y, A, B, C, west, north):
    """Пересечения стеновой грани с сетками x=const и y=const → z-интервалы на
    границах ячеек. edges_x[(gx,gy)] — граница между (gx-1,gy) и (gx,gy);
    edges_y[(gx,gy)] — между (gx,gy-1) и (gx,gy)."""
    for axis, edges in ((0, edges_x), (1, edges_y)):
        origin = west if axis == 0 else north
        lo = min(A[axis], B[axis], C[axis])
        hi = max(A[axis], B[axis], C[axis])
        k0 = int((lo - origin) // 16) + 1
        k1 = int((hi - origin) // 16)
        for k in range(max(k0, 1), min(k1, CELLS - 1) + 1):
            plane = origin + k * 16
            pts = []
            for P, Q in ((A, B), (B, C), (C, A)):
                d0, d1 = P[axis] - plane, Q[axis] - plane
                if d0 == d1:
                    continue
                if d0 * d1 <= 0:
                    t = d0 / (d0 - d1)
                    other = 1 - axis
                    o = P[other] + t * (Q[other] - P[other])
                    z = P[2] + t * (Q[2] - P[2])
                    pts.append((o, z))
            if len(pts) < 2:
                continue
            (o1, z1), (o2, z2) = pts[0], pts[-1]
            if o2 < o1:
                o1, o2, z1, z2 = o2, o1, z2, z1
            oorigin = north if axis == 0 else west
            c0 = max(0, int((o1 - oorigin) // 16))
            c1 = min(CELLS - 1, int((o2 - oorigin) // 16))
            for c in range(c0, c1 + 1):
                s_lo = max(o1, oorigin + c * 16)
                s_hi = min(o2, oorigin + (c + 1) * 16)
                if o2 > o1:
                    t_lo = (s_lo - o1) / (o2 - o1)
                    t_hi = (s_hi - o1) / (o2 - o1)
                    za = z1 + t_lo * (z2 - z1)
                    zb = z1 + t_hi * (z2 - z1)
                else:
                    za, zb = z1, z2
                key = (k, c) if axis == 0 else (c, k)
                lst = edges.setdefault(key, [])
                iv = (min(za, zb), max(za, zb))
                if not (lst and lst[-1] == iv):
                    lst.append(iv)


CROSS_LO = 24    # стена перекрывает шаг, если её z-интервал пересекает корпус
CROSS_HI = 96    # персонажа над слоем: (z+CROSS_LO, z+CROSS_HI)


def _edge_blocked(intervals, z):
    """Есть ли стена-интервал, перекрывающий корпус персонажа над слоем z."""
    for wlo, whi in intervals:
        if whi > z + CROSS_LO and wlo < z + CROSS_HI:
            return True
    return False



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
            root = own_t is not None and _step_ok(z - own_t)
            if not root:
                # шаг с чистого соседнего рельефа (ячейки без полов)
                for ngx, ngy in ((gx, gy - 1), (gx, gy + 1), (gx - 1, gy), (gx + 1, gy)):
                    if not (0 <= ngx < CELLS and 0 <= ngy < CELLS):
                        continue
                    ncell = ngy * CELLS + ngx
                    if ncell in cell_layers or hole_cell[ngy][ngx]:
                        continue
                    if _step_ok(z - hcell[ngy][ngx]):
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


def build_l2j_full(hcell, hole_cell, floors, ceils, max_step=UP_STEP, blocked=(),
                   edges_x=None, edges_y=None):
    """Рельеф + heightfield-спаны → l2j с multilayer; NSWE по перепаду высот
    (+ рёберные блокировки: стены мешей edges_x/y и BlockingVolume blocked)."""
    blocked = blocked or set()
    edges_x = edges_x or {}
    edges_y = edges_y or {}
    # слои по ячейкам: рельеф (всегда проходим) + полы мешей с клиренсом по
    # потолкам. Рельеф не гасится — глухих ячеек почти нет (как в реальной
    # геодате). Дыры QuadVisibilityBitmap НЕ делаем непроходимыми: в застройке
    # это зоны, где террейн заменён мешем-мостовой, а не провалы; эталонная
    # геодата тоже трактует их как проходимый рельеф (иначе город глухой на 5%).
    cell_layers = {}
    for cell in set(floors) | set(ceils):
        gy, gx = divmod(cell, CELLS)
        ls = _span_layers(floors.get(cell, ()), ceils.get(cell, ()), [hcell[gy][gx]])
        if ls:
            cell_layers[cell] = ls
    _prune_unreachable(cell_layers, hcell, hole_cell, max_step)

    def layers_at(gx, gy):
        cell = gy * CELLS + gx
        ls = cell_layers.get(cell)
        if ls is not None:
            return ls
        return (hcell[gy][gx],)

    def enc(h, n):
        h = max(-16384, min(16376, h))
        return ((h << 1) & 0xFFF0) | n
    out = bytearray()
    for bx in range(256):
        for by in range(256):
            cells = []
            multi = False
            flat_ok = True
            hs = set()
            for cx in range(8):
                gx = bx * 8 + cx
                for cy in range(8):
                    gy = by * 8 + cy
                    ls = layers_at(gx, gy) or (hcell[gy][gx],)  # рельеф — минимум
                    lay = []
                    for z in ls:
                        nswe = 15
                        # NSWE как в реальной геодате (~60% блокировок —
                        # стены, ~40% — склоны). Направление закрыто, если: у соседа
                        # нет слоя в пределах шага по высоте (склон/обрыв); ИЛИ
                        # границу перекрывает стена-грань меша (edges_x/y с z-интер-
                        # валом на уровне корпуса персонажа); ИЛИ BlockingVolume.
                        wall_bits = 0                          # закрытые ТОЛЬКО стеной
                        for bit, ngx, ngy, bkey, ekey, ed in (
                                (8, gx, gy - 1, ('y', gx, gy), (gx, gy), edges_y),
                                (4, gx, gy + 1, ('y', gx, gy + 1), (gx, gy + 1), edges_y),
                                (2, gx - 1, gy, ('x', gx, gy), (gx, gy), edges_x),
                                (1, gx + 1, gy, ('x', gx + 1, gy), (gx + 1, gy), edges_x)):
                            if not (0 <= ngx < CELLS and 0 <= ngy < CELLS):
                                continue
                            nls = layers_at(ngx, ngy)
                            height_ok = any(_step_ok(nz - z, max_step) for nz in nls)
                            if bkey in blocked:                # BlockingVolume
                                nswe &= ~bit
                                if height_ok:                  # ровно — снимаемо anti-глухой
                                    wall_bits |= bit
                                continue
                            if not height_ok:
                                nswe &= ~bit                   # перепад высот (склон/обрыв)
                                continue
                            ivs = ed.get(ekey)                 # стена-грань меша
                            if ivs and _edge_blocked(ivs, z):
                                nswe &= ~bit
                                wall_bits |= bit
                        # anti-глухая: тонкий вертикальный объект (ствол/столб) не
                        # должен запирать проходимую клетку со всех сторон — если её
                        # заперли ТОЛЬКО стены, возвращаем сторону обхода. Держим
                        # правило для всех ячеек: глухих в разы меньше эталона, ходьба
                        # чистая. Известный компромисс — полностью замкнутый рукотвор-
                        # ный карман 1×1 (чулан без двери) станет односторонне прохо-
                        # димым; в реальных картах это ничтожно редко.
                        if nswe == 0 and wall_bits:
                            nswe = wall_bits
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
                out += struct.pack('<h', cells[0][0][0])
            else:
                out.append(1)
                for lay in cells:
                    out += struct.pack('<H', enc(*lay[0]))
    return bytes(out)


def generate_region_full(client_dir, region, max_step=UP_STEP, progress_cb=None,
                         variant='main'):
    """Клиент → l2j: рельеф + статик-меши и BSP-модель (полы/потолки в спаны) +
    BlockingVolume (заградительные рёбра NSWE). Heightfield-ядро, NSWE по высоте."""
    from .meshes import region_mesh_triangles
    maps_dir = os.path.join(client_dir, 'Maps')
    if not os.path.isdir(maps_dir):
        maps_dir = os.path.join(client_dir, 'MAPS')
    tex_dir = os.path.join(client_dir, 'Textures')
    usx_dir = os.path.join(client_dir, 'StaticMeshes')
    heights, holes, loc = load_terrain(maps_dir, tex_dir, region, variant)
    hcell, hole_cell = terrain_cells(region, heights, holes, loc)
    rx, ry = map(int, region.split('_'))
    west, north = (rx - 20) * REGION_UNITS, (ry - 18) * REGION_UNITS
    from .unreal import Package
    unr = map_path(maps_dir, region, variant)
    tris, skipped = region_mesh_triangles(unr, usx_dir, progress_cb)
    from .meshes import level_extra_triangles
    solid, blocking = level_extra_triangles(Package(unr))
    # конвенция высот едина для всего: меши и BSP сдвигаются так же, как рельеф
    zc = Z_CALIBRATION
    tris = [tuple((x, y, z + zc) for x, y, z in t) for t in tris]
    solid = [tuple((x, y, z + zc) for x, y, z in t) for t in solid]
    # Единый heightfield: статик-меши + BSP-модель уровня растеризуются в спаны
    # floor/ceiling. Клиренс, классификация нормали (дно/потолок ≠ пол) и фильтр
    # BIG_PLANE (region-плоскости зон/воды) конструктивно убирают паразитные слои.
    # Проходимость NSWE — по перепаду высот соседей , плюс
    # заградительные рёбра BlockingVolume (невидимые границы играбельной зоны).
    floors, ceils, edges_x, edges_y = _accumulate_spans(tris + solid, west, north)
    blocked = _blocking_edges(blocking, west, north)
    data = build_l2j_full(hcell, hole_cell, floors, ceils, max_step, blocked,
                          edges_x, edges_y)
    return data, len(tris) + len(solid), skipped


# ─────────────────────────── команда generate ───────────────────────────

def _worker(job):
    """Один регион одного варианта (для пула процессов).

    Атомарная запись: сперва во временный файл, валидация, затем
    os.replace → каноничный .l2j появляется только целым и проверенным.
    Прерывание (Ctrl+C/terminate) в момент записи оставит максимум .tmp,
    но никогда — обрезанный или невалидный .l2j.
    """
    client_dir, region, variant, out_path, max_step, terrain_only = job
    tmp = out_path + '.tmp'
    try:
        if terrain_only:
            data = generate_region(client_dir, region, max_step, variant)
        else:
            data, _n, _s = generate_region_full(client_dir, region, max_step,
                                                variant=variant)
        with open(tmp, 'wb') as f:
            f.write(data)
        from .convert import validate_l2j
        validate_l2j(tmp)
        os.replace(tmp, out_path)                 # атомарная подмена
        return (region, variant, None)
    except BaseException as e:                     # incl. KeyboardInterrupt/SystemExit
        if os.path.exists(tmp):
            os.remove(tmp)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return (region, variant, f'{type(e).__name__}: {e}')



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


def cmd_generate(client_dir, out_dir, regions=None, max_step=UP_STEP,
                 terrain_only=False, jobs=None):
    """Генерация из клиента: оба варианта карт по папкам out/main и out/classic.

    main/XX_YY.l2j — из XX_YY.unr (все регионы);
    classic/XX_YY.l2j — из XX_YY_Classic.unr (только переработанные зоны;
    для классик-сервера поверх main кладутся файлы из classic)."""
    import glob as _g
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
    mains = sorted(m.group(1) for f in listing
                   for m in [_re.match(r'^(\d+_\d+)\.unr$', f, _re.IGNORECASE)] if m)
    classics = sorted(m.group(1) for f in listing
                      for m in [_re.match(r'^(\d+_\d+)_classic\.unr$', f, _re.IGNORECASE)] if m)
    if regions:
        mains = [r for r in mains if r in regions]
        classics = [r for r in classics if r in regions]
    tasks = ([(client_dir, r, 'main', os.path.join(out_dir, 'main', r + '.l2j'),
               max_step, terrain_only) for r in mains] +
             [(client_dir, r, 'classic', os.path.join(out_dir, 'classic', r + '.l2j'),
               max_step, terrain_only) for r in classics])
    if not tasks:
        print(red('  ✗ нет карт вида XX_YY.unr'))
        return 1
    os.makedirs(os.path.join(out_dir, 'main'), exist_ok=True)
    if classics:
        os.makedirs(os.path.join(out_dir, 'classic'), exist_ok=True)
    nproc = jobs or _auto_jobs()
    ram = _total_ram_gb()
    how = 'задано -j' if jobs else (f'авто: половина ресурсов'
          + (f' от {ram:.0f} ГБ ОЗУ / {os.cpu_count() or "?"} потоков' if ram else ''))
    print(f'  {bold("Генерация")}: main {len(mains)} + classic {len(classics)}'
          f' → {out_dir} · {nproc} процессов ({how})'
          f'{" · только рельеф" if terrain_only else ""}\n')
    print(dim('  (Ctrl+C — прервать; готовые файлы сохранятся)\n'))
    t0 = _t.time()
    ok, errors, done = 0, [], 0
    total = len(tasks)
    aborted = False
    # воркеры игнорируют SIGINT — прерывание ловит только главный процесс,
    # чтобы Ctrl+C не сыпал трейсбеками из пула
    import signal
    orig = signal.signal(signal.SIGINT, signal.SIG_IGN)
    pool = mp.Pool(nproc)
    signal.signal(signal.SIGINT, orig)
    try:
        for i, (region, variant, err) in enumerate(
                pool.imap_unordered(_worker, tasks), 1):
            done = i
            if err is None:
                ok += 1
            else:
                errors.append((f'{region}/{variant}', err))
            progress(i, total, 'генерация ')
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
        for sub in ('main', 'classic'):
            for t in _g2.glob(os.path.join(out_dir, sub, '*.l2j.tmp')):
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
        print(dim('    (пропущены регионы без heightmap в клиенте — там нечего'
                  ' генерировать; на классик-сервере такой квадрат берётся из main/)'))
    if not terrain_only:
        main_dir = os.path.join(out_dir, 'main')
        print(f'\n  {bold("Что дальше:")}')
        print(f'    • {out_dir}/{bold("main")}/ — геодата всего мира. Для обычного'
              ' (не classic) сервера бери её целиком.')
        if classics:
            print(f'    • {out_dir}/{bold("classic")}/ — {len(classics)} переработанных'
                  ' для Classic зон. Для classic-сервера: возьми main/ и скопируй'
                  ' classic/ поверх (замещает эти квадраты).')
        print(f'\n  {yellow("Проверь перед установкой:")}')
        print(f'    geotool.py view {main_dir}   — глянуть карту/города/слои в браузере')
        print('    geotool.py diff <твоя_геодата> ' + main_dir
              + '   — сравнить с текущей (высоты и проходимость)')
        print(dim('    …и обязательно пройди ключевые зоны в игре — генерация'
                  ' не проверяет их сама.'))
    return 0 if not errors else 2
