#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Браузерный просмотрщик: локальный HTTP-сервер + API."""

import glob
import json
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .formats import block_type, parse_region, summarize
from .page import HTML_PAGE
from .render import render_png
from .ui import bold, dim, green, red

CACHE_CAPACITY = 2  # регионов в памяти: текущий и предыдущий


class ViewerState:
    def __init__(self, primary):
        self.primary = primary
        self.cache = {}
        self.meta_lock = threading.Lock()   # защищает cache и locks
        self.locks = {}                     # путь → Lock: парсинг не блокирует чужие регионы

    def files(self):
        # стем имени → путь; 27_24_Classic — отдельный регион от 27_24,
        # координаты фронт берёт из первых двух чисел имени
        out = {}
        for f in sorted(glob.glob(os.path.join(self.primary, '*.l2j')) +
                        glob.glob(os.path.join(self.primary, '*_conv.dat'))):
            base = os.path.basename(f)
            if not re.match(r'^\d+_\d+', base):
                continue
            stem = base[:-len('_conv.dat')] if base.endswith('_conv.dat') else base[:-len('.l2j')]
            out.setdefault(stem, f)
        return out

    def parsed(self, name):
        # имя региона → файл через files(): суффиксные имена тоже находятся
        candidates = [p for p in [self.files().get(name)] if p]
        for p in candidates:
            if os.path.exists(p):
                key = (p, os.path.getmtime(p))
                with self.meta_lock:
                    v = self.cache.get(key)
                    if v is not None:
                        return v
                    lk = self.locks.setdefault(p, threading.Lock())
                # лок на конкретный файл: два запроса не парсят один регион
                # дважды, но парсинг не задерживает запросы к другим регионам
                with lk:
                    with self.meta_lock:
                        v = self.cache.get(key)
                        if v is not None:
                            return v
                    v = parse_region(p)
                    with self.meta_lock:
                        while len(self.cache) >= CACHE_CAPACITY:
                            self.cache.pop(next(iter(self.cache)))
                        self.cache[key] = v
                    return v
        return None


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, separators=(',', ':')).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                self._route(urlparse(self.path).path)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # кривой запрос не должен ронять поток без ответа
                try:
                    self._json({'err': f'{type(e).__name__}: {e}'}, 400)
                except OSError:
                    pass

        def _route(self, path):
            if path == '/':
                body = HTML_PAGE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == '/api/meta':
                self._json({'primary': state.primary})
            elif path == '/api/regions':
                out = []
                for name, f in state.files().items():
                    size = os.path.getsize(f)
                    stub = size in (196608, 393234)
                    out.append({'name': name, 'size': size, 'stub': stub})
                self._json(out)
            elif path.startswith('/api/region/'):
                name = path.split('/')[-1]
                q = parse_qs(urlparse(self.path).query)
                blocks = state.parsed(name)
                if blocks is None:
                    return self._json({'err': 'регион не найден'}, 404)
                li = int(q.get('layer', ['-1'])[0])
                if li >= 0:
                    # срез: N-й сверху слой каждой ячейки; блоки без него → null
                    t, hmax = [], []
                    for blk in blocks:
                        best = None
                        for cell in blk:
                            if len(cell) > li:
                                h = sorted((l[0] for l in cell), reverse=True)[li]
                                if best is None or h > best:
                                    best = h
                        t.append(block_type(blk))
                        hmax.append(best)
                    self._json({'t': t, 'hmax': hmax, 'slice': li})
                else:
                    t, hmin, hmax, lm, smin = summarize(blocks)
                    srt = sorted(smin)
                    self._json({'t': t, 'hmin': hmin, 'hmax': hmax, 'lm': lm,
                                'nf': t.count(0), 'nc': t.count(1), 'nm': t.count(2),
                                'gmin': min(hmin), 'gmax': max(hmax),
                                # 2-й перцентиль: провалы/шахты не выцвечивают палитру
                                'gsmin': srt[len(srt) // 50]})
            elif path.startswith('/api/block/'):
                parts = path.split('/')
                if len(parts) != 6:
                    return self._json({'err': 'ожидается /api/block/<регион>/<bx>/<by>'}, 400)
                _, _, _, name, bx, by = parts
                bx, by = int(bx), int(by)
                if not (0 <= bx <= 255 and 0 <= by <= 255):
                    return self._json({'err': 'координаты блока вне 0..255'}, 400)
                blocks = state.parsed(name)
                if blocks is None:
                    return self._json({'err': 'регион не найден'}, 404)
                blk = blocks[bx * 256 + by]
                self._json({'type': block_type(blk),
                            'cells': [[list(l) for l in cell] for cell in blk]})
            elif path.startswith('/api/render/'):
                name = path.split('/')[-1]
                q = parse_qs(urlparse(self.path).query)
                blocks = state.parsed(name)
                if blocks is None:
                    return self._json({'err': 'регион не найден'}, 404)
                li = int(q.get('layer', ['-1'])[0])
                body = render_png(blocks, li)
                fname = name + ('' if li < 0 else f'_layer{li + 1}') + '.png'
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith('/api/nswe/'):
                name = path.split('/')[-1]
                q = parse_qs(urlparse(self.path).query)
                blocks = state.parsed(name)
                if blocks is None:
                    return self._json({'err': 'регион не найден'}, 404)
                bx0 = max(0, min(255, int(q.get('bx0', ['0'])[0])))
                by0 = max(0, min(255, int(q.get('by0', ['0'])[0])))
                bx1 = max(0, min(255, int(q.get('bx1', ['255'])[0])))
                by1 = max(0, min(255, int(q.get('by1', ['255'])[0])))
                li = int(q.get('layer', ['-1'])[0])
                out = {}
                for bx in range(bx0, bx1 + 1):
                    for by in range(by0, by1 + 1):
                        blk = blocks[bx * 256 + by]
                        vals = []
                        for cell in blk:
                            if li < 0:
                                # поверхность: NSWE самого высокого слоя
                                vals.append(max(cell, key=lambda l: l[0])[1])
                            elif len(cell) > li:
                                vals.append(sorted(cell, key=lambda l: -l[0])[li][1])
                            else:
                                vals.append(None)
                        out[f'{bx}_{by}'] = vals
                self._json({'b': out})
            else:
                self._json({'err': 'not found'}, 404)
    return H


def cmd_view(primary, port=8777):
    state = ViewerState(primary)
    if not state.files():
        print(red(f'  ✗ в {primary} нет файлов геодаты.'))
        return 1
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', port), make_handler(state))
    except OSError as e:
        print(red(f'  ✗ не удалось занять порт {port}: {e}'))
        print(dim('    возможно, просмотрщик уже запущен — укажи другой порт: --port N'))
        return 1
    url = f'http://127.0.0.1:{port}/'
    print(f'  {green("✓")} просмотрщик: {bold(url)}  (Ctrl+C — выход)')
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(dim('\n  остановлено.'))
    return 0
