# API keys the person typed into the settings, for turns that are billed per use.
# They are kept apart from the other settings, in a file only its owner can
# read, and go one way: to the CLI, in its environment, when a turn starts.
# The page is told that a key is kept and how it ends, never the key.

import json
import os
import threading

from .config import DATA_DIR

FILE = os.path.join(DATA_DIR, 'keys.json')
MAX_LENGTH = 512

_lock = threading.Lock()
_keys = None


def _load():
    global _keys
    if _keys is None:
        try:
            with open(FILE, 'r', encoding='utf-8') as handle:
                found = json.load(handle)
        except (OSError, ValueError):
            found = {}  # none was given yet
        _keys = {name: key for name, key in found.items() if isinstance(key, str) and key} if isinstance(found, dict) else {}
    return _keys


def usable(key):
    """Whether this can be a key at all: one run of visible characters, as they are pasted."""
    return 0 < len(key) <= MAX_LENGTH and key.isascii() and key.isprintable() and ' ' not in key


def get(provider):
    """The key kept for a provider ('claude', 'codex'); '' when there is none."""
    with _lock:
        return _load().get(provider, '')


def ending(provider):
    """The last characters of the kept key, to tell it from another; '' when there is none."""
    key = get(provider)
    return key[-4:] if len(key) >= 12 else ''


def put(provider, key):
    """Keep this key for the provider; '' forgets the one that is kept."""
    global _keys
    with _lock:
        keys = dict(_load())
        if key:
            keys[provider] = key
        else:
            keys.pop(provider, None)
        os.makedirs(DATA_DIR, exist_ok=True)
        part = FILE + '.part'
        # Made for its owner only from the start: it never exists readable by others.
        with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8', newline='') as handle:
            json.dump(keys, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.chmod(part, 0o600)
        os.replace(part, FILE)
        _keys = keys
