# Projects: one folder each under a common root, each its own git repository.
#
#   <root>/<project>/src/<name>.py     a model; a project holds any number, or none yet
#   <root>/<project>/<name>.stl        that model's latest successful export (not tracked)
#   <root>/<project>/scans/<name>.stl  meshes uploaded as read-only references
#   <root>/<project>/notes/<scan>.md   what was learned about a scan
#   <root>/<project>/.sys/                  journal, build cache, attachments (not tracked)
#
# Every model has its own history: a commit touches one file. HEAD of every
# model builds, because a change that breaks a model is never committed.

import hashlib
import json
import math
import os
import random
import re
import shutil
import struct
import threading
import time
import unicodedata
from decimal import ROUND_HALF_UP, Context, Decimal

from . import git
from .builder import cache_dir, scan_cache
from .config import DEFAULT_FILE, MERGED_STL, MODELS_DIR, NOTES_DIR, SCANS_DIR, STATE_DIR, TRASH_DIR
from .report import _js_number, report_for

PROJECT_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
NOTE_LIMIT = 400  # characters of one finding about a scan
NOTES_KEPT = 40   # findings kept per scan, the newest
MODEL_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,47}\.py')
SCAN_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}\.stl')
JOURNAL_LIMIT = 300
# Journal entries about the project as a whole; every other entry is about one model.
PROJECT_KINDS = {'scan.add', 'scan.remove', 'duplicate'}
# Not copied when a project is duplicated: both can be made again.
NOT_DUPLICATED = {'cache', 'attachments', 'log.jsonl', 'log.1.jsonl', 'uploads'}

IGNORE_LINES = ['/*.stl', '/*.step', f'{STATE_DIR}/', '__pycache__/', '*.pyc', '.DS_Store']
OLD_STATE_DIRS = ('.promptorys', '.bryla')
OLD_IGNORE_LINES = {'model.stl', 'model.step', '/model.stl', '/model.step', '.bryla/', '.promptorys/'}  # written by earlier versions

_BASE36 = '0123456789abcdefghijklmnopqrstuvwxyz'
_SPACES = re.compile(r'\s+')


