# The local web server: the page, the API behind it, live events, and the MCP
# endpoint through which the agent's tools reach the project.
# Listens on 127.0.0.1 only; every API call needs the access key, and every
# MCP call the key of the turn it belongs to.

import json
import math
import os
import posixpath
import queue
import re
import secrets
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit

from .accounts import Accounts
from .agent import Agent, default_model
from .builder import Builder, cache_dir
from .config import APP_DIR, PROJECTS_DIR, STATE_DIR, TIERS, same_secret
from .projects import UserError, Workspace, stem_of

MAX_BODY_BYTES = 48 * 1024 * 1024   # a prompt may carry dropped files
MAX_SCAN_BYTES = 400 * 1024 * 1024  # one uploaded mesh
WEB_DIR = os.path.join(APP_DIR, 'web')
VENDOR_DIR = os.path.join(WEB_DIR, 'vendor')
VENDOR = {'preact', 'htm', 'three'}  # the packages the page may load from web/vendor
TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.stl': 'model/stl',
    '.py': 'text/x-python; charset=utf-8',
}
PING_ID = 'cad-editor'  # how a running copy of this app is told from anything else on the port
COOKIE = 'session'  # the page's pass, set once from the key in the address
BEAT_S = 25            # a live-events connection hears from the server at least this often
IDLE_S = 120           # a connection that says nothing for this long is closed
CHUNK = 1 << 16
SMALL_BODY = 1 << 20   # an unread request body up to this size is read off; a larger one ends the connection

_MESH_KEY = re.compile(r'[0-9a-f]{16}')
_MESH_NAME = re.compile(r'[\w.-]+\.stl', re.ASCII)
_TURN_ID = re.compile(r'[0-9a-f-]{36}')
_BAD_ESCAPE = re.compile(r'%(?![0-9A-Fa-f]{2})')


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _js_string(value):
    """What JavaScript's String(value) gives for a value that came from JSON."""
    if isinstance(value, str):
        return value
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return str(value)


def _decode_component(text):
    """decodeURIComponent: raises ValueError for what is not properly encoded UTF-8."""
    if _BAD_ESCAPE.search(text):
        raise ValueError('malformed escape')
    return unquote_to_bytes(text.encode('utf-8', 'surrogateescape')).decode('utf-8')


def _pathname(path):
    """The path as a browser's URL parser normalises it: no `.` or `..` segments."""
    if not path.startswith('/'):
        path = '/' + path
    pieces = path.replace('\\', '/').split('/')[1:]
    kept = []
    for index, piece in enumerate(pieces):
        last = index == len(pieces) - 1
        plain = piece.lower().replace('%2e', '.')
        if plain == '..':
            if kept:
                kept.pop()
            if last:
                kept.append('')
        elif plain == '.':
            if last:
                kept.append('')
        else:
            kept.append(piece)
    return '/' + '/'.join(kept)


def inside(base, relative):
    """Resolve `relative` under `base`, refusing anything that climbs out of it."""
    target = os.path.normpath(os.path.join(base, '.' + posixpath.normpath('/' + relative)))
    return target if target == base or target.startswith(base + os.sep) else None


