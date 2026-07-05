#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Парсинг UStaticMesh из .usx (UE2 / Lineage 2) и трансформации акторов."""

import math
import os
import struct

from .formats import GeoError
from .unreal import Package, Reader, read_properties


def parse_staticmesh(pkg, e):
    """Экспорт StaticMesh → (verts, tris) — только коллизионные секции.

    Коллизия в L2 задаётся по-материально (Materials[i].EnableCollision);
    секция i меша использует материал i (кроны деревьев — без коллизии)."""
    props = read_properties(pkg, e)
    materials = props.get('Materials') or []
    r = Reader(pkg.data, props['__end__'])
    obj_end = e.offset + e.size
    # UPrimitive: FBox + FSphere
    r.read(25)
    r.read(16)                                    # sphere
    # секции: u32, first_index u16, min_v u16, max_v u16, tri_count u16, tri_max u16
    n_sec = r.ci()
    if not (0 < n_sec < 1000):
        raise GeoError(f'{e.name}: подозрительное число секций {n_sec}')
    sections = []
    for si in range(n_sec):
        _u, first_index, _mn, _mx, tri_count, _tm = struct.unpack_from(
            '<IHHHHH', pkg.data, r.p + 14 * si)
        coll = True
        if si < len(materials):
            coll = bool(materials[si].get('EnableCollision', True))
        sections.append((first_index, tri_count, coll))
    r.read(14 * n_sec)
    # FBox вершинного потока
    r.read(24); r.u8()
    # вершины: pos + normal
    n_v = r.ci()
    if not (0 < n_v < 2_000_000):
        raise GeoError(f'{e.name}: подозрительное число вершин {n_v}')
    verts = []
    d = pkg.data
    p0 = r.p
    for i in range(n_v):
        off = p0 + i * 24
        verts.append(struct.unpack_from('<fff', d, off))
    r.p = p0 + n_v * 24
    r.i32()                                       # revision
    # цветовой и альфа-потоки: TArray<FColor> + rev
    for _ in range(2):
        n = r.ci()
        r.read(4 * n); r.i32()
    # UV-потоки: count, каждый TArray<8b> + f10 + rev
    n_uv = r.ci()
    if not (0 <= n_uv < 16):
        raise GeoError(f'{e.name}: подозрительное число UV-потоков {n_uv}')
    for _ in range(n_uv):
        n = r.ci()
        r.read(8 * n); r.i32(); r.i32()
    # индексный буфер
    n_i = r.ci()
    if not (0 < n_i < 6_000_000) or r.p + n_i * 2 > obj_end:
        raise GeoError(f'{e.name}: подозрительный индексный буфер {n_i}')
    idx = struct.unpack_from(f'<{n_i}H', d, r.p)
    tris = []
    if any(c for _, _, c in sections):
        for first_index, tri_count, coll in sections:
            if not coll:
                continue
            for t in range(tri_count):
                base = first_index + t * 3
                if base + 2 < n_i:
                    tris.append((idx[base], idx[base + 1], idx[base + 2]))
    # хвост (wireframe, коллизия, kDOP) не нужен
    if not tris:
        return [], []
    lo = min(min(v) for v in verts)
    hi = max(max(v) for v in verts)
    if not (-600000 < lo and hi < 600000):
        raise GeoError(f'{e.name}: вершины вне разумного диапазона')
    return verts, tris


_ANG = math.pi / 32768.0


def actor_matrix(rot, scale, scale3d):
    """Матрица поворота+масштаба актора (UE2: сначала Roll(X), Pitch(Y), Yaw(Z))."""
    if rot is None:
        rot = (0, 0, 0)
    pitch, yaw, roll = rot[0] * _ANG, rot[1] * _ANG, rot[2] * _ANG
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    # UE2 FRotationMatrix
    m = [
        [cp * cy, cp * sy, sp],
        [sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp],
        [-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp],
    ]
    s = scale if scale else 1.0
    s3 = scale3d if scale3d else (1.0, 1.0, 1.0)
    return [[m[c][r_] * s * s3[c] for c in range(3)] for r_ in range(3)]


