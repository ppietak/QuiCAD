# The accounts a turn can run on, as the page sees them: which CLI is installed
# and signed in on this computer, and the models that gives. The app only reads
# that state; signing in and out is done with the CLIs themselves.

import threading
import time

from .agent import providers

STALE_S = 20  # how long an answer from the CLIs is trusted before asking again


class Accounts:
    """`fixed` (a list of models) makes a set that never changes: for tests."""

    def __init__(self, fixed=None, on_change=None):
        self.lock = threading.Lock()
        self.fixed = fixed
        self.on_change = on_change
        self.found = []     # what `providers()` said last
        self.asked = 0      # when it was asked
        self.asking = False
        if fixed is None:
            self.refresh()

    def refresh(self):
        """Ask the CLIs again who is signed in."""
        if self.fixed is not None:
            return
        try:
            found = providers()
            with self.lock:
                self.found = found
        finally:
            with self.lock:
                self.asked = time.monotonic()
                self.asking = False
        if self.on_change:
            self.on_change()

    def refresh_if_stale(self):
        """Somebody may have signed in or out in a terminal meanwhile: ask again, without making anyone wait."""
        with self.lock:
            if self.fixed is not None or self.asking or time.monotonic() - self.asked < STALE_S:
                return
            self.asking = True
        threading.Thread(target=self.refresh, daemon=True).start()

    def models(self):
        if self.fixed is not None:
            return self.fixed
        with self.lock:
            return [model for provider in self.found for model in provider['models']]

    def plan(self):
        with self.lock:
            return ', '.join(provider['plan'] for provider in self.found if provider['subscription'] and provider.get('plan')) or None

    def public(self):
        """The providers for the page."""
        with self.lock:
            return [{
                'id': provider['id'],
                'name': provider['name'],
                'command': provider['command'],
                'install': provider['install'],
                'installed': provider['installed'],
                'signedIn': provider['subscription'],
                # Signed in, but in a way that is billed per use: turns do not run on it.
                'otherSignIn': provider['authMethod'] if provider['loggedIn'] and not provider['subscription'] else None,
                'plan': provider.get('plan'),
            } for provider in self.found]