def run(command, args):
    try:
        return subprocess.call([command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def reveal(file):
    if sys.platform == 'darwin':
        return run('open', ['-R', file])
    if sys.platform == 'win32':
        return run('explorer', [f'/select,{file}'])
    return run('xdg-open', [os.path.dirname(file)])


def open_with_default_app(file):
    if sys.platform == 'darwin':
        return run('open', [file])
    if sys.platform == 'win32':
        return run('cmd', ['/c', 'start', '', file])
    return run('xdg-open', [file])


def copy_file_to_clipboard(file):
    """Puts the file itself on the clipboard, so it pastes into Finder or a slicer."""
    if sys.platform != 'darwin':
        return False
    return run('osascript', ['-e', 'on run argv', '-e', 'set the clipboard to (POSIX file (item 1 of argv))',
                             '-e', 'end run', file])


class _Subscriber:
    """One open live-events connection: what the project emits waits here for its thread."""

    def __init__(self):
        self.queue = queue.SimpleQueue()

    def __call__(self, event):
        self.queue.put(event)

    write = __call__

    def end(self):
        self.queue.put(None)

    close = end


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = IDLE_S

    def log_message(self, format, *args):  # noqa: A002 -- the terminal is for the app's own messages
        pass

    # --- one request -----------------------------------------------------------

    def _handle(self):
        app = self.server.app
        self.sent = False
        self.extra_headers = ()  # headers a route adds to its reply (the cookie)
        self.left = 0          # bytes of the request body not read yet (None: chunked, length unknown)
        self.body_done = True
        encoding = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in encoding:
            self.left, self.body_done = None, False
        else:
            try:
                self.left = max(0, int(self.headers.get('Content-Length') or 0))
            except ValueError:
                self.left = 0
            self.body_done = self.left == 0
        try:
            try:
                app.route(self)
            except UserError as err:
                if self.sent:
                    self.close_connection = True
                else:
                    self.send_json(err.status, {'error': err.message})
            except (ConnectionError, socket.timeout):
                raise
            except Exception:  # noqa: BLE001 -- one bad request must not stop the server
                if self.sent:
                    self.close_connection = True
                else:
                    traceback.print_exc()
                    self.send_json(500, {'error': 'Application error. Details are in the terminal.'})
        except (ConnectionError, socket.timeout):
            self.close_connection = True  # the other side went away
        if not self.body_done:
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = _handle

    # --- reading ---------------------------------------------------------------

    def body_chunks(self):
        """The request body, piece by piece."""
        if self.left is None:  # chunked
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    raise ConnectionResetError('request ended early')
                try:
                    size = int(line.split(b';')[0].strip() or b'0', 16)
                except ValueError:
                    raise UserError('Invalid request data.') from None
                if size == 0:
                    while self.rfile.readline(1024) not in (b'\r\n', b'\n', b''):
                        pass  # trailers
                    break
                while size:
                    data = self.rfile.read(min(size, CHUNK))
                    if not data:
                        raise ConnectionResetError('request ended early')
                    size -= len(data)
                    yield data
                self.rfile.readline(8)
        else:
            while self.left:
                data = self.rfile.read(min(self.left, CHUNK))
                if not data:
                    raise ConnectionResetError('request ended early')
                self.left -= len(data)
                yield data
        self.body_done = True

    def read_json(self):
        chunks = []
        size = 0
        for chunk in self.body_chunks():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                self.close_connection = True
                raise UserError('Too much data in one request.', 413)
            chunks.append(chunk)
        try:
            return json.loads(b''.join(chunks).decode('utf-8', 'replace') or '{}')
        except ValueError:
            raise UserError('Invalid request data.') from None

    def read_object(self):
        """A request's JSON body, for the routes that read fields from it."""
        body = self.read_json()
        return body if isinstance(body, dict) else {}

    def receive_file(self, file, limit):
        """Save the request body to `file` as it arrives, refusing more than `limit` bytes."""
        size = 0
        try:
            with open(file, 'wb') as handle:
                for chunk in self.body_chunks():
                    size += len(chunk)
                    if size > limit:
                        self.close_connection = True
                        raise UserError(f'The file is too large (limit {math.floor(limit / 1024 / 1024 + 0.5)} MB).', 413)
                    handle.write(chunk)
        except BaseException:
            _remove(file)
            raise
        return size

    def _settle_body(self):
        # A reply to a request whose body was not read: read a small one off, so
        # the connection can be used again; otherwise the connection ends here.
        if self.body_done:
            return
        if self.left is None or self.left > SMALL_BODY or self.close_connection:
            self.close_connection = True
            return
        for _ in self.body_chunks():
            pass

    # --- answering -------------------------------------------------------------

    def start_reply(self, status, headers):
        self._settle_body()
        self.send_response_only(status)
        self.send_header('Date', self.date_time_string())
        for name, value in headers.items():
            self.send_header(name, str(value))
        for name, value in getattr(self, 'extra_headers', ()):
            self.send_header(name, value)
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        self.sent = True

    def reply(self, status, headers, payload=b''):
        self.start_reply(status, {**headers, 'Content-Length': len(payload)})
        if payload and self.command != 'HEAD':
            self.wfile.write(payload)
        self.wfile.flush()

    def send_json(self, status, body):
        self.reply(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'},
                   _json(body).encode('utf-8'))

    def send_file(self, file, cache='no-cache', download=None):
        try:
            handle = open(file, 'rb')
        except (OSError, ValueError):
            return self.send_json(404, {'error': 'There is no such file.'})
        with handle:
            try:
                info = os.fstat(handle.fileno())
            except OSError:
                return self.send_json(404, {'error': 'There is no such file.'})
            headers = {
                'Content-Type': TYPES.get(os.path.splitext(file)[1].lower(), 'application/octet-stream'),
                'Content-Length': info.st_size,
                'Cache-Control': cache,
            }
            if download:
                headers['Content-Disposition'] = f'attachment; filename="{download}"'
            self.start_reply(200, headers)
            if self.command == 'HEAD':
                return None
            left = info.st_size
            while left:
                data = handle.read(min(left, CHUNK))
                if not data:
                    # The file shrank under the read: drop the connection, not the server.
                    self.close_connection = True
                    break
                self.wfile.write(data)
                left -= len(data)
            self.wfile.flush()
        return None

    def client_gone(self, wait):
        """Whether the other side has closed the connection; waits up to `wait` seconds to see."""
        try:
            readable, _, _ = select.select([self.connection], [], [], wait)
            if not readable:
                return False
            if self.connection.recv(1, socket.MSG_PEEK) == b'':
                return True
        except (OSError, ValueError):
            return True
        time.sleep(wait)  # it sent something more (a next request): that is not a goodbye
        return False

    def bearer(self):
        header = self.headers.get('Authorization') or ''
        return header[7:] if header.startswith('Bearer ') else None


def _remove(file):
    try:
        os.remove(file)
    except OSError:
        pass


class _App:
    """Everything one running server knows; `route` answers one request."""

    def __init__(self, python, token, accounts):
        self.token = token
        self.accounts = accounts
        self.builder = Builder(python)
        self.workspace = Workspace(PROJECTS_DIR, self.builder)
        self.allowed_hosts = set()
        self.allowed_origins = set()
        self.agent = None

    def authorized(self, req):
        given = req.bearer()
        if given is not None and same_secret(given, self.token):
            return True
        parts = [part.strip() for part in (req.headers.get('Cookie') or '').split(';')]
        cookie = next((part for part in parts if part.startswith(f'{COOKIE}=')), None)
        return same_secret(cookie[len(COOKIE) + 1:], self.token) if cookie else False

    # --- MCP (streamable HTTP, the subset a tool server needs) ---------------

    def mcp_reply(self, message, turn_id):
        def ok(result):
            return {'jsonrpc': '2.0', 'id': message.get('id'), 'result': result}

        params = message.get('params')
        if not isinstance(params, dict):
            params = {}
        method = message.get('method')
        if method == 'initialize':
            version = params.get('protocolVersion')
            return ok({
                'protocolVersion': '2025-06-18' if version is None else version,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'cad', 'version': '1'},
            })
        if method == 'ping':
            return ok({})
        if method == 'tools/list':
            return ok({'tools': self.agent.tools_for(turn_id)})
        if method == 'tools/call':
            outcome = self.agent.tool(turn_id, params.get('name'), params.get('arguments'))
            if outcome.get('finish'):
                return None  # the turn is over: never answered
            content = [{'type': 'text', 'text': outcome.get('text')}]
            image = outcome.get('image')
            if image:
                content.append({'type': 'image', 'data': image.get('data'), 'mimeType': image.get('mimeType')})
            return ok({'content': content, 'isError': outcome.get('isError') is True})
        return {'jsonrpc': '2.0', 'id': message.get('id'), 'error': {'code': -32601, 'message': 'Method not found'}}

    def handle_mcp(self, req, turn_id):
        if req.command != 'POST':
            return req.reply(405, {'Allow': 'POST'})
        body = req.read_json()
        replies = []
        for message in body if isinstance(body, list) else [body]:
            if not isinstance(message, dict) or 'id' not in message:
                continue  # a notification
            reply = self.mcp_reply(message, turn_id)
            if reply is None:
                return self.never_answer(req, turn_id)
            replies.append(reply)
        if not replies:
            return req.reply(202, {})
        return req.send_json(200, replies if isinstance(body, list) else replies[0])

    def never_answer(self, req, turn_id):
        # The call that ended the turn gets no answer: the request is held until
        # the agent's CLI has gone (or the turn is over), then the connection is
        # closed with nothing sent.
        key = req.bearer()
        while self.agent.allows(turn_id, key) and not req.client_gone(0.25):
            pass
        req.close_connection = True
        return None

    # --- API -----------------------------------------------------------------

    @staticmethod
    def done(req, ok, failure):
        return req.send_json(200 if ok else 500, {} if ok else {'error': failure})

    # One model file of a project: /api/projects/<id>/files/<name>/<action>
    def file_route(self, req, project, file, rest, query):
        action = rest[0] if rest else None
        a = rest[1] if len(rest) > 1 else None

        if req.command == 'GET':
            if action == 'version':
                return req.send_json(200, project.version(file, a))
            if action == 'source':
                rev = query.get('rev')
                text = project.source_at(file, rev) if rev else project.source(file)
                return req.reply(200, {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'},
                                 text.encode('utf-8'))
            if action == 'thumb':
                return req.send_file(project.thumb(file=file), cache='private, max-age=31536000, immutable')
            if action == 'download' and a == 'stl':
                return req.send_file(project.export_path(file), cache='no-store', download=f'{stem_of(file)}.stl')
            return req.send_json(404, {'error': 'There is no such operation.'})
        if req.command != 'POST':
            return req.send_json(404, {'error': 'There is no such operation.'})
        body = req.read_object()
        exported = project.export_path(file)

        if action == 'param':
            name = body.get('name')
            project.set_param(file, '' if name is None else _js_string(name), body.get('value'))
            return req.send_json(200, {})
        if action == 'restore':
            rev = body.get('rev')
            project.restore(file, '' if rev is None else _js_string(rev))
            return req.send_json(200, {})
        if action == 'undo':
            project.undo(file)
            return req.send_json(200, {})
        if action == 'redo':
            project.redo(file)
            return req.send_json(200, {})
        if action == 'rename':
            return req.send_json(200, {'file': project.rename_file(file, body.get('to'))})
        if action == 'delete':
            project.delete_file(file)
            return req.send_json(200, {})
        if action == 'reveal':
            target = exported if body.get('what') != 'source' and os.path.exists(exported) else project.file_path(file)
            return self.done(req, reveal(target), 'Could not open the folder.')
        if action == 'open':
            target = project.file_path(file) if body.get('what') == 'source' else exported
            if not os.path.exists(target):
                raise UserError('This file does not exist yet.', 404)
            return self.done(req, open_with_default_app(target), 'Could not open the file.')
        if action == 'copy':
            if not os.path.exists(exported):
                raise UserError('The model has not built yet, so there is no STL file.', 404)
            return req.send_json(200, {'copied': copy_file_to_clipboard(exported), 'path': exported})
        return req.send_json(404, {'error': 'There is no such operation.'})

    # The scans of a project: /api/projects/<id>/scans/<name>[/<action>]
    def scan_route(self, req, project, name, rest):
        action = rest[0] if rest else None

        if req.command == 'PUT' and not action:
            project.busy_check()
            uploads = os.path.join(project.dir, STATE_DIR, 'uploads')
            os.makedirs(uploads, exist_ok=True)
            received = os.path.join(uploads, f'{secrets.token_hex(6)}.part')
            req.receive_file(received, MAX_SCAN_BYTES)
            try:
                return req.send_json(201, {'name': project.add_scan(name, received)})
            finally:
                _remove(received)

        scan = project.scan(name)
        if req.command == 'GET' and not action:
            return req.send_file(project.scan_path(scan), cache='no-cache')
        if req.command == 'GET' and action == 'thumb':
            return req.send_file(project.thumb(scan=scan), cache='private, max-age=31536000, immutable')
        if req.command == 'GET' and action == 'info':
            try:
                info = project.scan_overview(scan)['info']
            except Exception as err:  # noqa: BLE001 -- whatever went wrong, the page gets one sentence
                raise UserError(f'Could not measure the STL. ({err})', 422) from None
            return req.send_json(200, info)
        if req.command != 'POST':
            return req.send_json(404, {'error': 'There is no such operation.'})
        body = req.read_json()
        if action == 'delete':
            project.remove_scan(scan)
            return req.send_json(200, {})
        if action == 'rename':
            return req.send_json(200, {'name': project.rename_scan(scan, body.get('to') if isinstance(body, dict) else None)})
        return req.send_json(404, {'error': 'There is no such operation.'})

    def events(self, req, project):
        """Live events: this thread relays what the project emits until the page goes away."""
        req.start_reply(200, {
            'Content-Type': 'text/event-stream; charset=utf-8',
            'Cache-Control': 'no-store',
            'Connection': 'keep-alive',
            'Transfer-Encoding': 'chunked',
        })
        req.close_connection = True  # nothing else is served on this connection afterwards

        def write(text):
            data = text.encode('utf-8')
            req.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
            req.wfile.flush()

        subscriber = _Subscriber()
        if hasattr(project, 'subscribe'):
            unsubscribe = project.subscribe(subscriber)
        else:
            project.clients.add(subscriber)
            unsubscribe = lambda: project.clients.discard(subscriber)  # noqa: E731
        try:
            write(': connected\n\n')
            beat = time.monotonic() + BEAT_S
            while True:
                try:
                    event = subscriber.queue.get(timeout=max(0.0, min(1.0, beat - time.monotonic())))
                except queue.Empty:
                    if time.monotonic() >= beat:
                        write(': beat\n\n')
                        beat += BEAT_S
                    elif req.client_gone(0):
                        break
                    continue
                if event is None:  # the project ended the stream (it was renamed)
                    req.wfile.write(b'0\r\n\r\n')
                    req.wfile.flush()
                    break
                write(event if isinstance(event, str) else f'data: {_json(event)}\n\n')
        finally:
            if callable(unsubscribe):
                unsubscribe()
        return None

    def project_route(self, req, project, rest, query):
        action = rest[0] if rest else None
        a = rest[1] if len(rest) > 1 else None
        b = rest[2] if len(rest) > 2 else None
        method = req.command

        if method == 'GET' and not action:
            return req.send_json(200, project.snapshot(query.get('file')))

        if method == 'GET' and action == 'events':
            return self.events(req, project)

        if method == 'GET' and action == 'mesh':
            if not _MESH_KEY.fullmatch(a or '') or not _MESH_NAME.fullmatch(b or ''):
                return req.send_json(404, {'error': 'There is no such mesh.'})
            # A build key names its content, so the browser may keep it forever.
            return req.send_file(os.path.join(cache_dir(project.dir, a), b), cache='private, max-age=31536000, immutable')

        if method == 'GET' and action == 'attachments':
            # A file dropped into a prompt, e.g. for the picture's thumbnail in the chat.
            if not _TURN_ID.fullmatch(a or '') or not b:
                return req.send_json(404, {'error': 'There is no such file.'})
            file = inside(os.path.join(project.dir, STATE_DIR, 'attachments', a), b)
            if file:
                return req.send_file(file, cache='private, max-age=31536000, immutable')
            return req.send_json(404, {'error': 'There is no such file.'})

        if action == 'files' and a:
            return self.file_route(req, project, project.model(a), rest[2:], query)
        if action == 'scans' and a:
            return self.scan_route(req, project, a, rest[2:])

        if method != 'POST':
            return req.send_json(404, {'error': 'There is no such operation.'})
        body = req.read_object()

        if action == 'prompt':
            return req.send_json(202, self.agent.start(
                project,
                file=body['file'] if isinstance(body.get('file'), str) else None,
                text=body.get('text'),
                model=body.get('model'),
                files=body.get('files'),
                pick=body.get('pick'),
                tuning=body.get('tuning'),
            ))
        if action == 'stop':
            self.agent.stop(project)
            return req.send_json(200, {})
        if action == 'files':
            # A model can come with its code (brought in), as a copy of another, or empty.
            if isinstance(body.get('source'), str):
                source = body['source']
            elif not isinstance(body.get('copyOf'), str):
                source = None
            elif isinstance(body.get('rev'), str):
                source = project.source_at(project.model(body['copyOf']), body['rev'])  # as it was in that version
            else:
                source = project.source(project.model(body['copyOf']))
            file = project.create_file(name=body.get('name'), source=source,
                                       unique=body.get('unique') is True or source is not None)
            return req.send_json(201, {'file': file})
        if action == 'rename':
            return req.send_json(200, {'id': self.workspace.rename(project.id, body.get('name'))})
        if action == 'duplicate':
            return req.send_json(201, {'id': self.workspace.duplicate(project.id, body.get('name')).id})
        if action == 'delete':
            self.workspace.delete(project.id)
            return req.send_json(200, {})
        return req.send_json(404, {'error': 'There is no such operation.'})

    def api(self, req, pathname, query):
        try:
            segments = [_decode_component(part) for part in pathname.split('/') if part][1:]
        except ValueError:
            raise UserError('Invalid address.') from None
        first = segments[0] if segments else None

        if first == 'ping':
            return req.send_json(200, {'app': PING_ID})
        # The pass is a cookie, and cookies ignore ports: a page served from another
        # port of this machine must not be able to act with it.
        origin = req.headers.get('Origin')
        if origin is not None and origin not in self.allowed_origins:
            return req.send_json(403, {'error': 'Request origin not allowed.'})
        if not self.authorized(req):
            return req.send_json(401, {'error': 'No access. Open the app at the address printed by `./start`.'})

        if first == 'session' and req.command == 'POST':
            req.extra_headers = [('Set-Cookie', f'{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000')]
            return req.send_json(200, {})
        if first == 'state' and req.command == 'GET':
            self.accounts.refresh_if_stale()
            models = self.accounts.models()
            return req.send_json(200, {
                'plan': self.accounts.plan(),
                'providers': self.accounts.public(),
                'tiers': TIERS,
                'models': models,
                'defaultModel': default_model(models) if models else None,
                'root': PROJECTS_DIR,
                'engine': self.builder.info,
                'limits': self.agent.limits,
                'projects': self.workspace.list(),
            })
        if first == 'projects' and len(segments) == 1 and req.command == 'POST':
            body = req.read_object()
            project = self.workspace.create(
                name=body.get('name'),
                files=body['files'] if isinstance(body.get('files'), list) else [],
            )
            return req.send_json(201, {'id': project.id})
        if first == 'projects' and len(segments) > 1 and segments[1]:
            project = self.workspace.open(segments[1])
            return self.project_route(req, project, segments[2:], query)
        return req.send_json(404, {'error': 'There is no such operation.'})

    def route(self, req):
        # Refusing unknown Host values blocks DNS rebinding from other sites.
        if req.headers.get('Host') not in self.allowed_hosts:
            return req.send_json(403, {'error': 'Host not allowed.'})
        # The request line arrives as Latin-1; what the client sent was bytes of UTF-8.
        target = req.path.encode('latin-1', 'replace').decode('utf-8', 'surrogateescape')
        url = urlsplit(target)
        pathname = _pathname(url.path)
        query = {name: values[0] for name, values in parse_qs(url.query, keep_blank_values=True).items()}

        if pathname.startswith('/mcp/'):
            turn_id = pathname[5:]
            if not self.agent.allows(turn_id, req.bearer()):
                return req.send_json(401, {'error': 'unauthorized'})
            return self.handle_mcp(req, turn_id)
        if pathname.startswith('/api/'):
            return self.api(req, pathname, query)
        if req.command != 'GET':
            return req.send_json(405, {'error': 'Method not allowed.'})

        if pathname in ('/', '/index.html'):
            return req.send_file(os.path.join(WEB_DIR, 'index.html'))
        if pathname.startswith('/web/'):
            file = inside(WEB_DIR, pathname[5:])
            return req.send_file(file) if file else req.send_json(404, {'error': 'There is no such file.'})
        if pathname.startswith('/vendor/'):
            # The page's libraries, kept in the repository (web/vendor) under the addresses its import map names.
            name, _, rest = pathname[8:].partition('/')
            file = inside(os.path.join(VENDOR_DIR, name), rest) if name in VENDOR else None
            if file:
                return req.send_file(file, cache='private, max-age=86400')
            return req.send_json(404, {'error': 'There is no such file.'})
        return req.send_json(404, {'error': 'There is no such page.'})


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128
    allow_reuse_address = sys.platform != 'win32'  # on Windows that option would let two servers share the port

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # without the name lookup HTTPServer adds
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, socket.timeout)):
            return  # a browser closing a connection is not news
        traceback.print_exc()