def transform(verts, mtx, loc):
    lx, ly, lz = loc
    out = []
    for x, y, z in verts:
        out.append((
            x * mtx[0][0] + y * mtx[0][1] + z * mtx[0][2] + lx,
            x * mtx[1][0] + y * mtx[1][1] + z * mtx[1][2] + ly,
            x * mtx[2][0] + y * mtx[2][1] + z * mtx[2][2] + lz,
        ))
    return out


class MeshLibrary:
    """Кэш распарсенных мешей по (пакет, имя).

    Сырые USX-пакеты (весь файл в памяти) ограничены LRU-лимитом —
    иначе городской регион раздувает каждый воркер на гигабайты."""

    MAX_PACKAGES = 6

    def __init__(self, usx_dir):
        import collections
        self.usx_dir = usx_dir
        self.packages = collections.OrderedDict()  # LRU: недавние в конце
        self.meshes = {}
        self.failed = {}

    def get(self, pkg_name, mesh_name):
        key = (pkg_name.lower(), mesh_name)
        if key in self.meshes:
            return self.meshes[key]
        if key in self.failed:
            return None
        try:
            low = pkg_name.lower()
            pkg = self.packages.get(low)
            if pkg is not None:
                self.packages.move_to_end(low)     # освежить recency
            if pkg is None:
                path = os.path.join(self.usx_dir, pkg_name + '.usx')
                if not os.path.exists(path):
                    # регистронезависимый поиск
                    low = (pkg_name + '.usx').lower()
                    cand = [f for f in os.listdir(self.usx_dir) if f.lower() == low]
                    if not cand:
                        raise GeoError(f'нет пакета {pkg_name}.usx')
                    path = os.path.join(self.usx_dir, cand[0])
                pkg = Package(path)
                while len(self.packages) >= self.MAX_PACKAGES:
                    self.packages.popitem(last=False)  # выселить самый старый
                self.packages[low] = pkg
            ex = [x for x in pkg.find_exports('StaticMesh') if x.name == mesh_name]
            if not ex:
                raise GeoError(f'{pkg_name}.usx: нет меша {mesh_name}')
            res = parse_staticmesh(pkg, ex[0])
            self.meshes[key] = res
            return res
        except (GeoError, struct.error, IndexError, ValueError) as err:
            self.failed[key] = str(err)
            return None


def region_mesh_triangles(unr_path, usx_dir, progress_cb=None):
    """Все треугольники статик-мешей карты в мировых координатах.

    Возвращает (tris, skipped): tris — список ((x,y,z)×3), skipped — счётчик
    акторов, чьи меши не удалось разобрать."""
    pkg = Package(unr_path)
    lib = MeshLibrary(usx_dir)
    tris_out = []
    skipped = 0
    actors = pkg.find_exports('StaticMeshActor')
    for i, e in enumerate(actors):
        props = read_properties(pkg, e)
        sm = props.get('StaticMesh')
        loc = props.get('Location', (0.0, 0.0, 0.0))
        if not isinstance(sm, int) or sm >= 0:
            skipped += 1
            continue
        # актор без коллизии не участвует (bCollideActors/bBlockActors/bBlockPlayers)
        if (props.get('bCollideActors') is False or props.get('bBlockActors') is False
                or props.get('bBlockPlayers') is False):
            continue
        # цепочка импортов: имя меша и имя usx-пакета
        idx = -sm - 1
        _, cname, pref, mesh_name = pkg.imports[idx]
        pkg_name = None
        for _hop in range(64):                    # защита от циклов в битых пакетах
            if pref >= 0:
                break
            _, cname2, pref, nm = pkg.imports[-pref - 1]
            if cname2 == 'Package':
                pkg_name = nm
        if pkg_name is None:
            skipped += 1
            continue
        mesh = lib.get(pkg_name, mesh_name)
        if mesh is None:
            skipped += 1
            continue
        verts, tris = mesh
        mtx = actor_matrix(props.get('Rotation'), props.get('DrawScale'),
                           props.get('DrawScale3D'))
        pre = props.get('PrePivot')
        if pre:
            verts = [(x - pre[0], y - pre[1], z - pre[2]) for x, y, z in verts]
        w = transform(verts, mtx, loc)
        for a, b, c in tris:
            tris_out.append((w[a], w[b], w[c]))
        if progress_cb and i % 200 == 0:
            progress_cb(i, len(actors))
    return tris_out, skipped


