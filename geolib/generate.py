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

    is_classic = '_classic' in os.path.basename(unr).lower()
    utx_names = ([f'T_{region}_Classic.utx', f'T_{region}.utx'] if is_classic
                 else [f'T_{region}.utx'])
    utx = _find_file(tex_dir, utx_names)
    if not utx:
        raise GeoError(f'нет текстуры высот T_{region}.utx')
    tpkg = Package(utx)
    tex = [e for e in tpkg.find_exports('Texture') if e.name == tm_name]
    if not tex:
        raise GeoError(f'T_{region}.utx: нет текстуры {tm_name}')
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

WALK_NZ = 0.5          # |нормаль.z| ≥ 0.5 → пол (склон ≤ 60°)
LAYER_MERGE = 24       # слои ближе 24 юнитов сливаются
CLEARANCE = 64         # под слоем должно быть ≥ 64 юнитов, чтобы там стоять
WALL_HEAD = 32         # стена мешает проходу, если торчит выше слоя на 32+


def _rasterize(tris, west, north, forced_walls=()):
    """Треугольники → (floors, walls).

    floors: dict cell → list[int z] — высоты полов в центре ячейки;
    walls:  dict cell → list[(zlo, zhi)] — вертикальные препятствия.
    forced_walls — треугольники, считающиеся стеной независимо от наклона
    (BlockingVolume)."""
    floors = {}
    walls = {}
    edges_x = {}
    edges_y = {}
    for tri_i, ((ax, ay, az), (bx, by, bz), (cx, cy, cz)) in enumerate(
            list(tris) + list(forced_walls)):
        forced = tri_i >= len(tris)
        # нормаль
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        ln2 = nx * nx + ny * ny + nz * nz
        if ln2 <= 0:
            continue
        walkable = (not forced) and abs(nz) / math.sqrt(ln2) >= WALK_NZ
        # bbox в ячейках
        x0, x1 = min(ax, bx, cx), max(ax, bx, cx)
        y0, y1 = min(ay, by, cy), max(ay, by, cy)
        gx0 = max(0, int((x0 - west) // 16))
        gx1 = min(CELLS - 1, int((x1 - west) // 16))
        gy0 = max(0, int((y0 - north) // 16))
        gy1 = min(CELLS - 1, int((y1 - north) // 16))
        if gx1 < gx0 or gy1 < gy0:
            continue
        if walkable:
            # точечный тест центра ячейки + z по плоскости
            d00 = vx * vx + vy * vy
            d01 = vx * ux + vy * uy
            d11 = ux * ux + uy * uy
            den = d00 * d11 - d01 * d01
            if abs(den) < 1e-9:
                continue
            inv = 1.0 / den
            for gy in range(gy0, gy1 + 1):
                py = north + gy * 16 + 8 - ay
                for gx in range(gx0, gx1 + 1):
                    px = west + gx * 16 + 8 - ax
                    d02 = vx * px + vy * py
                    d12 = ux * px + uy * py
                    u = (d11 * d02 - d01 * d12) * inv
                    v = (d00 * d12 - d01 * d02) * inv
                    if u >= -0.02 and v >= -0.02 and u + v <= 1.02:
                        z = az + u * vz + v * uz  # bary: P = A + u*AC + v*AB
                        if -16384 <= z <= 16376:  # плоскости мирового куба — мимо
                            floors.setdefault(gy * CELLS + gx, []).append(int(z))
        else:
            # стена: (а) пометка ячеек по рёбрам (стоять внутри стены нельзя);
            # (б) точные пересечения с границами ячеек — рёберные блокировки
            # NSWE (заборы, перила, стены проёмов).
            zlo, zhi = min(az, bz, cz), max(az, bz, cz)
            for (px0, py0, pz0), (px1, py1, pz1) in (((ax, ay, az), (bx, by, bz)),
                                                     ((bx, by, bz), (cx, cy, cz)),
                                                     ((cx, cy, cz), (ax, ay, az))):
                dx, dy = px1 - px0, py1 - py0
                steps = max(1, int(max(abs(dx), abs(dy)) / 8))
                for s in range(steps + 1):
                    t = s / steps
                    gx = int((px0 + dx * t - west) // 16)
                    gy = int((py0 + dy * t - north) // 16)
                    if 0 <= gx < CELLS and 0 <= gy < CELLS:
                        cell = gy * CELLS + gx
                        lst = walls.setdefault(cell, [])
                        if lst and lst[-1][0] == zlo and lst[-1][1] == zhi:
                            continue
                        lst.append((zlo, zhi))
            _edge_cross(edges_x, edges_y, (ax, ay, az), (bx, by, bz), (cx, cy, cz),
                        west, north)
    return floors, walls, edges_x, edges_y


def _edge_cross(edges_x, edges_y, A, B, C, west, north):
    """Пересечения стенового треугольника с сетками x=const и y=const.

    edges_x[(gx, gy)] — граница между ячейками (gx−1, gy) и (gx, gy);
    edges_y[(gx, gy)] — между (gx, gy−1) и (gx, gy). Значения — z-интервалы."""
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
                # z-диапазон отрезка в пределах этой ячейки-строки
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
                if lst and lst[-1] == iv:
                    continue
                lst.append(iv)


def _merge_layers(zs):
    """Кандидаты высот → слои (по убыванию): слияние близких и
    clearance-фильтр — слой существует, только если над ним есть место
    для персонажа (декор и камни не плодят недосягаемых слоёв)."""
    zs = sorted(set(zs), reverse=True)
    layers = []
    for z in zs:
        if not layers:
            layers.append(z)
        elif layers[-1] - z > CLEARANCE:
            layers.append(z)
        # z ближе CLEARANCE к вышележащему слою: стоять нельзя — пропуск
    return layers[:120]


def _blocked(walls, cell, z):
    """Стоит ли над слоем z стена в ячейке cell."""
    for zlo, zhi in walls.get(cell, ()):
        if zlo <= z + WALL_HEAD and zhi >= z + WALL_HEAD:
            return True
    return False


CROSS_LO = 24    # стена мешает шагу, если перекрывает корпус персонажа:
CROSS_HI = 96    # её интервал пересекает (z+CROSS_LO, z+CROSS_HI)



def _edge_blocked(intervals, z):
    for wlo, whi in intervals:
        if whi > z + CROSS_LO and wlo < z + CROSS_HI:
            return True
    return False


MIN_ISLAND = 64        # изолированные «острова» слоёв меньше этого — мусор


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


def build_l2j_full(hcell, hole_cell, floors, walls, max_step=UP_STEP,
                   edges_x=None, edges_y=None):
    """Рельеф + меши → l2j с multilayer и стеновыми блокировками."""
    # слои по ячейкам: рельеф (если не дыра) + полы мешей
    cell_layers = {}
    for cell, zs in floors.items():
        gy, gx = divmod(cell, CELLS)
        base = [] if hole_cell[gy][gx] else [hcell[gy][gx]]
        cell_layers[cell] = _merge_layers(zs + base)
    _prune_unreachable(cell_layers, hcell, hole_cell, max_step)

    def layers_at(gx, gy):
        cell = gy * CELLS + gx
        ls = cell_layers.get(cell)
        if ls is not None:
            return ls
        if hole_cell[gy][gx]:
            return ()
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
                    ls = layers_at(gx, gy)
                    if not ls:
                        # дыра без полов: непроходимая ячейка на высоте рельефа
                        ls = (hcell[gy][gx],)
                        lay = [(ls[0], 0)]
                        cells.append(lay)
                        flat_ok = False
                        hs.add(ls[0] & 0xFFF8)
                        continue
                    cell = gy * CELLS + gx
                    lay = []
                    for z in ls:
                        nswe = 15
                        if _blocked(walls, cell, z):
                            nswe = 0
                        else:
                            # рёберные блокировки: N — граница edges_y[(gx,gy)],
                            # S — edges_y[(gx,gy+1)], W — edges_x[(gx,gy)],
                            # E — edges_x[(gx+1,gy)]
                            for bit, ngx, ngy, ek, ed in (
                                    (8, gx, gy - 1, (gx, gy), edges_y),
                                    (4, gx, gy + 1, (gx, gy + 1), edges_y),
                                    (2, gx - 1, gy, (gx, gy), edges_x),
                                    (1, gx + 1, gy, (gx + 1, gy), edges_x)):
                                if not (0 <= ngx < CELLS and 0 <= ngy < CELLS):
                                    continue
                                nls = layers_at(ngx, ngy)
                                ok = any(_step_ok(nz - z, max_step) for nz in nls)
                                if ok and _blocked(walls, ngy * CELLS + ngx, z):
                                    ok = False
                                if ok and ed is not None:
                                    ivs = ed.get(ek)
                                    if ivs and _edge_blocked(ivs, z):
                                        ok = False
                                if not ok:
                                    nswe &= ~bit
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
    """Клиент → l2j: рельеф + статик-меши (полы, стены) + BlockingVolume."""
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
    tris.extend(solid)
    # конвенция высот едина для всего: меши и BSP сдвигаются так же, как рельеф
    zc = Z_CALIBRATION
    tris = [tuple((x, y, z + zc) for x, y, z in t) for t in tris]
    blocking = [tuple((x, y, z + zc) for x, y, z in t) for t in blocking]
    floors, walls, edges_x, edges_y = _rasterize(tris, west, north, blocking)
    data = build_l2j_full(hcell, hole_cell, floors, walls, max_step,
                          edges_x, edges_y)
    return data, len(tris), skipped


# ─────────────────────────── команда generate ───────────────────────────

def _worker(job):
    """Один регион одного варианта (для пула процессов)."""
    client_dir, region, variant, out_path, max_step, terrain_only = job
    try:
        if terrain_only:
            data = generate_region(client_dir, region, max_step, variant)
        else:
            data, _n, _s = generate_region_full(client_dir, region, max_step,
                                                variant=variant)
        open(out_path, 'wb').write(data)
        from .convert import validate_l2j
        validate_l2j(out_path)
        return (region, variant, None)
    except Exception as e:                        # noqa: BLE001 — воркер не должен падать молча
        if os.path.exists(out_path):
            os.remove(out_path)
        return (region, variant, f'{type(e).__name__}: {e}')



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
    from .ui import bold, green, progress, red, yellow
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
    nproc = jobs or max(1, (os.cpu_count() or 4) - 1)
    print(f'  {bold("Генерация")}: main {len(mains)} + classic {len(classics)}'
          f' → {out_dir} · {nproc} процессов'
          f'{" · только рельеф" if terrain_only else ""}\n')
    t0 = _t.time()
    ok, errors = 0, []
    with mp.Pool(nproc) as pool:
        for i, (region, variant, err) in enumerate(
                pool.imap_unordered(_worker, tasks), 1):
            if err is None:
                ok += 1
            else:
                errors.append((f'{region}/{variant}', err))
            progress(i, len(tasks), 'генерация ')
    print(f'\n  {bold("Итог")} за {(_t.time() - t0) / 60:.1f} мин:'
          f' {green(f"✓ {ok}")}' + (f' · {red(f"✗ {len(errors)}")}' if errors else ''))
    for name, why in errors[:10]:
        print(f'    {red("✗")} {name}: {why}')
    if not terrain_only:
        print(f'\n  {yellow("Классик-сервер:")} основа — main/, поверх — файлы из classic/.')
        print(f'  Проверка: geotool.py view … и geotool.py diff … с рабочей геодатой.')
    return 0 if not errors else 2