class _Running:
    """A started server: where it listens, and how to stop it."""

    def __init__(self, app, httpd, thread, port):
        self._app = app
        self._httpd = httpd
        self._thread = thread
        self.port = port
        self.base_url = f'http://127.0.0.1:{port}'

    def close(self):
        app = self._app
        for project in list(app.workspace.projects.values()):
            app.agent.stop(project)
        app.builder.stop()
        self._httpd.shutdown()
        self._httpd.server_close()


def start_server(port, python, token, account=None, models=None, accounts=None):
    # `models` gives a fixed set, as tests want; otherwise the signed-in CLIs decide.
    accounts = accounts or Accounts(fixed=models)
    app = _App(python, token, accounts)
    httpd = _HttpServer(('127.0.0.1', port), _Handler)  # OSError (errno EADDRINUSE) when the port is taken
    httpd.app = app
    actual_port = httpd.server_address[1]
    app.allowed_hosts = {f'127.0.0.1:{actual_port}', f'localhost:{actual_port}'}
    app.allowed_origins = {f'http://{host}' for host in app.allowed_hosts}
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True)
    running = _Running(app, httpd, thread, actual_port)
    app.agent = Agent(base_url=running.base_url, models=accounts.models())

    def models_changed():
        app.agent.models = accounts.models()

    accounts.on_change = models_changed
    thread.start()

    # Warm the CAD kernel now so the first build does not wait for the import.
    def warm():
        try:
            app.builder.start()
        except Exception as err:  # noqa: BLE001
            print(str(err), file=sys.stderr)

    threading.Thread(target=warm, daemon=True).start()
    return running
