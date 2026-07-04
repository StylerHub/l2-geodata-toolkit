#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оформление CLI: цвета, баннер, прогресс-бар."""

import os
import sys

# Windows: включить обработку ANSI-кодов в conhost (no-op на Win10+ Terminal
# и других ОС) и не падать на символах вне кодировки консоли (cp1251/cp866).
if sys.platform == 'win32':
    os.system('')
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

USE_COLOR = sys.stdout.isatty() and os.environ.get('NO_COLOR') is None


def c(code, t):
    return f'\033[{code}m{t}\033[0m' if USE_COLOR else str(t)


def bold(t):   return c('1', t)
def dim(t):    return c('2', t)
def red(t):    return c('31', t)
def green(t):  return c('32', t)
def yellow(t): return c('33', t)
def cyan(t):   return c('36', t)


BANNER = '''
    ██████╗ ███████╗ ██████╗ ████████╗ ██████╗  ██████╗ ██╗     
   ██╔════╝ ██╔════╝██╔═══██╗╚══██╔══╝██╔═══██╗██╔═══██╗██║     
   ██║  ███╗█████╗  ██║   ██║   ██║   ██║   ██║██║   ██║██║     
   ██║   ██║██╔══╝  ██║   ██║   ██║   ██║   ██║██║   ██║██║     
   ╚██████╔╝███████╗╚██████╔╝   ██║   ╚██████╔╝╚██████╔╝███████╗
    ╚═════╝ ╚══════╝ ╚═════╝    ╚═╝    ╚═════╝  ╚═════╝ ╚══════╝

                         L2 Geodata Toolkit                     
       конвертация · просмотр · сравнение · проверка · сверка   

'''


def fmt_dur(sec):
    """Секунды → компактная длительность (2м 05с / 1ч 12м)."""
    sec = int(max(0, sec))
    if sec < 60:
        return f'{sec}с'
    if sec < 3600:
        return f'{sec // 60}м {sec % 60:02d}с'
    return f'{sec // 3600}ч {(sec % 3600) // 60:02d}м'


def progress(done, total, prefix='', suffix=''):
    """Прогресс-бар с авто-оценкой остатка времени (ETA).

    Время старта засекается на первом тике (по ключу prefix); ETA =
    среднее-на-единицу × остаток. suffix, если задан, вытесняет ETA."""
    import time
    key = prefix or '_'
    starts = progress._starts
    if done <= 1:                                # начало цикла — новый отсчёт
        starts[key] = time.monotonic()
    elif key not in starts:
        starts[key] = time.monotonic()
    w = 36
    frac = done / total if total else 1.0
    bar = '█' * int(w * frac) + '░' * (w - int(w * frac))
    if suffix:
        tail = '  ' + suffix
    elif 1 <= done < total:
        elapsed = time.monotonic() - starts[key]
        tail = '  ~' + fmt_dur(elapsed / done * (total - done)) if elapsed > 0.3 else ''
    else:
        tail = ''
    line = f'\r  {prefix}{cyan(bar)} {frac:6.1%} ({done}/{total}){tail}'
    sys.stdout.write(line + ' ' * 12)            # затираем хвост прошлой строки (ETA до ~9 колонок)
    sys.stdout.flush()
    if done == total:
        starts.pop(key, None)
        sys.stdout.write('\n')


progress._starts = {}

