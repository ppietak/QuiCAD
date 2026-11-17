# The few git operations a project needs. Every project is its own repository;
# history is linear and nothing is ever rewritten: undo is a new commit.

import os
import re
import subprocess

REV = re.compile(r'[0-9a-f]{7,40}')
FIELD = '\x1f'
RECORD = '\x1e'
_SPACES = re.compile(r'\s+')

# The git on this computer, or the one the app brought with it (the desktop app has its own).
GIT = os.environ.get('APP_GIT') or 'git'

# Signing and hooks belong to the person's own repositories, not to these.
# (No hooks: the null device, under the name git knows it by on this system.)
_NO_HOOKS = 'NUL' if os.name == 'nt' else '/dev/null'
BASE_ARGS = ['-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={_NO_HOOKS}', '-c', 'core.quotepath=false']

_identity_env = None


def _exec(dir, args, env=None):
    """Run git in `dir`; never raises: `{'ok', 'stdout', 'stderr'}`."""
    try:
        done = subprocess.run(
            [GIT, '-C', dir, *BASE_ARGS, *args],
            env={**os.environ, **(env or {})},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError:
        return {'ok': False, 'stdout': '', 'stderr': ''}
    return {
        'ok': done.returncode == 0,
        'stdout': done.stdout.decode('utf-8', 'replace'),
        'stderr': done.stderr.decode('utf-8', 'replace'),
    }


def _git(dir, args, env=None):
    result = _exec(dir, args, env=env)
    if not result['ok']:
        raise RuntimeError(f"git {args[0]}: {result['stderr'].strip() or 'failed'}")
    return result['stdout']


# Commits need an author; fall back to a neutral one when git has none configured.
def _identity(dir):
    global _identity_env
    if _identity_env is not None:
        return _identity_env
    configured = _exec(dir, ['config', '--get', 'user.email'])
    _identity_env = {} if configured['ok'] and configured['stdout'].strip() else {
        'GIT_AUTHOR_NAME': 'Editor',
        'GIT_AUTHOR_EMAIL': 'editor@localhost',
        'GIT_COMMITTER_NAME': 'Editor',
        'GIT_COMMITTER_EMAIL': 'editor@localhost',
    }
    return _identity_env


def _is_rev(value):
    return isinstance(value, str) and REV.fullmatch(value) is not None


def init(dir):
    _git(dir, ['init', '--quiet', '--initial-branch=main'])


def is_repo(dir):
    return _exec(dir, ['rev-parse', '--git-dir'])['ok']


def head(dir):
    result = _exec(dir, ['rev-parse', '--verify', '--quiet', 'HEAD'])
    return result['stdout'].strip() if result['ok'] else None


def is_dirty(dir, paths=()):
    return len(_git(dir, ['status', '--porcelain', '--', *(paths if paths else ['.'])]).strip()) > 0


def _text(value):
    """A trailer's value as text, the way the page's JSON would spell it."""
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return str(value)


def _message_of(subject, body, trailers):
    trailer_lines = [
        key + ': ' + _SPACES.sub(' ', _text(value))
        for key, value in trailers.items()
        if value is not None and value != ''
    ]
    parts = [_SPACES.sub(' ', subject).strip() or 'Change', body.strip(), '\n'.join(trailer_lines)]
    return '\n\n'.join(part for part in parts if part)


def commit_paths(dir, paths, subject, body='', trailers=None):
    """
    Commit what changed in `paths` and nothing else, so each version in a file's
    history is about that file. Returns the new hash, or None when nothing changed.
    """
    paths = list(paths)
    # A path that is neither on disk nor in the index (the old name after `git mv`)
    # has nothing to stage, and naming it would make `git add` fail. Every path
    # must still be known to HEAD or the index, or the commit itself fails.
    known = [file for file in paths if os.path.exists(os.path.join(dir, file)) or is_tracked(dir, file)]
    if known:
        _git(dir, ['add', '--all', '--', *known])
    if not is_dirty(dir, paths):
        return None
    _git(dir, ['commit', '--quiet', '--no-verify', '-m', _message_of(subject, body, trailers or {}), '--', *paths],
         env=_identity(dir))
    return head(dir)


def commit_all(dir, subject, body='', trailers=None, empty=False):
    """Commit everything that changed. Returns the new hash, or None when nothing changed (`empty` commits even then)."""
    _git(dir, ['add', '--all'])
    if not empty and not is_dirty(dir):
        return None
    _git(dir, ['commit', '--quiet', '--no-verify', *(['--allow-empty'] if empty else []),
               '-m', _message_of(subject, body, trailers or {})], env=_identity(dir))
    return head(dir)


def log(dir, file, limit=300):
    """
    The versions of one file, newest first, followed across renames. `path` is
    the file's name as of that version.
    """
    # Versions made under the app's earlier names carry prefixed trailers; a commit has only one of them.
    def trailer(key):
        return ''.join(f'%(trailers:key={prefix}{key},valueonly,separator=%x20)' for prefix in ('', 'Promptorys-', 'Bryla-'))
    fields = '%x1f'.join(['%H', '%ct', '%s', trailer('Kind'), trailer('Origin'), trailer('Step')])
    result = _exec(dir, ['log', '--follow', '--name-status', f'--max-count={limit}', f'--format=%x1e{fields}%x1f', '--', file])
    if not result['ok']:
        return []
    entries = []
    for record in result['stdout'].split(RECORD):
        if not record.strip():
            continue
        parts = record.split(FIELD)
        parts += [None] * (7 - len(parts))
        hash_, time, subject, kind, origin, step, names = parts[:7]
        # "M\tname", "A\tname" or "R100\told\tnew": the last field is the name in this version.
        status = next((line.strip() for line in (names or '').split('\n') if line.strip()), '')
        entries.append({
            'hash': hash_.strip(),
            'time': int(time) * 1000,
            'subject': subject,
            'kind': (kind or '').strip() or 'manual',
            'origin': (origin or '').strip() or None,
            'step': (step or '').strip() or None,  # 'undo' or 'redo' when the version was made by one
            'path': status.split('\t')[-1] or file,
        })
    return entries


def show_file(dir, rev, file):
    if not _is_rev(rev):
        return None
    result = _exec(dir, ['show', f'{rev}:{file}'])
    return result['stdout'] if result['ok'] else None


def move(dir, from_, to):
    _git(dir, ['mv', '--', from_, to])


def is_tracked(dir, file):
    """Whether git knows the file (it is in HEAD or staged)."""
    return _exec(dir, ['ls-files', '--error-unmatch', '--', file])['ok']
