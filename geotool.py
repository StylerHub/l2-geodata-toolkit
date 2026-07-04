#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║  L2 Geodata Toolkit — конвертация, просмотр, сравнение, сверка ║
╚══════════════════════════════════════════════════════════════╝

Интерактивное меню:  python3 geotool.py
Подкоманды:
  convert <dir|files...> -o <dir> [-y]         конвертер PTS → L2J
  view   <dir> [--port N]                      браузерный просмотрщик
  diff   <dirA> <dirB> [--region XX_YY]        сравнение двух наборов
  generate <клиент> -o <dir> [--region ...]    генерация из файлов клиента
  check  <dir|files...>                        валидация + заглушки
  verify <dir1> <dir2>                         сверка PTS ↔ L2J (порядок любой)
  l2j2pts <file|dir> -o <dir>                  обратный конвертер L2J → PTS

Спецификации форматов: geolib/formats.py
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from geolib import cmd_check, cmd_convert, cmd_diff, cmd_generate, cmd_l2j2pts, cmd_verify, cmd_view
from geolib.ui import BANNER, bold, cyan, dim


def ask(prompt, default=None):
    s = input(f'  {prompt}' + (f' [{dim(default)}]' if default else '') + ': ').strip().strip("'\"")
    return os.path.expanduser(s) if s else default


