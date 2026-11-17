# What the person chose on the settings screen: one small file in the app's
# data folder. Whatever is not set there is as the app was started (its
# options and variables).
#
#   projects   the folder projects are kept in
#   assistant  'claude' or 'codex': the CLI prompts run on; '' while none was
#              chosen, and the first one that can be used is taken
#   billing    'subscription': turns run only on a plan; 'api-key': also on an
#              API key, billed per use: the one typed into the settings, or
#              one the CLI has or finds
#   claude     where the Claude Code CLI is, when it is not found by itself
#   codex      the same for Codex
#
# No key or sign-in is kept here: sign-ins stay with the CLIs, and a key typed
# into the settings has a file of its own (keys.py).

import json
import os
import threading

from .config import DATA_DIR

FILE = os.path.join(DATA_DIR, 'settings.json')
DEFAULTS = {'projects': '', 'assistant': '', 'billing': 'subscription', 'claude': '', 'codex': ''}
BILLING = ('subscription', 'api-key')
ASSISTANTS = ('', 'claude', 'codex')  # the CLIs a turn can run on (app/agent.py); '' while none was chosen

_lock = threading.Lock()
_values = None


def _clean(found):
    """What a settings file says, with anything it should not say left out."""
    values = dict(DEFAULTS)
    if isinstance(found, dict):
        for name in DEFAULTS:
            if isinstance(found.get(name), str):
                values[name] = found[name].strip()
    if values['billing'] not in BILLING:
        values['billing'] = DEFAULTS['billing']
    if values['assistant'] not in ASSISTANTS:
        values['assistant'] = DEFAULTS['assistant']
    return values


def _load():
    global _values
    if _values is None:
        try:
            with open(FILE, 'r', encoding='utf-8') as handle:
                _values = _clean(json.load(handle))
        except (OSError, ValueError):
            _values = dict(DEFAULTS)  # nothing was chosen yet
    return _values


def get(name):
    with _lock:
        return _load()[name]


def everything():
    with _lock:
        return dict(_load())


def save(patch):
    """Keep the given settings; the others stay. Returns all of them."""
    global _values
    with _lock:
        values = _clean({**_load(), **patch})
        os.makedirs(DATA_DIR, exist_ok=True)
        part = FILE + '.part'
        with open(part, 'w', encoding='utf-8', newline='') as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(part, FILE)
        _values = values
        return dict(values)