def brush_transform(pkg, props):
    """Трансформация brush-актора: (v−PrePivot)·MainScale → поворот →
    ·PostScale → +Location."""
    from .unreal import parse_scale
    pre = props.get('PrePivot') or (0.0, 0.0, 0.0)
    main = parse_scale(pkg, props.get('MainScale'))
    post = parse_scale(pkg, props.get('PostScale'))
    loc = props.get('Location') or (0.0, 0.0, 0.0)
    mtx = actor_matrix(props.get('Rotation'), None, None)
    def tf(v):
        x = (v[0] - pre[0]) * main[0]
        y = (v[1] - pre[1]) * main[1]
        z = (v[2] - pre[2]) * main[2]
        wx = x * mtx[0][0] + y * mtx[0][1] + z * mtx[0][2]
        wy = x * mtx[1][0] + y * mtx[1][1] + z * mtx[1][2]
        wz = x * mtx[2][0] + y * mtx[2][1] + z * mtx[2][2]
        return (wx * post[0] + loc[0], wy * post[1] + loc[1], wz * post[2] + loc[2])
    return tf


PF_PORTAL = 0x04000000


def level_extra_triangles(pkg):
    """BSP-геометрия карты: (solid_tris, blocking_tris) в мировых координатах.

    solid — CSG-браши уровня и главная BSP-модель (интерьеры);
    blocking — BlockingVolume (невидимые заградительные стены)."""
    from .unreal import parse_polys, model_polys, model_points, read_properties as rp
    solid, blocking = [], []

    # геометрический матчинг Model → Polys для случаев, когда экспорт Polys
    # лежит далеко: сопоставляем по совпадению вершин
    all_polys = []
    for pe in pkg.find_exports('Polys'):
        pl = parse_polys(pkg, pe)
        pts = {tuple(round(c, 1) for c in v) for verts, _f in pl[:3] for v in verts}
        all_polys.append((pe, pl, pts))

    def polys_for(mref):
        pe = model_polys(pkg, mref)
        if pe is not None:
            return parse_polys(pkg, pe)
        try:
            mpts = {tuple(round(c, 1) for c in v)
                    for v in model_points(pkg, pkg.exports[mref - 1])}
        except Exception:
            return []
        best, best_hits = None, 0
        for _pe, pl, pts in all_polys:
            hits = len(mpts & pts)
            if hits > best_hits:
                best, best_hits = pl, hits
        return best if best_hits >= 3 else []
    # BlockingVolume: все грани — заграждения
    for e in pkg.find_exports('BlockingVolume'):
        props = rp(pkg, e)
        mref = props.get('Brush')
        if not isinstance(mref, int) or mref <= 0:
            continue
        tf = brush_transform(pkg, props)
        for verts, flags in polys_for(mref):
            w = [tf(v) for v in verts]
            for i in range(1, len(w) - 1):
                blocking.append((w[0], w[i], w[i + 1]))
    # Главная BSP-модель уровня (post-CSG) — невидимая коллизия интерьеров
    # (полы соборов/залов). Берём все поверхности, кроме порталов и
    # гигантских скайбокс-плоскостей. Крупные water/zone-плоскости тоже
    # попадают сюда, но их отсеивает heightfield-ядро по габариту грани
    # (BIG_PLANE): region-масштабная горизонталь — не пол, а зонная плоскость.
    from .unreal import parse_model
    models = pkg.find_exports('Model')
    if models:
        main = max(models, key=lambda m: m.size)
        for tri, flags in parse_model(pkg, main):
            if flags & PF_PORTAL:
                continue
            (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
            if max(abs(ax - bx), abs(ay - by), abs(bx - cx), abs(by - cy),
                   abs(cx - ax), abs(cy - ay)) > 16384:
                continue                          # скайбокс/задник
            if min(az, bz, cz) >= 16376 or max(az, bz, cz) <= -16376:
                continue                          # плоскости мирового куба
            solid.append(tri)
    return solid, blocking
