# The build worker: one warm Python process, asked to build model sources.
# Results are cached on disk by content, so a version seen before costs nothing.

import hashlib
import json
import math
import os
import secrets
import shutil
import subprocess
import tempfile
import threading

from .config import APP_DIR, BUILD_TIMEOUT_S, ENGINE_VERSION, MESH, MODELS_DIR, SCANS_DIR, STATE_DIR

WORKER = os.path.join(APP_DIR, 'py', 'worker.py')
CACHE_KEEP = 80  # builds kept per project
SCAN_TIMEOUT_S = 120
PICTURE_SIZE = 640  # pixels a side of a picture made for the agent
THUMB_SIZE = 144    # and of the small one shown beside a name in the list
IGNORED = {STATE_DIR, '.git', '.gitignore', '__pycache__', '.DS_Store'}


# The variables the worker's environment keeps (and every LC_*).
WORKER_ENV = {'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TMPDIR', 'TEMP', 'TMP', 'TZ', 'SYSTEMROOT', 'SYSTEMDRIVE',
              'WINDIR', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'PATHEXT', 'COMSPEC',
              'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'APP_NO_FORK'}
SANDBOX_EXEC = '/usr/bin/sandbox-exec'
_sandbox_prefixes = {}  # (python, projects folder) -> what goes before the worker's command


def _sandbox_profile(python, projects):
    """
    The rules macOS holds the worker (and so every model) to: no network; writing
    only inside the projects folder and the temporary folders; of the user's home,
    reading only the projects, the app and Python itself.
    """
    def quoted(path):
        return '"' + os.path.realpath(path).replace('\\', '\\\\').replace('"', '\\"') + '"'

    def subpaths(paths):
        return ' '.join(f'(subpath {quoted(path)})' for path in dict.fromkeys(os.path.realpath(path) for path in paths if path))

    home = os.path.expanduser('~')
    prefixes = subprocess.run([python, '-c', 'import sys; print(sys.prefix); print(sys.base_prefix)'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.split('\n')
    temporary = [tempfile.gettempdir(), '/private/tmp', '/private/var/folders']
    return '\n'.join([
        '(version 1)',
        '(allow default)',
        '(deny network*)',
        '(deny file-write*)',
        f'(allow file-write* {subpaths([projects, *temporary])} (regex #"^/dev/"))',
        f'(deny file-read-data (subpath {quoted(home)}))',
        f'(allow file-read-data {subpaths([projects, APP_DIR, *prefixes, os.path.join(home, "Library", "Fonts"), *temporary])})',
    ])


def _sandbox(python, projects):
    """What to put before the worker's command so the system fences it in; nothing where it cannot."""
    key = (python, projects)
    if key not in _sandbox_prefixes:
        _sandbox_prefixes[key] = []
        if os.path.exists(SANDBOX_EXEC) and not os.environ.get('APP_NO_SANDBOX'):
            try:
                prefix = [SANDBOX_EXEC, '-p', _sandbox_profile(python, projects)]
                # Tried once on something harmless: a system that refuses the rules runs without them.
                if subprocess.run([*prefix, '/usr/bin/true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    _sandbox_prefixes[key] = prefix
            except OSError:
                pass
    return _sandbox_prefixes[key]


class _Waiter:
    """What one thread waits on: a reply, or the error that ended the wait."""

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None

    def resolve(self, value):
        self.value = value
        self.done.set()

    def reject(self, error):
        self.error = error
        self.done.set()

    def wait(self):
        self.done.wait()
        if self.error is not None:
            raise self.error
        return self.value


def _num(value):
    """A number as JavaScript writes it in a string (0.02, 6; never 6.0)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _mtime_ms(info):
    """Math.round of a file's modification time in milliseconds."""
    return math.floor(info.st_mtime_ns / 1e6 + 0.5)


def _sha1(*texts):
    digest = hashlib.sha1()
    for text in texts:
        digest.update(text.encode('utf-8'))
    return digest.hexdigest()[:16]


def _remove(target):
    """Delete a file or a folder with everything in it; no complaint when it is not there."""
    try:
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            os.remove(target)
    except OSError:
        pass


class Builder:
    """The worker that builds models, for the projects of one folder: the system lets it write nowhere else."""

    def __init__(self, python, projects):
        self.python = python
        self.projects = projects
        self.child = None
        self.starting = None  # the wait for the worker being warm
        self.pending = {}     # request id -> waiter
        self.next_id = 1
        self.info = None      # { build123d, python, scan } once the worker is warm; scan: it can measure meshes
        self.in_flight = {}   # cache dir -> waiter, so one source is built once
        self.lock = threading.RLock()   # guards everything above
        self.writing = threading.Lock()  # one request line at a time on the worker's stdin

    def start(self):
        """Start the worker and wait until build123d is imported."""
        with self.lock:
            starting = self.starting
            if starting is None:
                starting = self.starting = _Waiter()
                self._spawn(starting)
        return starting.wait()

    def _spawn(self, starting):
        # Models run in the worker, so it gets only what it needs of the environment:
        # nothing a key or a token could be sitting in. It replies on its stdout (it keeps
        # that channel to itself: see serve() in py/worker.py).
        env = {name: value for name, value in os.environ.items() if name.upper() in WORKER_ENV or name.upper().startswith('LC_')}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
        try:
            child = subprocess.Popen([*_sandbox(self.python, self.projects), self.python, WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env)
        except OSError as err:
            self.starting = None
            starting.reject(RuntimeError(f'The CAD engine stopped working ({err}).'))
            return
        self.child = child
        stderr = ['']

        def read_errors():
            try:
                for chunk in iter(lambda: child.stderr.read1(4096), b''):
                    stderr[0] = (stderr[0] + chunk.decode('utf-8', 'replace'))[-2000:]
            except (OSError, ValueError):
                pass

        def gone(reason):
            with self.lock:
                if self.child is not child:
                    return
                self.child = None
                self.starting = None
                last = stderr[0].strip().split('\n')[-1]
                error = RuntimeError(f'The CAD engine stopped working ({reason}). {last}'.strip())
                waiters = list(self.pending.values())
                self.pending.clear()
            for waiter in waiters:
                waiter.reject(error)
            starting.reject(error)

        def read_replies():
            try:
                for raw in child.stdout:
                    line = raw.decode('utf-8', 'replace')
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get('ready'):
                        self.info = {'build123d': message.get('build123d'), 'python': message.get('python'),
                                     'scan': message.get('scan') is True}
                        starting.resolve(self.info)
                        continue
                    with self.lock:
                        waiter = self.pending.pop(message.get('id'), None)
                    if waiter:
                        waiter.resolve(message)
            except (OSError, ValueError):
                pass
            code = child.wait()
            errors.join(2)  # so the message can quote the worker's last words
            gone(f"code {'null' if code < 0 else code}")

        errors = threading.Thread(target=read_errors, daemon=True)
        errors.start()
        threading.Thread(target=read_replies, daemon=True).start()

    def stop(self):
        with self.lock:
            child = self.child
            starting = self.starting
            self.child = None
            self.starting = None
            waiters = list(self.pending.values())
            self.pending.clear()
        if child:
            try:
                child.terminate()
            except OSError:
                pass
        # Whoever still waits for the worker is told at once, not when their time runs out.
        error = RuntimeError('The CAD engine stopped working (stopped).')
        for waiter in [*waiters, *([starting] if starting else [])]:
            waiter.reject(error)

    def request(self, payload):
        self.start()
        waiter = _Waiter()
        with self.lock:
            child = self.child
            if child is None:
                raise RuntimeError('The CAD engine stopped working (stopped).')
            request_id = self.next_id
            self.next_id += 1
            self.pending[request_id] = waiter
        # Plain ASCII on the wire: the worker reads its stdin in the system's
        # encoding, which is not UTF-8 everywhere.
        line = json.dumps({'id': request_id, **payload}, separators=(',', ':')) + '\n'
        try:
            with self.writing:
                child.stdin.write(line.encode('ascii'))
                child.stdin.flush()
        except (OSError, ValueError):
            pass  # the worker is going away; its end rejects the waiter
        return waiter.wait()

    def face_at(self, dir, file, source, name, point):
        """A plain description of the model face nearest `point` on part `name`, or None."""
        reply = self.request({'op': 'face_at', 'source': source, 'path': os.path.join(dir, MODELS_DIR, file),
                              'cwd': dir, 'name': name, 'point': point, 'timeout': BUILD_TIMEOUT_S})
        return reply.get('text') if reply.get('ok') else None

    def picture(self, dir, subject, camera):
        """
        A picture, as PNG bytes with one line saying what it shows. `subject` is
        `{ scan }` (a file under scans/) or `{ file, source, scans? }` (a model as
        that source builds); `camera` is `{ view }` or `{ direction }`.
        Returns `{ png, text }`.
        """
        out = os.path.join(dir, STATE_DIR, 'cache', f'view-{secrets.token_hex(6)}.png')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if subject.get('scan'):
            request = {'op': 'scan', 'what': 'view', 'path': os.path.join(dir, SCANS_DIR, subject['scan']),
                       'name': subject['scan'], 'timeout': SCAN_TIMEOUT_S}
        else:
            scans = subject.get('scans')
            request = {'op': 'view', 'source': subject.get('source'),
                       'path': os.path.join(dir, MODELS_DIR, subject['file']), 'cwd': dir,
                       'scans': [] if scans is None else scans, 'timeout': BUILD_TIMEOUT_S}
        try:
            reply = self.request({**request, **camera, 'size': PICTURE_SIZE, 'out': out})
            if not reply.get('ok'):
                raise RuntimeError(_message(reply, 'no picture'))
            with open(out, 'rb') as handle:
                png = handle.read()
            text = reply.get('text')
            return {'png': png, 'text': '' if text is None else text}
        finally:
            _remove(out)

    def thumbnail(self, dir, subject, out):
        """A small picture of a scan or a model (`subject` as for `picture`), written to the file `out`."""
        if subject.get('scan'):
            request = {'op': 'scan', 'what': 'view', 'path': os.path.join(dir, SCANS_DIR, subject['scan']),
                       'name': subject['scan'], 'timeout': SCAN_TIMEOUT_S}
        else:
            request = {'op': 'view', 'source': subject.get('source'),
                       'path': os.path.join(dir, MODELS_DIR, subject['file']), 'cwd': dir,
                       'scans': [], 'timeout': BUILD_TIMEOUT_S}
        fresh = f'{out}.{secrets.token_hex(4)}'
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            reply = self.request({**request, 'view': 'iso', 'size': THUMB_SIZE, 'clear': True, 'out': fresh})
            if not reply.get('ok'):
                raise RuntimeError(_message(reply, 'no picture'))
            os.replace(fresh, out)
        finally:
            _remove(fresh)

    def params(self, source):
        """The editable numbers of a source, without running it."""
        reply = self.request({'op': 'params', 'source': source})
        params = reply.get('params')
        return [] if params is None else params

    def scan_overview(self, dir, name):
        """
        What a scan is: size, flat faces, round holes and bosses, a few sections.
        Returns `{ info, text, views }`: the numbers, the same as text for the
        agent, and the path of a picture of it from four sides (or None).
        """
        out = scan_cache(dir, name)

        def job():
            file = os.path.join(out, 'overview.json')
            views = os.path.join(out, 'views.png')
            saved = None
            try:
                with open(file, 'r', encoding='utf-8') as handle:
                    saved = json.load(handle)
            except (OSError, ValueError):
                pass  # not measured yet
            if not saved:
                reply = self.request({'op': 'scan', 'what': 'overview', 'path': os.path.join(dir, SCANS_DIR, name),
                                      'name': name, 'out': out, 'timeout': SCAN_TIMEOUT_S})
                if not reply.get('ok'):
                    raise RuntimeError(_message(reply, 'scan failed'))
                saved = {'info': reply.get('info'), 'text': reply.get('text'), 'views': reply.get('views') is True}
                os.makedirs(out, exist_ok=True)
                _write_json(file, saved)
            else:
                touch(out)
            return {'info': saved.get('info'), 'text': saved.get('text'), 'views': views if saved.get('views') else None}

        return self.once(out, job)

    def scan_measure(self, dir, name, query):
        """
        One measurement of a scan, as text for the agent. `query` is
        `{ what: 'section', axis | normal, at }` (the outline where a plane cuts
        it) or `{ what: 'probe', point }` (what the surface is like at a point).
        """
        reply = self.request({'op': 'scan', **query, 'path': os.path.join(dir, SCANS_DIR, name), 'name': name,
                              'timeout': SCAN_TIMEOUT_S})
        if not reply.get('ok'):
            raise RuntimeError(_message(reply, 'scan failed'))
        return reply.get('text')

    def build(self, dir, file, source):
        """
        Build `source` as the model file `file` of the project in `dir`. Returns
        the build record (`meta`); its meshes are in the cache directory named by
        `meta.key`. A file with no code in it is not an error, just empty.
        """
        if not source.strip():
            return {'ok': False, 'empty': True, 'key': None, 'params': [], 'ms': {'total': 0}}
        key = _sha1(f"{_num(ENGINE_VERSION)}|{_num(MESH['tol'])}|{_num(MESH['ang'])}|{extras_stamp(dir, file)}|", source)
        out = cache_dir(dir, key)

        def job():
            meta_file = os.path.join(out, 'meta.json')
            try:
                with open(meta_file, 'r', encoding='utf-8') as handle:
                    cached = json.load(handle)
                touch(out)
                return {**cached, 'cached': True}
            except (OSError, ValueError, TypeError):
                pass  # not built yet

            reply = self.request({
                'op': 'build',
                'source': source,
                'path': os.path.join(dir, MODELS_DIR, file),
                'cwd': dir,
                'out': out,
                'tol': MESH['tol'],
                'ang': MESH['ang'],
                'timeout': BUILD_TIMEOUT_S,
            })
            meta = {name: value for name, value in reply.items() if name != 'id'}
            meta['key'] = key
            os.makedirs(out, exist_ok=True)
            _write_json(meta_file, meta)
            threading.Thread(target=_prune_quietly, args=(dir,), daemon=True).start()
            return meta

        return self.once(out, job)

    def once(self, out, job):
        """Run `job` for the cache folder `out`, unless it is already running: then share it."""
        with self.lock:
            running = self.in_flight.get(out)
            mine = running is None
            if mine:
                running = self.in_flight[out] = _Waiter()
        if mine:
            try:
                value = job()
            except BaseException as err:
                with self.lock:
                    self.in_flight.pop(out, None)
                running.reject(err)
                raise
            with self.lock:
                self.in_flight.pop(out, None)
            running.resolve(value)
            return value
        return running.wait()


def _message(reply, fallback):
    error = reply.get('error')
    message = error.get('message') if isinstance(error, dict) else None
    return fallback if message is None else message


def _write_json(file, value):
    with open(file, 'w', encoding='utf-8', newline='') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def touch(out):
    """Mark a cache folder as just used, so pruning keeps it."""
    try:
        os.utime(out, None)
    except OSError:
        pass


def scan_cache(dir, name):
    """Where what is known about one scan is kept; a changed file gets a new place."""
    info = os.stat(os.path.join(dir, SCANS_DIR, name))
    key = _sha1(f'scan|{_num(ENGINE_VERSION)}|{name}|{info.st_size}|{_mtime_ms(info)}')
    return os.path.join(dir, STATE_DIR, 'cache', f'scan-{key}')


def cache_dir(dir, key):
    return os.path.join(dir, STATE_DIR, 'cache', key)


def stamps(dir, prefix, skip):
    try:
        with os.scandir(dir) as found:
            names = sorted(entry.name for entry in found if entry.is_file(follow_symlinks=False) and not skip(entry.name))
    except OSError:
        return []
    result = []
    for name in names:
        info = os.stat(os.path.join(dir, name))
        result.append(f'{prefix}{name}:{info.st_size}:{_mtime_ms(info)}')
    return result


# A model may import the models next to it, read data files and compare itself
# with a scan; their names, sizes and times are part of the cache key, so
# changing one invalidates the build.
def extras_stamp(dir, file):
    beside = stamps(os.path.join(dir, MODELS_DIR), f'{MODELS_DIR}/', lambda name: name == file or name in IGNORED)
    scans = stamps(os.path.join(dir, SCANS_DIR), f'{SCANS_DIR}/', lambda name: False)
    return ','.join(beside + scans)


def prune(dir):
    root = os.path.join(dir, STATE_DIR, 'cache')
    names = os.listdir(root)
    if len(names) <= CACHE_KEEP:
        return
    dated = [(os.stat(os.path.join(root, name)).st_mtime_ns, name) for name in names]
    dated.sort(key=lambda entry: entry[0], reverse=True)
    for _, name in dated[CACHE_KEEP:]:
        _remove(os.path.join(root, name))


def _prune_quietly(dir):
    try:
        prune(dir)
    except OSError:
        pass