class UserError(Exception):
    """An error whose message is written for the person using the app."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- small helpers ------------------------------------------------------------

def _now():
    return int(time.time() * 1000)


def _base36(number):
    text = ''
    while number:
        number, digit = divmod(number, 36)
        text = _BASE36[digit] + text
    return text or '0'


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


# Text files are UTF-8 and their line ends are left exactly as they are.
def _read_text(file):
    with open(file, 'r', encoding='utf-8', errors='replace', newline='') as handle:
        return handle.read()


def _write_text(file, text):
    with open(file, 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)


def _remove(file):
    try:
        os.remove(file)
    except FileNotFoundError:
        pass


def _ascii(name):
    text = unicodedata.normalize('NFKD', '' if name is None else str(name))
    return re.sub('[̀-ͯ]', '', text).replace('ł', 'l').replace('Ł', 'L')


def slug(name):
    return re.sub(r'^-+|-+$', '', re.sub(r'[^a-z0-9]+', '-', _ascii(name).lower()))[:48]


def model_stem(name):
    """ "Cable clip.py" -> "Cable_clip": a name Python can also import. """
    text = re.sub(r'\.py\Z', '', _ascii(name), flags=re.I)
    return re.sub(r'^[_-]+|[_-]+\Z', '', re.sub(r'[^A-Za-z0-9_-]+', '_', text))[:44]


def scan_stem(name):
    text = re.sub(r'\.stl\Z', '', _ascii(name), flags=re.I)
    return re.sub(r'^[_.-]+|[_.-]+\Z', '', re.sub(r'[^A-Za-z0-9_.-]+', '_', text))[:52]


def stem_of(file):
    return re.sub(r'\.[^.]+\Z', '', file)


# "2026-11-14 22:23", local time: a form with no words, so the page can show it in its own language.
def _clock(when):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(when / 1000))


def _is_whole(value):
    return isinstance(value, int) or float(value).is_integer()


# "8" for a parameter written as an integer, "8.0" for one written as a float.
def _number_text(value, raw):
    whole = _is_whole(value)
    if whole and re.fullmatch(r'[-+]?[0-9]+', raw):
        return _js_number(value)
    if whole:
        return f'{int(value)}.0' if abs(value) < 1e21 else _js_number(value)
    # Twelve significant digits hide the noise of binary fractions (0.1 + 0.2).
    return _js_number(float(Context(prec=12, rounding=ROUND_HALF_UP).create_decimal(Decimal(value))))


_PUNCTUATION = {'_': 0, '-': 1, '.': 2}


# The order the page's language gives names: punctuation, then numbers by
# value ("part2" before "part10"), then letters without regard to case.
def _name_order(name):
    primary = []
    for run in re.findall(r'[0-9]+|[^0-9]', name):
        if run[0].isdigit():
            primary.append((1, int(run), ''))
        elif run in _PUNCTUATION:
            primary.append((0, _PUNCTUATION[run], ''))
        else:
            primary.append((2, 0, run.lower()))
    return (primary, [ch.isupper() for ch in name if ch.isalpha()])


def _listed(dir, pattern):
    found = []
    try:
        with os.scandir(dir) as entries:
            names = [entry.name for entry in entries
                     if entry.is_file(follow_symlinks=False) and pattern.fullmatch(entry.name)]
    except OSError:
        return []
    for name in names:
        info = os.stat(os.path.join(dir, name))
        found.append({'name': name, 'size': info.st_size, 'updatedAt': info.st_mtime_ns / 1e6})
    return sorted(found, key=lambda file: _name_order(file['name']))


def _models_in(dir):
    """The models of the project in `dir`; before it is first opened by this version they may still lie beside `src/`."""
    kept = _listed(os.path.join(dir, MODELS_DIR), MODEL_NAME)
    loose = _listed(dir, MODEL_NAME)
    names = {file['name'] for file in kept}
    return kept + [file for file in loose if file['name'] not in names]


# A folder is a project when it has a history, the app's state, or a model.
def _is_project(dir, models):
    return len(models) > 0 or os.path.exists(os.path.join(dir, '.git')) or os.path.exists(os.path.join(dir, STATE_DIR))


def _copy_tree(from_, to, skip):
    os.makedirs(to, exist_ok=True)
    with os.scandir(from_) as entries:
        listing = list(entries)
    for entry in listing:
        source = os.path.join(from_, entry.name)
        if skip(source):
            continue
        target = os.path.join(to, entry.name)
        if entry.is_dir(follow_symlinks=False):
            _copy_tree(source, target, skip)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(source, target)


def _looks_like_stl(file):
    """Whether the file starts like an STL, binary or text."""
    size = os.stat(file).st_size
    with open(file, 'rb') as handle:
        head = handle.read(84)
    if len(head) >= 84 and size == 84 + 50 * struct.unpack_from('<I', head, 80)[0] and size > 84:
        return True
    return re.match(r'\s*solid\b', head.decode('latin-1'), re.ASCII) is not None and size > 20


class Workspace:
    def __init__(self, root, builder):
        self.root = root
        self.builder = builder
        self.projects = {}
        self._lock = threading.Lock()  # guards `projects`

    def list(self):
        os.makedirs(self.root, exist_ok=True)
        with os.scandir(self.root) as entries:
            folders = [entry.name for entry in entries
                       if entry.is_dir(follow_symlinks=False) and PROJECT_ID.fullmatch(entry.name)]
        projects = []
        for name in folders:
            dir = os.path.join(self.root, name)
            try:
                models = _models_in(dir)
                scans = _listed(os.path.join(dir, SCANS_DIR), SCAN_NAME)
                info = os.stat(dir)
            except FileNotFoundError:
                continue  # renamed or removed while listing
            if not _is_project(dir, models):
                continue
            projects.append({
                'id': name,
                'models': len(models),
                'scans': len(scans),
                'updatedAt': max([info.st_mtime_ns / 1e6] + [file['updatedAt'] for file in models + scans]),
            })
        return sorted(projects, key=lambda project: -project['updatedAt'])

    def open(self, id):
        if not PROJECT_ID.fullmatch(str(id)):
            raise UserError('There is no such project.', 404)
        project = self.projects.get(id)
        if not project:
            dir = os.path.join(self.root, id)
            if not os.path.exists(dir) or not _is_project(dir, _models_in(dir)):
                raise UserError('There is no such project.', 404)
            # Another request may have opened it while this one was looking.
            with self._lock:
                project = self.projects.get(id)
                mine = project is None
                if mine:
                    project = Project(self.builder, id, dir)
                    self.projects[id] = project
            if mine:
                try:
                    project.load()
                except BaseException as err:
                    # A project that failed to load is tried again by the next request.
                    project.load_error = err
                    with self._lock:
                        if self.projects.get(id) is project:
                            del self.projects[id]
                    raise
                finally:
                    project.ready.set()
        project.ready.wait()
        if project.load_error is not None:
            raise project.load_error
        return project

    def free_id(self, name):
        base = slug(name)
        if not base:
            raise UserError('Give the project a name (letters or digits).')
        os.makedirs(self.root, exist_ok=True)
        id = base
        n = 2
        while os.path.exists(os.path.join(self.root, id)):
            id = f'{base}-{n}'
            n += 1
        return id

    def create(self, name=None, files=()):
        """A new project: empty, or with model files brought in (`files`: [{ name, source }])."""
        id = self.free_id(name)
        dir = os.path.join(self.root, id)
        os.makedirs(os.path.join(dir, STATE_DIR), exist_ok=True)
        git.init(dir)
        git.commit_all(dir, 'New project', trailers={'Kind': 'project'}, empty=True)
        project = self.open(id)
        for file in files or ():
            if isinstance(file, dict) and isinstance(file.get('source'), str):
                project.create_file(name=file.get('name'), source=file['source'], unique=True)
        return project

    def rename(self, id, name):
        """Give a project a new name: its folder is renamed."""
        project = self.open(id)
        project.busy_check()
        wanted = slug(name)
        if not wanted:
            raise UserError('Give the project a name (letters or digits).')
        if wanted == id:
            return id
        next_id = self.free_id(name)

        def move():
            os.rename(project.dir, os.path.join(self.root, next_id))
            project.end_clients()
            with self._lock:
                self.projects.pop(id, None)

        project.exclusive(move)
        return next_id

    def delete(self, id):
        """Take a project away. Its folder is not destroyed: it moves to the app's trash folder."""
        project = self.open(id)
        project.busy_check()
        os.makedirs(TRASH_DIR, exist_ok=True)
        to = os.path.join(TRASH_DIR, f"{id}-{time.strftime('%Y%m%d-%H%M%S')}")

        def move():
            shutil.move(project.dir, to)
            project.end_clients()
            with self._lock:
                self.projects.pop(id, None)

        project.exclusive(move)

    def duplicate(self, id, name):
        """A copy of a project with its history and conversation, under a new name."""
        original = self.open(id)
        original.busy_check()
        copy_id = self.free_id(str('' if name is None else name).strip() or f'{id}-copy')
        dir = os.path.join(self.root, copy_id)
        state = os.path.join(original.dir, STATE_DIR)
        original.exclusive(lambda: _copy_tree(original.dir, dir, lambda source: (
            os.path.dirname(source) == state and os.path.basename(source) in NOT_DUPLICATED
        )))
        project = self.open(copy_id)
        project.record({'kind': 'duplicate', 'from': id})
        return project


