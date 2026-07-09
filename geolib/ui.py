#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оформление CLI: цвета, баннер, прогресс-бар."""

import os
import re
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


_ANSI = re.compile(r'\033\[[0-9;]*m')


def vlen(s):
    """Видимая длина строки — без ANSI-кодов цвета."""
    return len(_ANSI.sub('', s))


def vtrunc(s, width):
    """Обрезать до `width` ВИДИМЫХ символов, не разрывая ANSI-последователь-
    ности (escape-коды копируются даром и в ширину не считаются)."""
    out, vis, i, n = [], 0, 0, len(s)
    while i < n:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        if vis >= width:
            break
        out.append(s[i])
        vis += 1
        i += 1
    return ''.join(out)


def term_width(default=100):
    try:
        return os.get_terminal_size().columns
    except OSError:                                   # не tty (пайп/редирект)
        return default


def term_height(default=40):
    try:
        return os.get_terminal_size().lines
    except OSError:                                   # не tty (пайп/редирект)
        return default


def status(text):
    """Перерисовать одну строку статуса поверх текущей.

    Обрезаем по реальной ширине терминала (ANSI не считаем и не рвём) и
    стираем хвост прошлого кадра через \\x1b[K — вместо ручного добивания
    строки пробелами, которого никогда не хватает при длинном прошлом кадре.
    Масштабируется на любое число активных строк: что не влезло — отсекается
    по ширине, а не по магической константе."""
    line = vtrunc(text, max(1, term_width() - 1))
    if USE_COLOR:
        sys.stdout.write('\r' + line + '\033[0m\033[K')  # reset цвета + erase-to-EOL
    else:
        sys.stdout.write('\r' + line + ' ' * 12)         # не-tty: старое поведение
    sys.stdout.flush()


# Единый формат бара для ВСЕХ мест (одиночных и в блоке): одна ширина бара,
# одна колонка подписи, один формат процента и (done/total). Меняешь тут — меняется
# везде.
BAR_W = 28                                           # ширина полосы █░
LABEL_W = 13                                          # колонка подписи ('конвертация', 'XX_YY_Classic')


def bar_line(label, done, total, tail=''):
    """Одна строка бара в едином формате:

        <подпись>  ████████░░░░  44.5% (137/308)<tail>

    tail — необязательный хвост (ETA/суффикс), уже со своими отступами.
    Подпись всегда ровно LABEL_W видимых символов (длинные — обрезаются с …),
    доля зажата в [0,1] — бар не переполняется при d>t."""
    frac = min(1.0, max(0.0, done / total)) if total else 1.0
    fill = int(BAR_W * frac)
    bar = '█' * fill + '░' * (BAR_W - fill)
    lbl = str(label).strip()
    lbl = lbl[:LABEL_W - 1] + '…' if len(lbl) > LABEL_W else lbl.ljust(LABEL_W)
    return f'  {lbl} {cyan(bar)} {frac:6.1%} ({done}/{total}){tail}'


class LiveBlock:
    """Живой многострочный блок статуса: шапка + произвольное число строк.

    Каждый кадр перерисовывается НА МЕСТЕ: курсор поднимается на высоту
    прошлого блока и каждая строка переписывается с \\x1b[K. Строк может
    быть сколько угодно (по строке на воркер) — ничего не режем по количеству,
    в отличие от «одной строки», куда всё приходилось впихивать и обрезать.

    Инвариант: после render() курсор стоит в колонке 0 сразу ПОД блоком.
    Отдельная строка всё же подрезается по ширине терминала — иначе её
    перенос сбил бы построчный подсчёт и курсор бы «поехал»."""

    def __init__(self):
        self._prev = 0                               # высота блока прошлого кадра

    def render(self, lines):
        if not USE_COLOR:                            # не-tty: одна строка-шапка
            sys.stdout.write('\r' + vtrunc(lines[0], max(1, term_width() - 1)) + ' ' * 8)
            sys.stdout.flush()
            return
        w = max(1, term_width() - 1)
        buf = []
        if self._prev:
            buf.append(f'\033[{self._prev}A')        # вверх к первой строке блока
        buf.append('\r')
        for ln in lines:
            # \033[0m в конце: если vtrunc отрезал строку внутри cyan-бара,
            # его закрывающий reset потерялся бы и цвет «потёк» бы дальше.
            buf.append('\033[K' + vtrunc(ln, w) + '\033[0m\n')  # стираем строку, пишем, вниз
        extra = self._prev - len(lines)
        for _ in range(extra):                       # блок укоротился — гасим остаток
            buf.append('\033[K\n')
        if extra > 0:
            buf.append(f'\033[{extra}A')             # вернуться ровно под блок
        self._prev = len(lines)
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()

    def stop(self):
        """Курсор уже под блоком — просто забыть его высоту."""
        self._prev = 0


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
    if suffix:
        tail = '  ' + suffix
    elif 1 <= done < total:
        elapsed = time.monotonic() - starts[key]
        tail = '  ~' + fmt_dur(elapsed / done * (total - done)) if elapsed > 0.3 else ''
    else:
        tail = ''
    status(bar_line(prefix, done, total, tail))
    if done == total:
        starts.pop(key, None)
        sys.stdout.write('\n')


progress._starts = {}

