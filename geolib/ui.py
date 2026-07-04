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


def progress(done, total, prefix=''):
    w = 36
    frac = done / total if total else 1.0
    bar = '█' * int(w * frac) + '░' * (w - int(w * frac))
    sys.stdout.write(f'\r  {prefix}{cyan(bar)} {frac:6.1%} ({done}/{total})')
    sys.stdout.flush()
    if done == total:
        sys.stdout.write('\n')

