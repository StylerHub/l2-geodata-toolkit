#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кроссвендорный GPU/CPU-бэкенд рейкаста на Taichi (CUDA/Vulkan/Metal/CPU).

Опционально: если Taichi не установлен, вызывающая сторона откатывается на
чистый Python. Taichi требует Python ≤3.13; на любой поддерживаемой карте
(NVIDIA/AMD/Intel/Apple) работает один и тот же код.

Данные готовятся в Python (те же _prep_geometry-грани), затем лучи считаются
параллельно по ячейкам/границам на устройстве. Результат (полы/потолки на
ячейку, стены на границу-слой) возвращается в Python для сборки L2J.
"""

_TI = None            # модуль taichi после init (или None)
_ARCH = None          # выбранный бэкенд (для отчёта)
CELLS = 2048


def available():
    """Установлен ли Taichi (без инициализации устройства)."""
    try:
        import taichi  # noqa: F401
        return True
    except Exception:
        return False


def init(prefer_gpu=True):
    """Инициализировать Taichi с ОБЯЗАТЕЛЬНЫМ f64 — результат обязан совпадать с
    CPU-эталоном байт-в-байт независимо от железа. GPU берётся, ТОЛЬКО если он
    поддерживает f64 (CUDA/часть Vulkan). Устройство без f64 (Metal/Apple) для
    GPU не годится — откат на Taichi-CPU (тоже f64, нативный многопоточный код,
    идентичный результат). Возвращает имя бэкенда или None если Taichi недоступен."""
    global _TI, _ARCH
    if _TI is not None:
        return _ARCH
    try:
        import taichi as ti
    except Exception:
        return None
    ok = False
    if prefer_gpu:
        try:                                          # GPU только с f64
            ti.init(arch=ti.gpu, default_fp=ti.f64, offline_cache=True,
                    log_level=ti.ERROR)
            # пробный f64-kernel: Metal упадёт здесь («f64 not supported»)
            _probe_f64(ti)
            ok = True
        except Exception:
            ok = False
    if not ok:                                        # CPU-бэкенд f64 (везде есть)
        ti.init(arch=ti.cpu, default_fp=ti.f64, log_level=ti.ERROR)
        _probe_f64(ti)
    _TI = ti
    _ARCH = str(ti.lang.impl.current_cfg().arch).rsplit('.', 1)[-1]
    return _ARCH


def _probe_f64(ti):
    """Скомпилировать f64-kernel, РЕПРЕЗЕНТАТИВНЫЙ реальным лучам (деление,
    сравнения, cast) — падает на устройствах без полноценного f64 (Metal, часть
    Vulkan/MoltenVK, где простое умножение компилируется, а деление — уже нет)."""
    a = ti.ndarray(ti.f64, shape=(4,))

    @ti.kernel
    def k(a: ti.types.ndarray()):
        for i in range(4):
            x = a[i] * 2.0 + 1.0
            f = 1.0 / (x + 3.0)                 # f64-деление (валит неполный f64)
            s = 0.0
            if f > 0.0 and f <= 1.0:            # f64-сравнения
                s = f * (x - a[i])
            a[i] = s + ti.cast(f > 0.5, ti.f64)
    k(a)
    ti.sync()


def zray_columns(fc, fc_grid, west, north, cells_order, walk_nz):
    """Z-луч на устройстве: для каждой ячейки cells_order[i] с гранями fc_grid —
    пересечения вертикали в центре с гранями-полами/потолками fc.
    Возвращает (out_z[n,max_hits], out_isfloor[n,max_hits], out_cnt[n]) как numpy,
    где max_hits = максимальная степень ячейки (без усечения слоёв).
    fc[i] = (is_floor, ax,ay,az, ux,uy,uz, vx,vy,vz, d00,d01,d11, inv)."""
    ti = _TI
    import numpy as np
    n = len(cells_order)
    # CSR-раскладка граней по ячейкам порядка cells_order
    offs = np.zeros(n + 1, dtype=np.int32)
    flat = []
    for i, cell in enumerate(cells_order):
        idxs = fc_grid.get(cell, ())
        flat.extend(idxs)
        offs[i + 1] = len(flat)
    flat = np.asarray(flat, dtype=np.int32)
    # число пересечений вертикали в ячейке ≤ числу её граней → выделяем ровно
    # максимальную степень (без усечения слоёв, в отличие от фикс. лимита).
    max_hits = int(np.max(offs[1:] - offs[:-1])) if n else 1
    max_hits = max(max_hits, 1)
    fc_arr = np.asarray(fc, dtype=np.float64)                 # (N_fc, 14) — f64!
    cellxy = np.empty((n, 2), dtype=np.int32)
    for i, cell in enumerate(cells_order):
        cellxy[i, 0] = cell % CELLS
        cellxy[i, 1] = cell // CELLS

    f_fc = ti.ndarray(ti.f64, shape=fc_arr.shape)
    f_flat = ti.ndarray(ti.i32, shape=flat.shape if flat.size else (1,))
    f_off = ti.ndarray(ti.i32, shape=offs.shape)
    f_cxy = ti.ndarray(ti.i32, shape=(n, 2))
    o_z = ti.ndarray(ti.f64, shape=(n, max_hits))
    o_fl = ti.ndarray(ti.i32, shape=(n, max_hits))
    o_cnt = ti.ndarray(ti.i32, shape=(n,))
    f_fc.from_numpy(fc_arr)
    if flat.size:
        f_flat.from_numpy(flat)
    f_off.from_numpy(offs)
    f_cxy.from_numpy(cellxy)

    @ti.kernel
    def zray(f_fc: ti.types.ndarray(), f_flat: ti.types.ndarray(),
             f_off: ti.types.ndarray(), f_cxy: ti.types.ndarray(),
             o_z: ti.types.ndarray(), o_fl: ti.types.ndarray(),
             o_cnt: ti.types.ndarray(), west: ti.f64, north: ti.f64):
        for i in range(n):
            px = west + f_cxy[i, 0] * 16 + 8
            py = north + f_cxy[i, 1] * 16 + 8
            cnt = 0
            for k in range(f_off[i], f_off[i + 1]):
                fi = f_flat[k]
                ax = f_fc[fi, 1]
                ay = f_fc[fi, 2]
                az = f_fc[fi, 3]
                ux = f_fc[fi, 4]
                uy = f_fc[fi, 5]
                uz = f_fc[fi, 6]
                vx = f_fc[fi, 7]
                vy = f_fc[fi, 8]
                vz = f_fc[fi, 9]
                d00 = f_fc[fi, 10]
                d01 = f_fc[fi, 11]
                d11 = f_fc[fi, 12]
                inv = f_fc[fi, 13]
                pxr = px - ax
                pyr = py - ay
                d02 = vx * pxr + vy * pyr
                d12 = ux * pxr + uy * pyr
                u = (d11 * d02 - d01 * d12) * inv
                v = (d00 * d12 - d01 * d02) * inv
                if u >= -0.02 and v >= -0.02 and u + v <= 1.02:
                    z = az + u * vz + v * uz
                    if z >= -16384.0 and z <= 16376.0 and cnt < max_hits:
                        o_z[i, cnt] = z
                        o_fl[i, cnt] = ti.cast(f_fc[fi, 0] > 0.5, ti.i32)
                        cnt += 1
            o_cnt[i] = cnt

    zray(f_fc, f_flat, f_off, f_cxy, o_z, o_fl, o_cnt, float(west), float(north))
    ti.sync()
    return o_z.to_numpy(), o_fl.to_numpy(), o_cnt.to_numpy()


def nswe_grid(cell_layers, hcell, walls, w_grid, blocked, west, north,
              up_step, down_step, body_lo, body_hi):
    """NSWE всех слоёв всего грида на устройстве (горячий цикл сборки). Для каждой
    ячейки 2048² и каждого её слоя z считает 4 бита проходимости N,S,W,E по той же
    логике, что и Python build_l2j_ray: перепад высот к слоям соседа (_step_ok),
    заградительные рёбра (blocked) и стена корпуса (X/Y-луч seg_hit по граням
    ячейки И соседа, AND низ+верх), плюс anti-глухая. Стена считается для ЛЮБОЙ
    ячейки со стенами-гранями рядом (включая терраиновые без floor-слоёв).

    Возвращает (nswe_flat, l_off): nswe_flat[l_off[cell]+j] — nswe j-го слоя.
    Слои раскладываются в CSR; большинство ячеек — 1 слой рельефа, мультислои
    редки. hcell — list[C][C] (рельеф, [gy][gx])."""
    ti = _TI
    import numpy as np
    C = CELLS
    N = C * C
    hc_flat = np.asarray(hcell, dtype=np.float64).reshape(-1)   # cell = gy*C+gx

    # ── CSR слоёв: counts=1 (рельеф) либо len(ls) для ячеек с геометрией ──
    counts = np.ones(N, dtype=np.int64)
    for cell, ls in cell_layers.items():
        counts[cell] = len(ls)
    l_off = np.zeros(N + 1, dtype=np.int64)
    np.cumsum(counts, out=l_off[1:])
    total = int(l_off[-1])
    l_z = np.empty(total, dtype=np.float64)
    base_pos = l_off[:N]                                        # смещение слоя-0 ячейки
    l_z[base_pos] = hc_flat                                     # рельеф по умолчанию
    for cell, ls in cell_layers.items():                       # мультислои поверх
        o = int(l_off[cell])
        for j, z in enumerate(ls):
            l_z[o + j] = z

    # ── CSR стен по всем ячейкам грида (разреженный, off[c]==off[c+1] если пусто) ──
    w_counts = np.zeros(N, dtype=np.int64)
    for cell, idxs in w_grid.items():
        w_counts[cell] = len(idxs)
    w_off = np.zeros(N + 1, dtype=np.int64)
    np.cumsum(w_counts, out=w_off[1:])
    w_total = int(w_off[-1])
    w_flat = np.empty(max(w_total, 1), dtype=np.int32)
    for cell, idxs in w_grid.items():
        o = int(w_off[cell])
        w_flat[o:o + len(idxs)] = idxs
    walls_arr = np.asarray(walls, dtype=np.float64) if walls else \
        np.zeros((1, 9), dtype=np.float64)

    # ── заградительные рёбра в два грид-массива ──
    blk_y = np.zeros(N, dtype=np.int8)   # ('y', gx, gy): граница N ячейки (gx,gy)
    blk_x = np.zeros(N, dtype=np.int8)   # ('x', gx, gy): граница W ячейки (gx,gy)
    for key in blocked:
        kind, gx, gy = key
        if 0 <= gx < C and 0 <= gy < C:
            (blk_y if kind == 'y' else blk_x)[gy * C + gx] = 1

    l_off32 = l_off.astype(np.int32)
    f_off = ti.ndarray(ti.i32, shape=(N + 1,))
    f_z = ti.ndarray(ti.f64, shape=(max(total, 1),))
    f_woff = ti.ndarray(ti.i32, shape=(N + 1,))
    f_wflat = ti.ndarray(ti.i32, shape=(max(w_total, 1),))
    f_walls = ti.ndarray(ti.f64, shape=walls_arr.shape)
    f_by = ti.ndarray(ti.i32, shape=(N,))
    f_bx = ti.ndarray(ti.i32, shape=(N,))
    o_nswe = ti.ndarray(ti.i32, shape=(max(total, 1),))
    f_off.from_numpy(l_off32)
    f_z.from_numpy(l_z if total else np.zeros(1, np.float64))
    f_woff.from_numpy(w_off.astype(np.int32))
    f_wflat.from_numpy(w_flat)
    f_walls.from_numpy(walls_arr)
    f_by.from_numpy(blk_y.astype(np.int32))
    f_bx.from_numpy(blk_x.astype(np.int32))

    @ti.func
    def seg_hit(px, py, pz, npx, npy, ax, ay, az,
                e1x, e1y, e1z, e2x, e2y, e2z) -> ti.i32:
        dx = npx - px
        dy = npy - py
        hx = dy * e2z
        hy = -dx * e2z
        hz = dx * e2y - dy * e2x
        a = e1x * hx + e1y * hy + e1z * hz
        res = 0
        if a > 1e-9 or a < -1e-9:
            f = 1.0 / a
            sx = px - ax
            sy = py - ay
            sz = pz - az
            u = f * (sx * hx + sy * hy + sz * hz)
            if u >= 0.0 and u <= 1.0:
                qx = sy * e1z - sz * e1y
                qy = sz * e1x - sx * e1z
                qz = sx * e1y - sy * e1x
                vv = f * (dx * qx + dy * qy)
                if vv >= 0.0 and u + vv <= 1.0:
                    t = f * (e2x * qx + e2y * qy + e2z * qz)
                    if t >= 0.0 and t <= 1.0:
                        res = 1
        return res

    @ti.kernel
    def nk(f_off: ti.types.ndarray(), f_z: ti.types.ndarray(),
           f_woff: ti.types.ndarray(), f_wflat: ti.types.ndarray(),
           f_walls: ti.types.ndarray(), f_by: ti.types.ndarray(),
           f_bx: ti.types.ndarray(), o_nswe: ti.types.ndarray(),
           west: ti.f64, north: ti.f64, up: ti.f64, down: ti.f64,
           blo: ti.f64, bhi: ti.f64):
        for cell in range(N):
            gx = cell % C
            gy = cell // C
            px = west + gx * 16 + 8
            py = north + gy * 16 + 8
            for k in range(f_off[cell], f_off[cell + 1]):
                z = f_z[k]
                nswe = 15
                wall_bits = 0
                for d in ti.static(range(4)):
                    bit = 8 >> d                        # N=8,S=4,W=2,E=1
                    ngx = gx + (0 if d < 2 else (-1 if d == 2 else 1))
                    ngy = gy + (-1 if d == 0 else (1 if d == 1 else 0))
                    if 0 <= ngx < C and 0 <= ngy < C:
                        ncell = ngy * C + ngx
                        # height_ok: есть ли слой соседа в шаге по высоте
                        hok = 0
                        for kk in range(f_off[ncell], f_off[ncell + 1]):
                            dz = f_z[kk] - z
                            if dz >= -down and dz <= up:
                                hok = 1
                        # заградительное ребро направления d
                        blk = 0
                        if ti.static(d == 0):
                            blk = f_by[cell]
                        elif ti.static(d == 1):
                            blk = f_by[ncell]
                        elif ti.static(d == 2):
                            blk = f_bx[cell]
                        else:
                            blk = f_bx[ncell]
                        if blk == 1:
                            nswe &= ~bit
                            if hok == 1:
                                wall_bits |= bit
                        elif hok == 0:
                            nswe &= ~bit
                        else:
                            # X/Y-луч стены: AND низ(blo)+верх(bhi) по граням
                            # своей ячейки И соседа (seg_hit по обеим)
                            npx = west + ngx * 16 + 8
                            npy = north + ngy * 16 + 8
                            h_lo = 0
                            h_hi = 0
                            for kk in range(f_woff[cell], f_woff[cell + 1]):
                                wi = f_wflat[kk]
                                if h_lo == 0 and seg_hit(
                                        px, py, z + blo, npx, npy,
                                        f_walls[wi, 0], f_walls[wi, 1], f_walls[wi, 2],
                                        f_walls[wi, 3], f_walls[wi, 4], f_walls[wi, 5],
                                        f_walls[wi, 6], f_walls[wi, 7], f_walls[wi, 8]) == 1:
                                    h_lo = 1
                                if h_hi == 0 and seg_hit(
                                        px, py, z + bhi, npx, npy,
                                        f_walls[wi, 0], f_walls[wi, 1], f_walls[wi, 2],
                                        f_walls[wi, 3], f_walls[wi, 4], f_walls[wi, 5],
                                        f_walls[wi, 6], f_walls[wi, 7], f_walls[wi, 8]) == 1:
                                    h_hi = 1
                            for kk in range(f_woff[ncell], f_woff[ncell + 1]):
                                wi = f_wflat[kk]
                                if h_lo == 0 and seg_hit(
                                        px, py, z + blo, npx, npy,
                                        f_walls[wi, 0], f_walls[wi, 1], f_walls[wi, 2],
                                        f_walls[wi, 3], f_walls[wi, 4], f_walls[wi, 5],
                                        f_walls[wi, 6], f_walls[wi, 7], f_walls[wi, 8]) == 1:
                                    h_lo = 1
                                if h_hi == 0 and seg_hit(
                                        px, py, z + bhi, npx, npy,
                                        f_walls[wi, 0], f_walls[wi, 1], f_walls[wi, 2],
                                        f_walls[wi, 3], f_walls[wi, 4], f_walls[wi, 5],
                                        f_walls[wi, 6], f_walls[wi, 7], f_walls[wi, 8]) == 1:
                                    h_hi = 1
                            if h_lo == 1 and h_hi == 1:
                                nswe &= ~bit
                                wall_bits |= bit
                if nswe == 0 and wall_bits != 0:         # anti-глухая
                    nswe = wall_bits
                o_nswe[k] = nswe

    nk(f_off, f_z, f_woff, f_wflat, f_walls, f_by, f_bx, o_nswe,
       float(west), float(north), float(up_step), float(down_step),
       float(body_lo), float(body_hi))
    ti.sync()
    nswe_np = o_nswe.to_numpy()
    # nswe_np[l_off[cell]+j] = nswe j-го слоя ячейки cell (рельеф = слой 0).
    # Порядок слоёв совпадает с ls в build_l2j_ray → доступ O(1) без словарей.
    return nswe_np, l_off32