def _pick_regions(client_dir):
    """Список квадратов клиента с мультивыбором.

    Ввод: Enter — все; номера и диапазоны (1 4 7-12); имена (22_22);
    можно вперемешку. Помечено ●c — есть Classic-вариант."""
    import re as _re
    maps_dir = os.path.join(client_dir, 'Maps')
    if not os.path.isdir(maps_dir):
        maps_dir = os.path.join(client_dir, 'MAPS')
    if not os.path.isdir(maps_dir):
        return None
    listing = os.listdir(maps_dir)
    mains = sorted(m.group(1) for f in listing
                   for m in [_re.match(r'^(\d+_\d+)\.unr$', f, _re.IGNORECASE)] if m)
    classics = {m.group(1) for f in listing
                for m in [_re.match(r'^(\d+_\d+)_classic\.unr$', f, _re.IGNORECASE)] if m}
    if not mains:
        return None
    print(f'\n  Квадраты клиента ({len(mains)}; ●c — есть Classic-вариант):')
    per_row = 6
    for i in range(0, len(mains), per_row):
        row = ''
        for j, r in enumerate(mains[i:i + per_row], i + 1):
            mark = '●c' if r in classics else '  '
            row += f'{j:4d}) {r}{mark} '
        print('  ' + row)
    raw = input('\n  Выбор (Enter — все, номера/диапазоны/имена): ').strip()
    if not raw:
        return None
    chosen = []
    for tok in raw.replace(',', ' ').split():
        m = _re.match(r'^(\d+)-(\d+)$', tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for k in range(min(a, b), max(a, b) + 1):
                if 1 <= k <= len(mains):
                    chosen.append(mains[k - 1])
        elif _re.match(r'^\d+_\d+$', tok):
            chosen.append(tok)
        elif tok.isdigit() and 1 <= int(tok) <= len(mains):
            chosen.append(mains[int(tok) - 1])
        else:
            print(f'  ⚠ не понял «{tok}» — пропускаю')
    chosen = sorted(set(chosen))
    print(f'  выбрано: {len(chosen)} — {" ".join(chosen[:12])}' +
          (' …' if len(chosen) > 12 else ''))
    return chosen or None


def interactive():
    print(cyan(BANNER))
    last_dir = None  # последний использованный путь — подсказка для следующих промптов
    while True:
      try:
        print(f'''
  {bold('1')}  Конвертер PTS → L2J (XX_YY_conv.dat → XX_YY.l2j)
  {bold('2')}  Просмотрщик в браузере (карта → блок → слои)
  {bold('3')}  Сравнить два набора (отчёт по регионам, детально с картой)
  {bold('4')}  Проверка набора (валидация, поиск заглушек)
  {bold('5')}  Сверка конвертации PTS ↔ L2J (в обе стороны, поячеечно)
  {bold('6')}  Обратный конвертер L2J → PTS (для G3DEditor и др.)
  {bold('7')}  Генерация геодаты из клиента (Maps + Textures + StaticMeshes)
  {bold('0')}  Выход
''')
        ch = input('  выбор: ').strip()
        if ch == '1':
            d = ask('Источник (папка с *_conv.dat или файл)', last_dir)
            if not d:
                continue
            last_dir = d
            out = ask('Куда писать .l2j', os.path.join(os.path.dirname(d) or '.', 'l2j'))
            cmd_convert([d], out)
        elif ch == '2':
            d = ask('Папка с геодатой', last_dir)
            if d:
                last_dir = d
                cmd_view(d)
        elif ch == '3':
            a = ask('Папка A', last_dir)
            b = ask('Папка B')
            if a and b:
                last_dir = a
                r = ask('Регион для детального разбора (Enter — сводный отчёт)', None)
                cmd_diff(a, b, r)
        elif ch == '4':
            d = ask('Папка или файл', last_dir)
            if d:
                last_dir = d
                cmd_check([d])
        elif ch == '5':
            a = ask('Первая папка (PTS или L2J — порядок не важен)', last_dir)
            b = ask('Вторая папка')
            if a and b:
                last_dir = a
                cmd_verify(a, b)
        elif ch == '6':
            src = ask('Файл или папка .l2j', last_dir)
            out = ask('Куда писать *_conv.dat', (src or '.') + '_pts')
            if src:
                last_dir = src
                cmd_l2j2pts([src], out)
        elif ch == '7':
            c = ask('Папка клиента L2 (с Maps/Textures/StaticMeshes)', last_dir)
            if not c:
                continue
            last_dir = c
            out = ask('Куда писать .l2j', os.path.join(c, 'generated_l2j'))
            regions = _pick_regions(c)
            cmd_generate(c, out, regions)
        elif ch == '0' or ch == '':
            return 0
      except EOFError:
        return 0
      except ValueError as e:
        print(f'  некорректный ввод: {e}')


def main():
    ap = argparse.ArgumentParser(description='L2 Geodata Toolkit')
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('convert'); p.add_argument('src', nargs='+'); p.add_argument('-o', '--out', required=True); p.add_argument('-y', '--yes', action='store_true')
    p = sub.add_parser('view'); p.add_argument('dir'); p.add_argument('--port', type=int, default=8777)
    p = sub.add_parser('diff'); p.add_argument('dir_a'); p.add_argument('dir_b'); p.add_argument('--region')
    p = sub.add_parser('generate'); p.add_argument('client'); p.add_argument('-o', '--out', required=True); p.add_argument('--region', nargs='*'); p.add_argument('--terrain-only', action='store_true'); p.add_argument('--max-step', type=int, default=16); p.add_argument('-j', '--jobs', type=int)
    p = sub.add_parser('check'); p.add_argument('paths', nargs='+')
    p = sub.add_parser('verify'); p.add_argument('pts_dir'); p.add_argument('l2j_dir')
    p = sub.add_parser('l2j2pts'); p.add_argument('src', nargs='+'); p.add_argument('-o', '--out', required=True); p.add_argument('-y', '--yes', action='store_true')
    args = ap.parse_args()
    if not args.cmd:
        return interactive()
    print(cyan(BANNER))
    if args.cmd == 'convert':
        return cmd_convert([os.path.expanduser(s) for s in args.src], os.path.expanduser(args.out), args.yes)
    x = os.path.expanduser
    if args.cmd == 'view':
        return cmd_view(x(args.dir), args.port)
    if args.cmd == 'diff':
        return cmd_diff(x(args.dir_a), x(args.dir_b), args.region)
    if args.cmd == 'generate':
        return cmd_generate(x(args.client), x(args.out), args.region,
                            args.max_step, args.terrain_only, args.jobs)
    if args.cmd == 'check':
        return cmd_check([x(pp) for pp in args.paths])
    if args.cmd == 'verify':
        return cmd_verify(x(args.pts_dir), x(args.l2j_dir))
    if args.cmd == 'l2j2pts':
        return cmd_l2j2pts([x(s) for s in args.src], x(args.out), args.yes)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(dim('\n  прервано.'))
        sys.exit(130)