class Project:
    def __init__(self, builder, id, dir):
        self.builder = builder
        self.id = id
        self.dir = dir
        self.states = {}       # model file -> { build, good, exported }
        self.active = None     # the model last looked at
        self.turn = None       # the agent turn in progress, if any
        self.clients = set()   # open pages, as objects with write(frame) and end()
        self.ready = threading.Event()  # set once load() has finished, well or not
        self.load_error = None
        self._subscribers = []
        self._listeners = threading.Lock()  # guards `clients` and `_subscribers`
        self._queue = threading.Lock()      # one change to the project at a time

    def exclusive(self, fn):
        """Run `fn` after every earlier change to this project has finished. Not reentrant."""
        with self._queue:
            return fn()

    def load(self):
        if not git.is_repo(self.dir):
            git.init(self.dir)
        # Projects made under the app's earlier names keep their state in a folder named after it.
        for old in OLD_STATE_DIRS:
            legacy = os.path.join(self.dir, old)
            if os.path.exists(legacy) and not os.path.exists(os.path.join(self.dir, STATE_DIR)):
                os.rename(legacy, os.path.join(self.dir, STATE_DIR))
        os.makedirs(os.path.join(self.dir, STATE_DIR), exist_ok=True)
        self.gather_models()
        self.ignore_exports()
        if not git.head(self.dir):
            self.commit('import', 'Start of history')
        elif git.is_dirty(self.dir):
            self.commit('manual', 'Changes made outside the app')
        try:
            self.active = json.loads(_read_text(os.path.join(self.dir, STATE_DIR, 'state.json'))).get('active')
        except Exception:
            pass  # nothing remembered yet

    # What git should not track (exports, the app's state, Python's caches) is
    # listed in the repository itself, so a project needs no .gitignore. One the
    # app wrote earlier is taken away; one with lines of the user's own stays.
    def ignore_exports(self):
        exclude = os.path.join(self.dir, '.git', 'info', 'exclude')
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        _write_text(exclude, '\n'.join(IGNORE_LINES) + '\n')
        file = os.path.join(self.dir, '.gitignore')
        try:
            text = _read_text(file)
        except OSError:
            return
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if not all(line in IGNORE_LINES or line in OLD_IGNORE_LINES for line in lines):
            return
        tracked = git.is_tracked(self.dir, '.gitignore')
        _remove(file)
        if tracked and git.head(self.dir):
            self.commit('project', 'Ignore rules moved into the repository', paths=['.gitignore'])

    # --- events ------------------------------------------------------------------

    def emit(self, event):
        """Tell every open page. Listeners are called on this thread and must not block."""
        with self._listeners:
            clients = list(self.clients)
            subscribers = list(self._subscribers)
        if clients:
            frame = f'data: {_json(event)}\n\n'
            for client in clients:
                try:
                    client.write(frame)
                except Exception:
                    pass  # a page that went away
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass

    def subscribe(self, callback):
        """
        Call `callback(event)` with every event from now on; `callback(None)`
        means there will be no more (the project went away under this name).
        Returns a function that ends the subscription.
        """
        with self._listeners:
            self._subscribers.append(callback)

        def unsubscribe():
            with self._listeners:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def end_clients(self):
        """Close every open page's stream: the project is no longer at this address."""
        with self._listeners:
            clients = list(self.clients)
            subscribers = list(self._subscribers)
        for client in clients:
            try:
                client.end()
            except Exception:
                pass
        for callback in subscribers:
            try:
                callback(None)
            except Exception:
                pass

    def busy_check(self):
        if self.turn:
            raise UserError('Wait until I finish, or stop me.', 409)

    # --- files -------------------------------------------------------------------

    def files(self):
        return _listed(os.path.join(self.dir, MODELS_DIR), MODEL_NAME)

    def scans(self):
        return _listed(os.path.join(self.dir, SCANS_DIR), SCAN_NAME)

    def file_path(self, file):
        return os.path.join(self.dir, MODELS_DIR, file)

    def tracked(self, file):
        """A model's path inside the project, as git names it."""
        return f'{MODELS_DIR}/{file}'

    def gather_models(self):
        """
        Models used to lie directly in the project folder. Move them under `src/`
        as renames, so each keeps its history.
        """
        loose = [file['name'] for file in _listed(self.dir, MODEL_NAME) if not os.path.exists(self.file_path(file['name']))]
        if not loose:
            return
        os.makedirs(os.path.join(self.dir, MODELS_DIR), exist_ok=True)
        paths = []
        for name in loose:
            if git.is_tracked(self.dir, name):
                git.move(self.dir, name, self.tracked(name))
                paths.append(name)
            else:
                os.replace(os.path.join(self.dir, name), self.file_path(name))
            paths.append(self.tracked(name))
        if git.head(self.dir):
            self.commit('rename', f'Models moved to {MODELS_DIR}/', paths=paths)

    def export_path(self, file):
        return os.path.join(self.dir, f'{stem_of(file)}.stl')

    def scan_path(self, name):
        return os.path.join(self.dir, SCANS_DIR, name)

    def model(self, name):
        """The name of an existing model file, or a 404."""
        if not isinstance(name, str) or not MODEL_NAME.fullmatch(name) or not os.path.exists(self.file_path(name)):
            raise UserError('There is no such file in this project.', 404)
        return name

    def scan(self, name):
        """The name of an existing scan, or a 404."""
        if not isinstance(name, str) or not SCAN_NAME.fullmatch(name) or not os.path.exists(self.scan_path(name)):
            raise UserError('There is no such STL in this project.', 404)
        return name

    def state(self, file):
        state = self.states.get(file)
        if not state:
            # exported: the build key of the STL beside the model
            state = self.states.setdefault(file, {'build': None, 'good': None, 'exported': None})
        return state

    def source(self, file):
        return _read_text(self.file_path(file))

    def set_active(self, file):
        if self.active == file:
            return
        self.active = file
        try:
            _write_text(os.path.join(self.dir, STATE_DIR, 'state.json'), _json({'active': file}))
        except OSError:
            pass

    def files_changed(self):
        self.emit({'type': 'files', 'files': self.files(), 'scans': self.scans()})

    def free_name(self, wanted, *, stem, extension, pattern, taken, fallback, unique):
        base = stem(wanted) or fallback
        name = f'{base}{extension}'
        if not pattern.fullmatch(name):
            raise UserError('A name can have unaccented letters, digits, _ and -.')
        if taken(name) and not unique:
            raise UserError(f'The file {name} is already in this project.', 409)
        n = 2
        while taken(name):
            name = f'{base}_{n}{extension}'
            n += 1
        return name

    def target_for(self, wanted):
        """
        The file a prompt works on: the one asked for, else the one last looked
        at. In a project without models it names the file the turn will create.
        """
        if wanted is not None:
            return {'file': self.model(wanted), 'creates': False}
        files = self.files()
        if files:
            active = self.active
            return {'file': active if any(file['name'] == active for file in files) else files[0]['name'], 'creates': False}
        scans = self.scans()
        name = model_stem(stem_of(scans[0]['name'])) if len(scans) == 1 else ''
        file = f'{name or stem_of(DEFAULT_FILE)}.py'
        return {'file': file if MODEL_NAME.fullmatch(file) else DEFAULT_FILE, 'creates': True}

    # --- building ----------------------------------------------------------------

    def rebuild(self, file, reason, quiet=False):
        """Build the working tree's text of a model; the result is what everyone sees."""
        state = self.state(file)
        if not quiet:
            self.emit({'type': 'build', 'state': 'start', 'file': file, 'reason': reason})
        meta = self.builder.build(self.dir, file, self.source(file))
        state['build'] = meta
        if meta.get('ok'):
            state['good'] = meta
            # Opening a project asks the cache every time: copy the export only when it is not this build already.
            if state['exported'] != meta.get('key') or not os.path.exists(self.export_path(file)):
                try:
                    shutil.copyfile(os.path.join(cache_dir(self.dir, meta['key']), MERGED_STL), self.export_path(file))
                    state['exported'] = meta['key']
                except Exception:
                    state['exported'] = None
        elif meta.get('empty'):
            state['good'] = None
        if not quiet:
            self.emit({'type': 'build', 'state': 'done', 'file': file, 'reason': reason, 'build': meta, 'good': state['good']})
        return meta

    def write_and_build(self, file, text):
        """Write a new text for a model and build it. Used by the agent's tools."""
        state = self.state(file)
        before = state['build'] if (state['build'] or {}).get('ok') else state['good']
        os.makedirs(os.path.join(self.dir, MODELS_DIR), exist_ok=True)
        _write_text(self.file_path(file), text)
        meta = self.rebuild(file, 'agent')
        return {'meta': meta, 'report': report_for(meta, before)}

    # --- history -----------------------------------------------------------------

    def commit(self, kind, subject, paths=None, body='', trailers=None):
        """Commit `paths` only, or everything that changed when none are given."""
        trailers = {'Kind': kind, **(trailers or {})}
        if paths is not None:
            return git.commit_paths(self.dir, paths, subject, body=body, trailers=trailers)
        return git.commit_all(self.dir, subject, body=body, trailers=trailers)

    def versions(self, file):
        followed = git.log(self.dir, self.tracked(file))
        # Git follows a file back through copies too: a model made from another one
        # (or one empty file from another) would inherit that one's past. A model's
        # history starts where it was created or brought in.
        born = next((i for i, entry in enumerate(followed) if entry['kind'] in ('create', 'import')), -1)
        history = followed if born < 0 else followed[:born + 1]
        # A rename changes no content, so undo looks through it.
        content = [entry for entry in history if entry['kind'] != 'rename']
        current = content[0] if content else None
        origin = (current['origin'] or current['hash']) if current else None
        at = next((i for i, entry in enumerate(content) if entry['hash'] == origin), -1)
        # A redo takes back the newest undo that no redo has answered yet, and only
        # while nothing else was done since: it goes to the version from before that undo.
        redo_to, answered = None, 0
        for i, entry in enumerate(content):
            if entry.get('step') == 'redo':
                answered += 1
            elif entry.get('step') == 'undo' and answered:
                answered -= 1
            elif entry.get('step') == 'undo':
                redo_to = content[i + 1]['hash'] if i + 1 < len(content) else None
                break
            else:
                break
        return {
            'redoTo': redo_to,
            'head': history[0]['hash'] if history else None,
            'history': history,
            # The version an undo goes back to: the one before what HEAD reproduces.
            'undoTo': content[at + 1]['hash'] if at >= 0 and at + 1 < len(content) else None,
        }

    def all_journal(self):
        try:
            text = _read_text(os.path.join(self.dir, STATE_DIR, 'journal.jsonl'))
            entries = []
            for line in text.split('\n'):
                if not line:
                    continue
                entry = json.loads(line)
                # Entries from when a project had exactly one model do not name it.
                if 'file' not in entry and entry.get('kind') not in PROJECT_KINDS:
                    entry['file'] = DEFAULT_FILE
                entries.append(entry)
            return entries
        except Exception:
            return []

    def journal(self, file):
        """What happened to one model. What happened to the project as a whole (an STL added) is in no conversation."""
        entries = self.all_journal()
        return [entry for entry in entries if entry.get('file') == file][-JOURNAL_LIMIT:]

    def retag(self, from_, to):
        """Move a model's journal entries to another name."""
        entries = self.all_journal()
        if not any(entry.get('file') == from_ for entry in entries):
            return
        lines = [_json({**entry, 'file': to} if entry.get('file') == from_ else entry) for entry in entries]
        _write_text(os.path.join(self.dir, STATE_DIR, 'journal.jsonl'), '\n'.join(lines) + '\n')

    def record(self, entry, event_type='journal'):
        """Add an entry to the journal and tell open pages about it."""
        now = _now()
        full = {'id': _base36(now) + ''.join(random.choice(_BASE36) for _ in range(4)), 'ts': now, **entry}
        full['file'] = entry.get('file')
        os.makedirs(os.path.join(self.dir, STATE_DIR), exist_ok=True)
        with open(os.path.join(self.dir, STATE_DIR, 'journal.jsonl'), 'a', encoding='utf-8', newline='') as handle:
            handle.write(_json(full) + '\n')
        self.emit({'type': event_type, 'entry': full, 'file': full['file'],
                   **(self.versions(full['file']) if full['file'] else {})})
        return full

    def snapshot(self, wanted):
        """Everything a page needs to show the project with one of its models open."""
        files = self.files()

        def has(name):
            return name is not None and any(file['name'] == name for file in files)

        file = wanted if has(wanted) else self.active if has(self.active) else files[0]['name'] if files else None
        state = {'build': None, 'good': None}
        if file:
            self.set_active(file)
            state = self.state(file)
            # A turn keeps its own file's build current; anything else may have
            # changed underneath (a model it imports, a scan), so ask the cache.
            turn = self.turn
            if not state['build'] or (turn.file if turn else None) != file:
                self.rebuild(file, 'open', quiet=True)
        scans = self.scans()
        journal = self.journal(file)
        versions = self.versions(file) if file else {'head': None, 'history': [], 'undoTo': None, 'redoTo': None}
        turn = self.turn
        return {
            'id': self.id,
            'dir': self.dir,
            'files': files,
            'scans': scans,
            'file': file,
            'build': state['build'],
            'good': state['good'],
            'turn': turn.public_state() if turn else None,
            'journal': journal,
            **versions,
        }

    # --- changes to one model ------------------------------------------------------

    def set_param(self, file, name, value):
        """Change one number at the top of a model, without the agent."""
        self.busy_check()

        def change():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise UserError('The value must be a number.')
            source = self.source(file)
            param = next((p for p in self.builder.params(source) if p.get('name') == name and p.get('kind') == 'value'), None)
            if not param:
                raise UserError(f'The parameter {name} cannot be changed directly.')
            raw = param['raw']
            text = _number_text(value, raw)
            if text == raw:
                return
            lines = source.split('\n')
            line = lines[param['line'] - 1]
            lines[param['line'] - 1] = line[:param['start']] + text + line[param['end']:]
            _write_text(self.file_path(file), '\n'.join(lines))
            meta = self.rebuild(file, 'param')
            if not meta.get('ok'):
                _write_text(self.file_path(file), source)
                self.rebuild(file, 'param')
                message = (meta.get('error') or {}).get('message')
                raise UserError(f"The model does not build with {text}, so it stays {raw}. ({'error' if message is None else message})", 422)
            # People know a parameter by its comment; the variable name is the fallback.
            comment = param.get('comment')
            label = comment[0].upper() + comment[1:] if comment else name
            commit = self.commit('param', f'{label}: {raw} → {text}', paths=[self.tracked(file)])
            entry = {'kind': 'param', 'file': file, 'name': name}
            if comment is not None:
                entry['label'] = comment
            self.record({**entry, 'from': raw, 'to': text, 'commit': commit})

        return self.exclusive(change)

    def restore(self, file, rev, kind='restore'):
        """Bring back an earlier version of a model, as a new version on top."""
        self.busy_check()

        def change():
            found = self.version_at(file, rev)
            target, text = found['target'], found['text']
            if text == self.source(file):
                return  # already identical
            _write_text(self.file_path(file), text)
            self.rebuild(file, 'restore')
            commit = self.commit('restore', f"Restored the version from {_clock(target['time'])}",
                                 paths=[self.tracked(file)],
                                 trailers={'Origin': target['origin'] or target['hash'],
                                           **({'Step': kind} if kind in ('undo', 'redo') else {})})
            if not commit:
                return
            self.record({'kind': kind, 'file': file, 'target': target['hash'], 'subject': target['subject'],
                         'time': target['time'], 'commit': commit})

        return self.exclusive(change)

    def undo(self, file):
        undo_to = self.versions(file)['undoTo']
        if not undo_to:
            raise UserError('There is no earlier version.', 409)
        return self.restore(file, undo_to, 'undo')

    def redo(self, file):
        redo_to = self.versions(file)['redoTo']
        if not redo_to:
            raise UserError('There is nothing to redo.', 409)
        return self.restore(file, redo_to, 'redo')

    def version_at(self, file, rev):
        """One earlier version of a model: its entry in the history and its text, or a 404."""
        history = self.versions(file)['history']
        target = next((entry for entry in history if entry['hash'] == rev), None)
        text = git.show_file(self.dir, target['hash'], target['path']) if target else None
        if text is None:
            raise UserError('There is no such version.', 404)
        return {'target': target, 'text': text}

    def source_at(self, file, rev):
        """The text of a model as it was in an earlier version."""
        return self.version_at(file, rev)['text']

    def version(self, file, rev):
        """The build of an earlier version, for looking at it without restoring."""
        return self.builder.build(self.dir, file, self.source_at(file, rev))

    # --- adding, renaming and removing files -----------------------------------------

    def create_file(self, name=None, source=None, unique=False):
        """
        A new model file: brought in (`source`) or empty, waiting for the first
        prompt. `unique` picks a free name instead of refusing a taken one.
        """
        self.busy_check()

        def change():
            file = self.free_name(
                name, stem=model_stem, extension='.py', pattern=MODEL_NAME, fallback=stem_of(DEFAULT_FILE), unique=unique,
                taken=lambda candidate: os.path.exists(self.file_path(candidate)),
            )
            brought = isinstance(source, str)
            os.makedirs(os.path.join(self.dir, MODELS_DIR), exist_ok=True)
            _write_text(self.file_path(file), source if brought else '')
            kind = 'import' if brought else 'create'
            commit = self.commit(kind, f'Imported model {file}' if brought else f'New model {file}', paths=[self.tracked(file)])
            self.set_active(file)
            self.record({'kind': kind, 'file': file, 'commit': commit})
            self.files_changed()
            return file

        return self.exclusive(change)

    def rename_file(self, from_, to):
        self.busy_check()

        def change():
            file = self.free_name(
                to, stem=model_stem, extension='.py', pattern=MODEL_NAME, fallback='', unique=False,
                taken=lambda candidate: candidate != from_ and os.path.exists(self.file_path(candidate)),
            )
            if file == from_:
                return from_
            # A model git has not seen yet (put there by hand since the last commit)
            # has no old name to commit: naming it would make the commit fail.
            tracked = git.is_tracked(self.dir, self.tracked(from_))
            if tracked:
                git.move(self.dir, self.tracked(from_), self.tracked(file))
            else:
                os.replace(self.file_path(from_), self.file_path(file))
            commit = self.commit('rename', f'{from_} → {file}',
                                 paths=[self.tracked(from_), self.tracked(file)] if tracked else [self.tracked(file)])
            try:
                os.replace(self.export_path(from_), self.export_path(file))
            except OSError:
                pass
            if from_ in self.states:
                self.states[file] = self.states.pop(from_)
            self.retag(from_, file)
            if self.active == from_:
                self.set_active(file)
            self.record({'kind': 'rename', 'file': file, 'from': from_, 'commit': commit})
            self.files_changed()
            return file

        return self.exclusive(change)

    def delete_file(self, file):
        self.busy_check()

        def change():
            tracked = git.is_tracked(self.dir, self.tracked(file))
            _remove(self.file_path(file))
            _remove(self.export_path(file))
            if tracked:
                self.commit('delete', f'Deleted {file}', paths=[self.tracked(file)])
            self.states.pop(file, None)
            # Its entries stay in the journal file but no longer belong to a model,
            # so a later file of the same name starts with a clean conversation.
            self.retag(file, f'{file}#{_base36(_now())}')
            if self.active == file:
                self.set_active(None)
            self.files_changed()

        return self.exclusive(change)

    def drop_empty(self, file):
        """Forget a file a turn created but left without a model in it."""
        _remove(self.file_path(file))
        self.states.pop(file, None)
        if self.active == file:
            self.set_active(None)
        self.files_changed()

    # --- scans -------------------------------------------------------------------

    def add_scan(self, name, uploaded):
        """Take an uploaded mesh (already on disk at `uploaded`) into the project."""
        def change():
            if not _looks_like_stl(uploaded):
                raise UserError('This does not look like an STL file.')
            file = self.free_name(
                name, stem=scan_stem, extension='.stl', pattern=SCAN_NAME, fallback='scan', unique=True,
                taken=lambda candidate: os.path.exists(self.scan_path(candidate)),
            )
            os.makedirs(os.path.join(self.dir, SCANS_DIR), exist_ok=True)
            os.replace(uploaded, self.scan_path(file))
            commit = self.commit('scan', f'Added scan {file}', paths=[f'{SCANS_DIR}/{file}'])
            self.record({'kind': 'scan.add', 'name': file, 'commit': commit})
            self.files_changed()
            # Measured now, in the background, so the first prompt about it does not wait.
            if (self.builder.info or {}).get('scan') is True:
                threading.Thread(target=self._measure_quietly, args=(file,), daemon=True).start()
            return file

        return self.exclusive(change)

    def _measure_quietly(self, name):
        try:
            self.scan_overview(name)
        except Exception:
            pass

    def remove_scan(self, name):
        self.busy_check()

        def change():
            tracked = git.is_tracked(self.dir, f'{SCANS_DIR}/{name}')
            noted = git.is_tracked(self.dir, self.notes_file(name))
            _remove(self.scan_path(name))
            _remove(self.notes_path(name))  # what was learned about it goes with it
            paths = ([f'{SCANS_DIR}/{name}'] if tracked else []) + ([self.notes_file(name)] if noted else [])
            if paths:
                self.commit('scan', f'Removed scan {name}', paths=paths)
            self.record({'kind': 'scan.remove', 'name': name})
            self.files_changed()

        return self.exclusive(change)

    def rename_scan(self, from_, to):
        """Give a scan another name; the models that reproduce it follow."""
        self.busy_check()

        def change():
            file = self.free_name(
                to if isinstance(to, str) else '', stem=scan_stem, extension='.stl', pattern=SCAN_NAME, fallback='scan', unique=False,
                taken=lambda candidate: candidate != from_ and os.path.exists(self.scan_path(candidate)),
            )
            if file == from_:
                return from_
            paths = []
            moves = [(f'{SCANS_DIR}/{from_}', f'{SCANS_DIR}/{file}')]
            if os.path.exists(self.notes_path(from_)):
                moves.append((self.notes_file(from_), self.notes_file(file)))
            for old, new in moves:
                if git.is_tracked(self.dir, old):
                    git.move(self.dir, old, new)
                    paths.append(old)
                else:
                    os.replace(os.path.join(self.dir, old), os.path.join(self.dir, new))
                paths.append(new)
            # A model names the scan it reproduces in its text: `scan("name")`, with or without `.stl`.
            named = re.compile(r'(\bscan\(\s*)([\'"])' + re.escape(from_[:-4]) + r'(?:\.stl)?\2', re.I)
            changed = []
            for model in self.files():
                source = self.source(model['name'])
                text = named.sub(lambda found: f'{found.group(1)}{found.group(2)}{file}{found.group(2)}', source)
                if text != source:
                    _write_text(self.file_path(model['name']), text)
                    paths.append(self.tracked(model['name']))
                    changed.append(model['name'])
            self.commit('scan', f'{from_} → {file}', paths=paths)
            self.files_changed()
            for name in changed:
                if name in self.states:
                    self.rebuild(name, 'rename')
            return file

        return self.exclusive(change)

    def thumb(self, *, file=None, scan=None):
        """The path of a small picture of a model or a scan, made on first use and kept until the file changes."""
        if scan:
            out = os.path.join(scan_cache(self.dir, scan), 'thumb-clear.png')
            subject = {'scan': scan}
        else:
            source = self.source(file)
            if not source.strip():
                raise UserError('The model is still empty.', 404)
            key = hashlib.sha1(f'thumb-clear|{file}|{source}'.encode('utf-8')).hexdigest()[:20]
            out = os.path.join(self.dir, STATE_DIR, 'cache', f'thumb-{key}.png')
            subject = {'file': file, 'source': source}
        if not os.path.exists(out):
            try:
                self.builder.thumbnail(self.dir, subject, out)
            except Exception as err:  # noqa: BLE001 -- a model that does not build has no picture
                raise UserError(f'No picture. ({err})', 404) from None
        return out

    # What has been learned about a scan (where a feature is, what it measures)
    # is kept as a short list next to the project's files, so the same question
    # is not measured twice. One Markdown file per scan, tracked like the models.
    def notes_file(self, name):
        return f'{NOTES_DIR}/{name}.md'

    def notes_path(self, name):
        return os.path.join(self.dir, NOTES_DIR, f'{name}.md')

    def scan_notes(self, name):
        try:
            return _read_text(self.notes_path(name)).strip()
        except OSError:
            return ''

    def add_scan_note(self, name, text):
        """
        Add one finding about a scan. Returns the notes as they now stand. Called
        from inside a turn, which already holds the project.
        """
        line = f"- {_SPACES.sub(' ', str(text)).strip()[:NOTE_LIMIT]}"
        before = self.scan_notes(name)
        if line in before.split('\n'):
            return before
        lines = ((before.split('\n') if before else []) + [line])[-NOTES_KEPT:]
        os.makedirs(os.path.join(self.dir, NOTES_DIR), exist_ok=True)
        _write_text(self.notes_path(name), '\n'.join(lines) + '\n')
        self.commit('note', f'Noted about {name}', paths=[self.notes_file(name)])
        return '\n'.join(lines)

    def scan_overview(self, name):
        """Size, faces, holes and sections of a scan; measured once, then remembered."""
        return self.builder.scan_overview(self.dir, name)
