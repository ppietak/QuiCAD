# Run from the app's folder, through the `start` launcher beside it; there is no global command.
#   ./start       start the editor and open it in the browser

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.request

from .config import DEFAULT_PORT, access_token, ensure_python, projects_dir

USAGE = f"""Usage, in the app's folder:
  ./start [options]    start the editor and open it in the browser

Options:
  --port <number>  port (default {DEFAULT_PORT})
  --no-open        do not open the browser
  --api-keys       let Claude Code and Codex use an API key from the environment
                   (billed per use); without it turns run on the subscription,
                   unless the settings say otherwise

Projects live in {projects_dir()} (another folder is chosen in the settings,
or named by the APP_PROJECTS variable).
The editor works through Claude Code or Codex, as they are signed in on this computer."""


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def _number(text):
    """JavaScript's Number(text) for a port: an int, a float, or None where that gives NaN."""
    text = (text or '').strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value:
        return None
    return int(value) if value.is_integer() else value


def open_in_browser(url):
    if sys.platform == 'darwin':
        command = ['open', url]
    elif sys.platform == 'win32':
        command = ['cmd', '/c', 'start', '', url]
    else:
        command = ['xdg-open', url]
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=sys.platform != 'win32')
    except OSError:
        pass


def already_running(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this machine, never through a proxy
        with opener.open(f'http://127.0.0.1:{port}/api/ping', timeout=0.8) as res:
            return res.status == 200 and json.loads(res.read().decode('utf-8')).get('app') == 'cad-editor'
    except Exception:  # noqa: BLE001 -- nothing there, or not this app
        return False


def command_open(argv):
    port = _number(os.environ.get('PORT')) or DEFAULT_PORT
    open_ = True
    i = 0
    while i < len(argv):
        if argv[i] == '--no-open':
            open_ = False
        elif argv[i] == '--api-keys':
            os.environ['APP_API_KEYS'] = '1'
        elif argv[i] == '--port' and i + 1 < len(argv) and argv[i + 1]:
            i += 1
            port = _number(argv[i])
        else:
            fail(f'Unknown option: {argv[i]}\n\n{USAGE}')
        i += 1
    if not isinstance(port, int) or port < 0 or port > 65535:
        fail(f'Bad port number.\n\n{USAGE}')

    token = access_token()
    if port != 0 and already_running(port):
        url = f'http://127.0.0.1:{port}/#t={token}'
        print(f'The editor is already running: {url}')
        if open_:
            open_in_browser(url)
        return

    # Any of the CLIs signed in with a plan will do; all that are get used.
    # With none signed in the editor still starts, and says so.
    from .accounts import Accounts
    accounts = Accounts()
    everyone = accounts.found

    try:
        python = ensure_python()
    except Exception as err:  # noqa: BLE001
        fail(str(err))
    from .server import start_server
    try:
        server = start_server(port=port, python=python, token=token, accounts=accounts)
    except OSError as err:
        fail(f'Port {port} is in use. Give another: ./start --port <number>'
             if err.errno == errno.EADDRINUSE else str(err))
    url = f'{server.base_url}/#t={token}'
    for provider in everyone:
        plan = provider.get('plan')
        if provider['subscription']:
            print(f"{provider['name']}: subscription {'(plan unknown)' if plan is None else plan}.")
        elif provider['installed']:
            print(f"{provider['name']}: not signed in with a subscription (sign in with `{provider['command']}` itself).")
        else:
            print(f"{provider['name']}: not installed ({provider['install']}).")
    print(f'Projects: {projects_dir()}')
    print(f'Editor: {url}')
    print('Ctrl+C quits.', flush=True)
    if open_:
        open_in_browser(url)

    # The server works on its own threads; this one waits for the signal to quit.
    quit_ = threading.Event()
    for name in ('SIGINT', 'SIGTERM'):
        try:
            signal.signal(getattr(signal, name), lambda *_: quit_.set())
        except (AttributeError, ValueError, OSError):
            pass  # a signal this system does not have
    try:
        while not quit_.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    server.close()
    sys.stdout.flush()
    os._exit(0)  # threads still waiting for a build or a turn must not keep the app alive


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else None
    rest = argv[1:]
    if command in ('open', 'start'):
        command_open(rest)
    elif command in ('-h', '--help', 'help'):
        print(USAGE)
    elif command is None or command.startswith('--'):
        command_open(argv)  # no command: start the editor, with whatever options were given
    else:
        fail(f'Unknown command: {command}\n\n{USAGE}')
