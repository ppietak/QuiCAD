# Where things live, and the Python environment the build worker runs in.
# Run from its folder (`./start`), the app keeps everything inside it: projects
# in projects/, the Python environment and the page's access key in .data/.
# The APP_DATA and APP_PROJECTS variables name other folders (the desktop app
# sets them), and the settings screen can move the projects.
#
# This module is imported by the launcher before the environment exists, with
# whatever `python3` the system has: it must stay importable on Python 3.8.

import hmac
import os
import secrets
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('APP_DATA') or os.path.join(APP_DIR, '.data')
DEFAULT_PROJECTS_DIR = os.environ.get('APP_PROJECTS') or os.path.join(APP_DIR, 'projects')
TRASH_DIR = os.path.join(DATA_DIR, 'trash')  # deleted projects end up here, whole
DEFAULT_PORT = 4610


def projects_dir():
    """The folder projects are kept in: the one chosen in the settings, else the app's own."""
    from . import settings
    return settings.get('projects') or DEFAULT_PROJECTS_DIR


def api_keys_forced():
    """Whether the app was started with `--api-keys`: the settings cannot switch that off."""
    return os.environ.get('APP_API_KEYS') == '1'


def keep_api_keys():
    """Whether the CLIs may run on an API key or a cloud provider they have or find in the
    environment (`./start --api-keys`, or chosen in the settings). Off by default: turns
    then always run on the subscription."""
    from . import settings
    return api_keys_forced() or settings.get('billing') == 'api-key'


DEFAULT_FILE = 'model.py'  # the name a project's first model gets
MERGED_STL = 'model.stl'   # in a build's cache folder: every printed part together
MODELS_DIR = 'src'         # per project: the code of its models
SCANS_DIR = 'scans'        # per project: meshes uploaded to be reverse engineered
NOTES_DIR = 'notes'        # per project, tracked: what was learned about each scan
STATE_DIR = '.sys'  # per project, ignored by git: journal, build cache, attachments

# Part of every build cache key: bump when the worker's output changes.
ENGINE_VERSION = 6
# Tessellation: absolute chord error in mm and the largest angle between facets in radians.
MESH = {'tol': 0.02, 'ang': 0.2}
BUILD_TIMEOUT_S = 90

BUILD123D = 'build123d==0.11.1'
PYTHON_VERSION = '3.13'
MIN_PYTHON = (3, 10)  # the oldest interpreter build123d installs on

# The three sizes of model a turn can run on. Each provider fills a size with
# one of its models, so the page can show them side by side.
TIERS = [
    {'id': 'light', 'hint': 'for the simplest changes'},
    {'id': 'standard', 'hint': 'recommended: the best fit for most work'},
    {'id': 'strong', 'hint': 'for the most demanding work'},
]
DEFAULT_TIER = 'standard'
_HINT = {tier['id']: tier['hint'] for tier in TIERS}

# What a turn can run on with Claude Code; Codex lists its own (app/codex.py).
MODELS = [
    {'id': 'sonnet', 'label': 'Sonnet', 'hint': _HINT['light'], 'provider': 'claude', 'tier': 'light'},
    {'id': 'opus', 'label': 'Opus', 'hint': _HINT['standard'], 'provider': 'claude', 'tier': 'standard'},
    {'id': 'fable', 'label': 'Fable', 'hint': _HINT['strong'], 'provider': 'claude', 'tier': 'strong'},
]
DEFAULT_MODEL = 'opus'  # when it is among the choices; else the standard size of what there is

_WINDOWS = sys.platform == 'win32'


def _run(command, args, quiet=False):
    """Run a command to its end; whether it succeeded."""
    try:
        if quiet:
            code = subprocess.call([command] + list(args), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            code = subprocess.call([command] + list(args))
    except OSError:
        return False
    return code == 0


def _venv_python(venv):
    return os.path.join(venv, 'Scripts', 'python.exe') if _WINDOWS else os.path.join(venv, 'bin', 'python')


def _own_uv():
    """
    A `uv` of the app's own, in .data/uv, for a system whose Python is too old
    for build123d: uv then fetches a newer Python by itself. None when it cannot
    be installed.
    """
    home = os.path.join(DATA_DIR, 'uv')
    uv = os.path.join(home, 'Scripts', 'uv.exe') if _WINDOWS else os.path.join(home, 'bin', 'uv')
    if os.path.exists(uv) and _run(uv, ['--version'], quiet=True):
        return uv
    ok = (_run(sys.executable, ['-m', 'venv', home])
          and _run(_venv_python(home), ['-m', 'pip', 'install', '--quiet', 'uv']))
    return uv if ok else None


def ensure_python():
    """The interpreter for the build worker; creates the environment on first use."""
    if os.environ.get('APP_PYTHON'):
        return os.environ['APP_PYTHON']
    venv = os.path.join(DATA_DIR, 'venv')
    python = _venv_python(venv)
    if os.path.exists(python) and _run(python, ['-c', 'import build123d'], quiet=True):
        return python

    print('First run: installing the CAD engine (build123d). This takes about a minute.', flush=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    uv = 'uv' if _run('uv', ['--version'], quiet=True) else None
    if uv is None and sys.version_info < MIN_PYTHON:
        # The app was started by a Python that build123d does not support.
        uv = _own_uv()
        ok = uv is not None
    else:
        ok = True
    if ok and uv is not None:
        ok = (_run(uv, ['venv', '--python', PYTHON_VERSION, venv])
              and _run(uv, ['pip', 'install', '--python', python, BUILD123D]))
    elif ok:
        ok = (_run(sys.executable, ['-m', 'venv', venv])
              and _run(python, ['-m', 'pip', 'install', '--quiet', BUILD123D]))
    if not ok:
        raise RuntimeError('Could not install build123d. Install `uv` (https://docs.astral.sh/uv/) '
                           'or point to a ready interpreter with the APP_PYTHON variable.')
    return python


def same_secret(given, secret):
    """Whether `given` is the secret, compared in constant time."""
    a = str('' if given is None else given).encode('utf-8')
    b = str(secret).encode('utf-8')
    return hmac.compare_digest(a, b)


def access_token():
    """The access key for the web page. Kept across runs so an open tab survives a restart."""
    file = os.path.join(DATA_DIR, 'token')
    try:
        with open(file, 'r', encoding='utf-8') as handle:
            saved = handle.read().strip()
        if len(saved) >= 32:
            return saved
    except (OSError, UnicodeDecodeError):
        pass  # first run
    os.makedirs(DATA_DIR, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
        handle.write(token + '\n')
    os.chmod(file, 0o600)
    return token
