# One prompt = one turn on one model file: start Claude Code with a minimal
# context, let it change the model through its tools, build after each change,
# commit at the end.
#
# Claude Code is used as installed and signs in by itself; this app never sees
# or stores credentials. Its usual context (its system prompt, built-in tools,
# the person's settings, plugins, MCP servers, CLAUDE.md) is switched off for
# these turns: about 24k tokens per request become about 2k.
#
# A turn is touched from several threads: the one that runs it, the ones that
# read the CLI's output, and one per tool call arriving over HTTP. What the
# Node version got from running one callback at a time is spelled out here:
# `turn.lock` for the turn's own fields, `turn.in_tool` for one tool at a time.

import base64
import decimal
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from . import codex
from . import git
from .config import DATA_DIR, DEFAULT_MODEL, DEFAULT_TIER, MODELS, STATE_DIR, keep_api_keys, same_secret
from .projects import UserError, stem_of
from .prompt import compose_message, js_length, js_slice, js_string, js_trim, system_prompt_for, tools_for
from .report import checks_failed, report_for, scan_fit

NAME = 'claude'
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
EFFORT = os.environ.get('APP_EFFORT') if os.environ.get('APP_EFFORT') in EFFORTS else 'low'
THINKING = os.environ.get('APP_THINKING') != '0'
LOG_FILE = 'log.jsonl'  # every turn, step by step, in <project>/.sys/ (for debugging)
LOG_MAX_BYTES = 8 * 1024 * 1024
MAX_FAILED_BUILDS = 6
# A model that reproduces a scan gets its deviation back and may refine a few
# times before the turn ends, until this share of points is within tolerance.
GOOD_FIT = 0.9
MAX_REFINEMENTS = 3
MAX_SCANS_IN_PROMPT = 3
PHASE_OF_TOOL = {'read': 'reading', 'measure': 'measuring', 'note': 'measuring', 'look': 'looking'}
VIEWS = ['iso', 'top', 'bottom', 'front', 'back', 'left', 'right']
ABOUT_SCANS = re.compile(r'skan|scan|\.stl|odtw[oó]rz|odwzoruj|reverse', re.I)
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}
TEXT_FILE = re.compile(r'\.(py|txt|md|json|csv|tsv|yaml|yml|toml|scad|svg|html?|css|js|ts|ini|cfg|log)$', re.I)

# Not inherited by the CLI. The first group would move billing from the
# subscription to an API key or a cloud provider: it is removed unless the app
# was started with `--api-keys`. The second marks a nested session when the app
# is started from inside Claude Code.
BILLING_ENV = [
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
]
NESTED_ENV = [
    'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_CHILD_SESSION',
    'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
    'CLAUDE_CODE_EXECPATH', 'CLAUDE_PID', 'CLAUDE_EFFORT',
]

_UNDEFINED = object()  # a key JavaScript would have left out of the JSON it wrote


def _defined(value):
    """`value` without the keys that are undefined, the way JSON.stringify drops them."""
    if isinstance(value, dict):
        return {key: _defined(item) for key, item in value.items() if item is not _UNDEFINED}
    if isinstance(value, list):
        return [None if item is _UNDEFINED else _defined(item) for item in value]
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _now():
    return int(time.time() * 1000)


def _is_number(value):
    """A finite number, as `typeof n === 'number' && Number.isFinite(n)`."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_vector(value):
    return isinstance(value, list) and len(value) == 3 and all(_is_number(n) for n in value)


def _text(value):
    """String(value ?? '')."""
    return '' if value is None else js_string(value)


def _report_error(err):
    """What console.error(err) was: the error with its trace, in the terminal."""
    traceback.print_exception(type(err), err, err.__traceback__, file=sys.stderr)


def _child_env():
    env = dict(os.environ)
    for name in [*([] if keep_api_keys() else BILLING_ENV), *NESTED_ENV]:
        env.pop(name, None)
    return codex.with_key(env, NAME)


def _capture(args):
    command = codex.resolve(NAME)
    if not command:
        return {'out': '', 'missing': True}
    try:
        done = subprocess.run([command, *args], env=_child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return {'out': '', 'missing': True}
    except OSError:
        return {'out': '', 'missing': False}
    return {'out': done.stdout.decode('utf-8', 'replace'), 'missing': False}


def account_status():
    """Whether Claude Code is installed and signed in with a subscription."""
    captured = _capture(['auth', 'status', '--json'])
    if captured['missing']:
        return {'installed': False, 'loggedIn': False, 'subscription': False, 'authMethod': 'none', 'plan': None}
    info = {}
    try:
        info = json.loads(captured['out'])
    except Exception:
        pass  # treated as signed out
    if not isinstance(info, dict):
        info = {}
    logged_in = info.get('loggedIn') is True
    return {
        'installed': True,
        'loggedIn': logged_in,
        'subscription': logged_in and info.get('authMethod') == 'claude.ai',
        'authMethod': info['authMethod'] if info.get('authMethod') is not None else 'none',
        'plan': info.get('subscriptionType'),
    }


# The CLIs a turn can run on, each with the person's own sign-in: what is
# installed, what is signed in with a plan, and the models that gives.
PROVIDERS = {
    'claude': {'name': 'Claude Code', 'command': 'claude', 'install': 'https://claude.com/claude-code', 'key': 'Anthropic API key', 'status': lambda: account_status(), 'models': lambda: MODELS},
    'codex': {'name': 'Codex', 'command': 'codex', 'install': 'https://developers.openai.com/codex/cli', 'key': 'OpenAI API key', 'status': codex.account_status, 'models': codex.models},
}


def providers():
    """Every provider with its account status, and its models where it can be used."""
    found = []
    for id, provider in PROVIDERS.items():
        account = provider['status']()
        if account['installed'] and codex.typed_key(id):
            # The key typed into the settings is what the CLI will run on: it comes before a plan it is signed in with.
            account = {**account, 'loggedIn': True, 'subscription': False, 'authMethod': 'API key', 'plan': None}
        found.append({
            'id': id, 'name': provider['name'], 'command': provider['command'], 'install': provider['install'],
            **account,
            # Signed in some other way (an API key) counts only when the app was started to allow it.
            'models': provider['models']() if account['subscription'] or (keep_api_keys() and account['loggedIn']) else [],
        })
    return found


def default_model(models):
    """The model a turn runs on when none is asked for."""
    chosen = next((model for model in models if model['id'] == DEFAULT_MODEL), None)
    if chosen is None:
        chosen = next((model for model in models if model.get('tier') == DEFAULT_TIER), None)
    if chosen is None:
        chosen = models[0] if models else None
    return chosen['id'] if chosen is not None and chosen.get('id') is not None else DEFAULT_MODEL


_UNSAFE_IN_NAME = re.compile(r'[^A-Za-z0-9_.\- ()ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]')
_ONLY_DOTS = re.compile(r'\.*')


# A file name safe to write into the turn's attachment folder ("." and ".." are not).
def _sanitize_name(name):
    full = _text(name or 'file')
    base = os.path.basename(full.rstrip('/' if os.sep == '/' else '/\\'))
    # A character beyond the BMP was two units to JavaScript, so it leaves two underscores.
    safe = js_slice(_UNSAFE_IN_NAME.sub(lambda m: '__' if m[0] > '￿' else '_', base), 80)
    return 'file' if _ONLY_DOTS.fullmatch(safe) else safe


def _save_attachments(project, turn_id, files):
    saved = []
    if not files:
        return saved
    dir = os.path.join(project.dir, STATE_DIR, 'attachments', turn_id)
    os.makedirs(dir, exist_ok=True)
    for file in files:
        name = _sanitize_name(file.get('name'))
        data = codex.decode_base64(_text(file.get('data')))
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise UserError(f'The file {name} is too large (limit 6 MB).', 413)
        with open(os.path.join(dir, name), 'wb') as f:
            f.write(data)
        kind = file.get('type')
        if kind in IMAGE_TYPES:
            saved.append({'kind': 'image', 'name': name, 'type': kind, 'data': base64.b64encode(data).decode('ascii'), 'size': len(data)})
        elif (isinstance(kind, str) and kind.startswith('text/')) or TEXT_FILE.search(name):
            saved.append({'kind': 'text', 'name': name, 'text': data.decode('utf-8', 'replace'), 'size': len(data)})
        else:
            saved.append({'kind': 'binary', 'name': name, 'size': len(data)})
    return saved


class _Work:
    """Something started that can be waited for: what a promise was in the Node version."""

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None

    def run(self, fn):
        try:
            self.value = fn()
        except BaseException as err:
            self.error = err
        finally:
            self.done.set()

    def wait(self):
        """The result, once there; an error the work ended with is raised here."""
        self.done.wait()
        if self.error is not None:
            raise self.error
        return self.value


class Turn:
    def __init__(self, project, *, file, creates, text, model, files, tuning=None):
        self.id = str(uuid.uuid4())
        # What Claude Code shows to reach this turn's tools. It ends up in a command
        # line, which other programs can read, so it is not the page's access key.
        self.key = secrets.token_urlsafe(24)
        self.project = project
        self.file = file          # the model this turn works on
        self.creates = creates    # the file did not exist before this turn
        self.has_scans = False
        self.can_look = False
        self.has_others = False   # the project has other models, so the `read` tool is offered
        self.refinements = 0
        self.text = text
        self.model = model
        self.files = files        # dropped files as the page sent them, until they are saved
        self.pick = None          # the spot the person pointed at, if any
        # Overrides for measuring; normal turns use the defaults.
        tuning = tuning if isinstance(tuning, dict) else {}
        self.effort = tuning['effort'] if tuning.get('effort') in EFFORTS else EFFORT
        self.thinking = tuning.get('thinking') is not False and THINKING
        self.attachment_names = [_sanitize_name(file.get('name')) for file in files]
        self.started_at = _now()
        self.phase = 'starting'
        self.reply = ''           # text of the assistant message being streamed
        self.summary = None       # from the last tool call that built
        self.child = None
        self.release = threading.Event()  # set to end the wait for the CLI
        self.lock = threading.RLock()     # guards the fields below against the threads a turn is touched from
        self.via_api_key = False  # Claude Code said it is not using the subscription
        self.stopped = False      # the person pressed stop
        self.finished = False     # a clean build ended the turn early
        self.error = None
        self.failed_builds = 0
        self.tool_calls = 0
        self.live_out = 0         # tokens of the message being written now: an estimate until its real count arrives
        self.tokens_told = 0      # when the page last heard the running count
        # The end of the line of tool work: an event that is set when the work
        # queued last is over (see queue_tool). A build started by the last tool
        # call may still be running when the CLI exits; settling waits for this.
        self.in_tool = threading.Event()
        self.in_tool.set()
        self.early = None         # a change started before its tool call arrived
        # Times are milliseconds since the turn started: Claude Code ready, first
        # token, first tool call begun (thinking over), first tool call received.
        self.stats = {'in': 0, 'out': 0, 'cacheRead': 0, 'cacheWrite': 0, 'requests': 0, 'builds': 0, 'buildMs': 0, 'readyMs': None, 'firstTokenMs': None, 'toolStartMs': None, 'callMs': None, 'previewMs': None}

    def public_state(self):
        return {
            'id': self.id,
            'file': self.file,
            'text': self.text,
            'model': self.model,
            'attachments': self.attachment_names,
            'pick': self.pick,
            'startedAt': self.started_at,
            'phase': self.phase,
            'reply': self.reply,
            'tokens': self.tokens(),
        }

    def tokens(self):
        """Tokens used so far, counted as the finished turn counts them."""
        stats = self.stats
        return stats['in'] + stats['cacheRead'] + stats['cacheWrite'] + stats['out'] + int(self.live_out)

    def tell_tokens(self, now=False):
        """Let open pages follow the count while the turn runs, a few times a second at most."""
        at = _now()
        if not now and at - self.tokens_told < 300:
            return
        self.tokens_told = at
        self.project.emit({'type': 'turn.tokens', 'tokens': self.tokens()})

    def set_phase(self, phase):
        with self.lock:
            if self.phase == phase:
                return
            self.phase = phase
            self.project.emit({'type': 'turn.phase', 'phase': phase})

    def mark(self, stat):
        """Note the time of a first: only the first call for a stat counts."""
        with self.lock:
            if self.stats[stat] is None:
                self.stats[stat] = _now() - self.started_at

    def count(self, stat, amount):
        with self.lock:
            self.stats[stat] += amount

    def queue_tool(self):
        """
        Take the next place in the line of tool work. Returns two events: wait
        for the first before starting (everything queued earlier is then over),
        set the second when done, whatever the outcome.
        """
        with self.lock:
            before = self.in_tool
            self.in_tool = threading.Event()
            return before, self.in_tool


class Agent:
    def __init__(self, *, base_url, models=MODELS):
        self.base_url = base_url
        self.models = models  # what the signed-in CLIs offer
        self.turns = {}       # turn id -> turn, while it runs
        self.limits = None    # latest subscription usage seen, for the page
        self._starting = threading.Lock()  # one request at a time decides whether a project is free

    def allows(self, turn_id, key):
        """Whether an MCP request for the turn `turn_id` carries that turn's key."""
        turn = self.turns.get(turn_id)
        return turn is not None and key is not None and same_secret(key, turn.key)

    def start(self, project, *, file=None, text=None, model=None, files=None, pick=None, tuning=None):
        """Start a turn. Returns at once; progress arrives as project events."""
        if not isinstance(text, str) or not js_trim(text):
            raise UserError('The prompt is empty.')
        if project.turn:
            raise UserError('The previous request is still running.', 409)
        if not self.models:
            raise UserError('No AI model to work with: sign in to Claude Code or Codex first.', 409)
        chosen = model if any(m['id'] == model for m in self.models) else default_model(self.models)
        target = project.target_for(file)
        dropped = [entry for entry in files if isinstance(entry, dict)] if isinstance(files, list) else []
        with self._starting:
            if project.turn:
                raise UserError('The previous request is still running.', 409)
            turn = Turn(project, file=target['file'], creates=target['creates'], text=js_trim(text), model=chosen, files=dropped, tuning=tuning)
            turn.pick = _clean_pick(pick)
            project.turn = turn
            self.turns[turn.id] = turn
        if turn.creates:
            # A project without a model gets one from its first prompt.
            try:
                with open(project.file_path(turn.file), 'w', encoding='utf-8', newline='') as f:
                    f.write('')
                project.set_active(turn.file)
                project.files_changed()
            except BaseException:
                # Nothing runs yet, so nothing would ever end this turn.
                self.turns.pop(turn.id, None)
                project.turn = None
                raise
        project.emit({'type': 'turn', 'turn': turn.public_state()})

        def run():
            try:
                project.exclusive(lambda: self.run(turn))
            except Exception as err:
                _report_error(err)

        threading.Thread(target=run, daemon=True).start()
        return {'turn': turn.id, 'file': turn.file}

    def stop(self, project):
        turn = project.turn
        if not turn:
            return
        with turn.lock:  # the CLI is either not started after this, or stopped by it
            turn.stopped = True
            codex.terminate(turn.child)

    def run(self, turn):
        project = turn.project
        source_before = None
        _rotate_log(project.dir)
        _turn_log(turn, {'event': 'start', 'text': turn.text, 'pick': turn.pick if turn.pick is not None else _UNDEFINED, 'model': turn.model, 'creates': turn.creates or _UNDEFINED})
        try:
            if not turn.creates and git.is_dirty(project.dir):
                project.commit('manual', 'Changes made outside the app')
            source_before = project.source(turn.file)
            build = project.rebuild(turn.file, 'open', quiet=project.state(turn.file)['build'] is not None)
            attachments = _save_attachments(project, turn.id, turn.files)
            turn.files = None  # the base64 payloads are not needed any more
            scans = self.scan_context(turn, build, source_before)
            turn.can_look = (project.builder.info or {}).get('scan') is True  # pictures come from the same module that measures
            turn.has_scans = len(scans) > 0 and turn.can_look
            files = project.files()
            turn.has_others = any(file['name'] != turn.file for file in files)
            siblings = []
            for file in files:
                if file['name'] == turn.file:
                    continue
                try:
                    siblings.append({'name': file['name'], 'source': project.source(file['name'])})
                except Exception:
                    siblings.append({'name': file['name'], 'source': ''})
            content = compose_message(
                file=turn.file,
                source=source_before,
                report=report_for(build),
                journal=project.journal(turn.file),
                files=[file['name'] for file in files],
                siblings=siblings,
                scans=scans,
                pick={**turn.pick, 'face': self.face_at(turn, source_before)} if turn.pick else turn.pick,
                text=turn.text,
                attachments=attachments,
            )
            build_ok = build.get('ok') if isinstance(build, dict) else None
            _turn_log(turn, {'event': 'message', 'text': content[0]['text'], 'images': len(content) - 1, 'build': build_ok})
            if not turn.stopped:
                self.converse(turn, content)
        except Exception as err:
            if isinstance(err, UserError):
                turn.error = err.message
            else:
                turn.error = f'Application error: {err}'
                _report_error(err)
            _turn_log(turn, {'event': 'error', 'message': str(err), 'stack': _UNDEFINED if isinstance(err, UserError) else ''.join(traceback.format_exception(type(err), err, err.__traceback__))})
        with turn.lock:
            last = turn.in_tool
        last.wait()  # a build started by the last tool call may still be running
        self.settle(turn, source_before)

    def face_at(self, turn, source):
        """
        What the picked spot is on: read from the model's own geometry (any
        surface type), or measured on the scan when the spot is on one.
        """
        pick, project = turn.pick, turn.project
        if not pick:
            return None
        try:
            if pick['kind'] == 'scan':
                if (project.builder.info or {}).get('scan') is not True:
                    return None
                return project.builder.scan_measure(project.dir, pick['name'], {'what': 'probe', 'point': pick['point']})
            if not source:
                return None
            return project.builder.face_at(project.dir, turn.file, source, pick['name'], pick['point'])
        except Exception:
            return None

    def scan_context(self, turn, build, source):
        """
        The project's scans as the turn should see them. All are named, with one
        line about their size and the findings noted so far. The full overview and
        a picture come only with the ones the turn starts from: those the model
        reproduces, those the request names and nothing is noted about yet, or
        (for a new model or a request about scans) the first few. Anything else
        the agent looks up itself with `measure`.
        """
        project = turn.project
        all = [scan['name'] for scan in project.scans()]
        if not all:
            return []
        declared = [scan['name'] for scan in ((build or {}).get('scans') or [])]
        # Without the measuring module the agent only learns that the scans exist.
        if (project.builder.info or {}).get('scan') is not True:
            return [{'name': name, 'declared': name in declared, 'unreadable': True} for name in all]
        notes = {name: project.scan_notes(name) for name in all}
        mentioned = turn.text.lower()
        named = [name for name in all if not notes.get(name) and stem_of(name).lower() in mentioned]
        detailed = []  # in order, each once
        for name in [*declared, *named]:
            if name in all and name not in detailed:
                detailed.append(name)
        if not detailed and (not js_trim(source) or ABOUT_SCANS.search(turn.text)):
            for name in [scan for scan in all if not notes.get(scan)][:MAX_SCANS_IN_PROMPT]:
                detailed.append(name)
        found = []
        for name in all:
            scan = {'name': name, 'declared': name in declared, 'notes': notes.get(name)}
            try:
                overview = project.scan_overview(name)
                if name not in detailed:
                    scan = {**scan, 'summary': overview['text'].split('\n', 1)[0]}
                else:
                    scan['text'] = overview['text']
                    if overview.get('views'):
                        with open(overview['views'], 'rb') as f:
                            scan['image'] = base64.b64encode(f.read()).decode('ascii')
            except Exception as err:
                scan['summary'] = f'could not be measured: {err}'
            found.append(scan)
        return found

    def converse(self, turn, content):
        """
        Run the CLI this turn's model belongs to and translate its event stream.
        Returns when the CLI has exited or `turn.release` was set.
        """
        chosen = next((model for model in self.models if model['id'] == turn.model), None)
        if chosen is not None and chosen.get('provider') == 'codex':
            return codex.converse(
                turn, content,
                system=system_prompt_for(scans=turn.has_scans, look=turn.can_look),
                tools={'url': f'{self.base_url}/mcp/{turn.id}', 'key': turn.key},
                cwd=os.path.join(DATA_DIR, 'agent'),
                model=None if chosen.get('unnamed') else chosen['id'],
                log=lambda record: _turn_log(turn, record),
                on_phase=lambda tool: turn.set_phase(PHASE_OF_TOOL.get(tool, 'editing')),
            )
        mcp = {
            'mcpServers': {
                'cad': {
                    'type': 'http',
                    'url': f'{self.base_url}/mcp/{turn.id}',
                    'headers': {'Authorization': f'Bearer {turn.key}'},
                },
            },
        }
        args = [
            '-p',
            '--model', turn.model,
            '--effort', turn.effort,
            '--system-prompt', system_prompt_for(scans=turn.has_scans, look=turn.can_look),
            '--tools', '',
            '--setting-sources', '',
            '--strict-mcp-config',
            '--mcp-config', _json(mcp),
            '--allowedTools', ','.join(f"mcp__cad__{tool['name']}" for tool in tools_for(scans=turn.has_scans, others=turn.has_others, look=turn.can_look)),
            '--disable-slash-commands',
            '--no-session-persistence',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            '--include-partial-messages',
        ]

        try:
            cwd = os.path.join(DATA_DIR, 'agent')
            os.makedirs(cwd, exist_ok=True)
            env = _child_env()
            if not turn.thinking:
                env['MAX_THINKING_TOKENS'] = '0'
            # Only the thread that reads Claude Code's stdout touches these.
            state = {'saw_result': False, 'message_out': 0, 'streaming': None}  # streaming: the tool call being streamed: { index, name, json, started }

            def on_message(msg):
                if not isinstance(msg, dict) or msg.get('parent_tool_use_id') is not None:
                    return
                kind = msg.get('type')
                if kind == 'stream_event':
                    event = msg.get('event') or {}
                    turn.mark('firstTokenMs')
                    what = event.get('type')
                    delta = event.get('delta') or {}
                    streaming = state['streaming']
                    if what == 'content_block_delta':
                        # The real count of a message arrives only at its end; until then its
                        # length gives a fair idea (thinking included), at about 3.5 characters a token.
                        written = delta.get('thinking') or delta.get('text') or delta.get('partial_json') or ''
                        if isinstance(written, str) and written:
                            turn.live_out += len(written) / 3.5
                            turn.tell_tokens()
                    if what == 'message_start':
                        usage = (event.get('message') or {}).get('usage') or {}
                        with turn.lock:
                            turn.stats['requests'] += 1
                            turn.stats['in'] += usage.get('input_tokens') or 0
                            turn.stats['cacheRead'] += usage.get('cache_read_input_tokens') or 0
                            turn.stats['cacheWrite'] += usage.get('cache_creation_input_tokens') or 0
                        state['message_out'] = 0
                        turn.live_out = 0
                        turn.tell_tokens(now=True)
                        if turn.reply:
                            turn.reply = ''
                            turn.project.emit({'type': 'turn.text', 'reset': True})
                    elif what == 'message_delta':
                        out = (event.get('usage') or {}).get('output_tokens')
                        if out is not None:
                            state['message_out'] = out
                            turn.live_out = out
                            turn.tell_tokens(now=True)
                    elif what == 'message_stop':
                        turn.count('out', state['message_out'])
                        state['message_out'] = 0
                        turn.live_out = 0
                    elif what == 'content_block_start':
                        block = event.get('content_block') or {}
                        if block.get('type') == 'thinking':
                            turn.set_phase('thinking')
                        elif block.get('type') == 'tool_use':
                            name = re.sub(r'^mcp__cad__', '', _text(block.get('name')))
                            turn.set_phase(PHASE_OF_TOOL.get(name, 'editing'))
                            turn.mark('toolStartMs')
                            state['streaming'] = {'index': event.get('index'), 'name': name, 'json': '', 'started': False}
                        elif block.get('type') == 'text':
                            turn.set_phase('writing')
                    elif what == 'content_block_delta' and delta.get('type') == 'text_delta' and delta.get('text'):
                        turn.reply += delta['text']
                        turn.project.emit({'type': 'turn.text', 'delta': delta['text']})
                    elif what == 'content_block_delta' and delta.get('type') == 'input_json_delta' and streaming is not None and streaming['index'] == event.get('index') and not streaming['started']:
                        # The change itself comes before the summary: start building now,
                        # so the preview does not wait for the summary to be written.
                        streaming['json'] += delta.get('partial_json') or ''
                        field = CHANGE_FIELD.get(streaming['name'])
                        value = _streamed_value(streaming['json'], field) if field else _UNDEFINED
                        if value is not _UNDEFINED:
                            streaming['started'] = True
                            self.start_early(turn, streaming['name'], {field: value})
                elif kind == 'system' and msg.get('subtype') == 'init':
                    turn.mark('readyMs')
                    if msg.get('apiKeySource') and msg.get('apiKeySource') != 'none':
                        turn.via_api_key = True
                elif kind == 'rate_limit_event':
                    # How much of the subscription's allowance is used, as Claude Code reports it.
                    windows = (msg.get('rate_limit_info') or {}).get('unifiedWindows') or {}

                    def share(window):
                        if not isinstance(window, dict) or not isinstance(window.get('utilization'), (int, float)) or isinstance(window.get('utilization'), bool):
                            return None
                        return {'used': window['utilization'], 'resetsAt': (window.get('resetsAt') or 0) * 1000}

                    limits = {'fiveHour': share(windows.get('five_hour')), 'week': share(windows.get('seven_day')), 'at': _now()}
                    if limits['fiveHour'] or limits['week']:
                        self.limits = limits
                elif kind == 'result':
                    state['saw_result'] = True
                    is_error = msg.get('is_error')
                    _turn_log(turn, {
                        'event': 'result', 'subtype': msg.get('subtype', _UNDEFINED), 'isError': is_error or _UNDEFINED,
                        'result': msg.get('result', _UNDEFINED) if is_error else _UNDEFINED,
                        'apiMs': msg.get('duration_api_ms', _UNDEFINED), 'requests': msg.get('num_turns', _UNDEFINED),
                    })
                    if is_error:
                        turn.error = msg.get('result') or msg.get('subtype') or 'unknown error'

            def on_line(line):
                if not line.strip():
                    return
                try:
                    on_message(json.loads(line))
                except Exception:
                    pass  # not a protocol line

            def on_close(code, sig, stderr):
                try:
                    _turn_log(turn, {
                        'event': 'claude.exit', 'code': code, 'signal': sig if sig is not None else _UNDEFINED,
                        'stderr': stderr.strip() or _UNDEFINED, 'finishedEarly': turn.finished or _UNDEFINED,
                    })
                    if not state['saw_result'] and not turn.finished and not turn.stopped and not turn.error:
                        turn.error = stderr.strip().split('\n')[-1] or f"Claude Code exited with code {'null' if code is None else code}."
                finally:
                    turn.release.set()

            # Under the turn's lock, so a stop cannot fall between the check and the start.
            with turn.lock:
                if turn.stopped:
                    return  # stopped before Claude Code was started
                command = codex.resolve(NAME)
                try:
                    if not command:
                        raise FileNotFoundError(f'spawn {NAME} ENOENT')
                    child = subprocess.Popen([command, *args], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except OSError as err:
                    _turn_log(turn, {'event': 'claude.error', 'message': str(err)})
                    turn.error = 'The claude command was not found.' if isinstance(err, FileNotFoundError) else str(err)
                    return
                turn.child = child

            codex.watch(child, on_line, on_close)
            codex.feed(child, f"{_json({'type': 'user', 'message': {'role': 'user', 'content': content}})}\n")
        except Exception as err:
            # Claude Code could not be started at all: end the turn instead of waiting forever.
            if turn.error is None:
                turn.error = str(err)
            return
        turn.release.wait()

    def tool(self, turn_id, name, input=None):
        """
        A tool call from the agent, arriving over MCP. Returns the text for the
        agent, or `{ finish: true }` when a clean build ends the turn: the call is
        then never answered and Claude Code is stopped, which saves the whole
        request the agent would spend on saying "done".

        Blocks until every tool call (and early change) queued before it is over,
        then runs this one: one at a time, in the order they came.
        """
        turn = self.turns.get(turn_id)
        if not turn or turn.project.turn is not turn:
            return {'text': 'This turn is over.', 'isError': True}
        before, done = turn.queue_tool()
        before.wait()
        try:
            return self.run_tool(turn, name, input if isinstance(input, dict) else {})
        finally:
            done.set()

    def start_early(self, turn, name, input):
        """Begin a tool call's change before the call itself arrives (see converse)."""
        with turn.lock:
            if turn.early or turn.stopped or turn.finished:
                return
            early = {'key': _change_key(name, input), 'before': None, 'work': _Work()}
            before, done = turn.queue_tool()
            turn.early = early

        def change():
            early['before'] = turn.project.source(turn.file)
            outcome = self.apply_change(turn, name, input)
            if outcome.get('clean'):
                turn.set_phase('describing')  # the preview is up; the summary is still being written
            return outcome

        def work():
            before.wait()
            try:
                early['work'].run(change)
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()

    def apply_change(self, turn, name, input):
        """Apply a tool call's change to the turn's model file and build it."""
        project = turn.project
        if turn.stopped or turn.finished:
            return {'failure': 'This turn is over.'}
        if name == 'write':
            code = input.get('code')
            if not isinstance(code, str) or not js_trim(code):
                return {'failure': '`code` must be the full text of the model file.'}
            text = code if code.endswith('\n') else f'{code}\n'
        elif name == 'edit':
            applied = _apply_edits(project.source(turn.file), input.get('edits'))
            if applied.get('error'):
                return {'failure': applied['error']}
            text = applied['text']
        else:
            return {'failure': f'Unknown tool {_text(name)}.'}
        turn.set_phase('building')
        built = project.write_and_build(turn.file, text)
        meta, report = built['meta'], built['report']
        with turn.lock:
            turn.stats['builds'] += 1
            turn.stats['buildMs'] += (meta.get('ms') or {}).get('total') or 0
        clean = bool(meta.get('ok')) and len(checks_failed(meta)) == 0
        if clean:
            turn.mark('previewMs')
        return {'clean': clean, 'report': report, 'fit': scan_fit(meta) if meta.get('ok') else None}

    def read_model(self, turn, input):
        """The `read` tool: the code of another model of the project; changes nothing."""
        project = turn.project
        names = [file['name'] for file in project.files()]
        wanted = js_trim(_text(input.get('file')))
        name = next((item for item in names if item == wanted), None)
        if name is None:
            name = next((item for item in names if item == f'{wanted}.py'), None)
        if not name:
            return {'text': f"No model named {json.dumps(wanted, ensure_ascii=False)} in this project. Models: {', '.join(names)}.", 'isError': True}
        try:
            code = project.source(name)
            return {'text': code if js_trim(code) else '(this model is empty)'}
        except Exception as err:
            return {'text': f'Could not read {name}: {err}', 'isError': True}

    def scan_named(self, turn, wanted):
        """The scan a tool call means, by file name or by its name without `.stl`."""
        names = [scan['name'] for scan in turn.project.scans()]
        asked = _text(wanted)
        name = next((candidate for candidate in names if candidate == asked), None)
        if name is None:
            name = next((candidate for candidate in names if stem_of(candidate) == stem_of(asked)), None)
        if name:
            return {'name': name}
        return {'problem': {'text': f"No scan named {json.dumps(asked, ensure_ascii=False)}. Scans: {', '.join(names) or 'none'}.", 'isError': True}}

    def measure(self, turn, input):
        """The `measure` tool: numbers about a scan; changes nothing."""
        project = turn.project
        found = self.scan_named(turn, input.get('scan'))
        if found.get('problem'):
            return found['problem']
        name = found['name']
        try:
            if input.get('point') is not None:
                point = input['point'] if _is_vector(input['point']) else None
                if not point:
                    return {'text': 'A probe needs `point` as [x, y, z] in mm.', 'isError': True}
                return {'text': project.builder.scan_measure(project.dir, name, {'what': 'probe', 'point': point})}
            if input.get('axis') is None and input.get('normal') is None and input.get('at') is None:
                return {'text': project.scan_overview(name)['text']}
            asked = _text(input.get('axis')).lower()
            axis = next((candidate for candidate in ['x', 'y', 'z'] if candidate == asked), None)
            normal = input['normal'] if _is_vector(input.get('normal')) else None
            if (not axis and not normal) or not _is_number(input.get('at')):
                return {'text': 'A section needs `at` (a number, mm) and either `axis` ("x", "y" or "z") or `normal` ([x, y, z]).', 'isError': True}
            return {'text': project.builder.scan_measure(project.dir, name, {'what': 'section', **({'normal': normal} if normal else {'axis': axis}), 'at': input['at']})}
        except Exception as err:
            return {'text': f'Could not measure {name}: {err}', 'isError': True}

    def look(self, turn, input):
        """The `look` tool: a picture of the model as it builds now, or of a scan."""
        project = turn.project
        direction = input.get('direction')
        if _is_vector(direction) and any(n != 0 for n in direction):
            camera = {'direction': direction}
        else:
            camera = {'view': input['view'] if input.get('view') in VIEWS else 'iso'}
        try:
            if input.get('of') is not None and _text(input['of']) != turn.file:
                found = self.scan_named(turn, input['of'])
                if found.get('problem'):
                    return found['problem']
                subject = {'scan': found['name']}
            else:
                try:
                    source = project.source(turn.file)
                except Exception:
                    source = ''
                if not js_trim(source):
                    return {'text': 'The model is empty: there is nothing to look at yet.', 'isError': True}
                build = project.state(turn.file)['build'] or {}
                declared = [scan['name'] for scan in (build.get('scans') or [])] if input.get('scans') is True else []
                subject = {'file': turn.file, 'source': source, 'scans': declared}
            picture = project.builder.picture(project.dir, subject, camera)
            return {'text': picture['text'], 'image': {'data': base64.b64encode(picture['png']).decode('ascii'), 'mimeType': 'image/png'}}
        except Exception as err:
            return {'text': f'Could not make the picture: {err}', 'isError': True}

    def note(self, turn, input):
        """The `note` tool: one finding about a scan, kept for later turns."""
        found = self.scan_named(turn, input.get('scan'))
        if found.get('problem'):
            return found['problem']
        text = js_trim(_text(input.get('text')))
        if not text:
            return {'text': 'A note needs `text`.', 'isError': True}
        try:
            turn.project.add_scan_note(found['name'], text)
            return {'text': 'Noted.'}
        except Exception as err:
            return {'text': f'Could not save the note: {err}', 'isError': True}

    def run_tool(self, turn, name, input):
        if turn.stopped or turn.finished:
            return {'text': 'This turn is over.', 'isError': True}
        with turn.lock:
            turn.tool_calls += 1
        turn.mark('callMs')
        if name == 'read':
            result = self.read_model(turn, input)
            _turn_log(turn, {'event': 'tool', 'tool': name, 'input': input, 'result': {'isError': result.get('isError', _UNDEFINED), 'chars': js_length(result['text'])}})
            turn.set_phase('thinking')
            return result
        if name in ('measure', 'note', 'look'):
            turn.set_phase(PHASE_OF_TOOL[name])
            result = self.note(turn, input) if name == 'note' else self.look(turn, input) if name == 'look' else self.measure(turn, input)
            _turn_log(turn, {'event': 'tool', 'tool': name, 'input': input, 'result': {
                'text': result['text'], 'isError': result.get('isError', _UNDEFINED),
                'image': len(result['image']['data']) if result.get('image') else _UNDEFINED,
            }})
            turn.set_phase('thinking')
            return result

        # Usually the change was already started while the call was streaming.
        with turn.lock:
            early = turn.early
            turn.early = None
        if early and early['key'] == _change_key(name, input):
            outcome = early['work'].wait()
        else:
            if early:
                try:
                    early['work'].wait()
                except BaseException:
                    pass
                if early['before'] is not None:
                    with open(turn.project.file_path(turn.file), 'w', encoding='utf-8', newline='') as f:
                        f.write(early['before'])
            outcome = self.apply_change(turn, name, input)
        _turn_log(turn, {'event': 'tool', 'tool': name, 'input': input, 'outcome': outcome})
        if outcome.get('failure'):
            return {'text': outcome['failure'], 'isError': True}
        # What the change took from scans is kept, whether or not it builds.
        learned = input.get('learned')
        for fact in learned[:8] if isinstance(learned, list) else []:
            try:
                self.note(turn, fact if fact is not None else {})
            except Exception:
                pass
        if isinstance(input.get('summary'), str) and js_trim(input['summary']):
            turn.summary = js_trim(input['summary'])

        clean, report, fit = outcome.get('clean'), outcome.get('report'), outcome.get('fit')
        # Far from the scan it reproduces: hand the deviation back for another go.
        rough = bool(clean) and fit is not None and fit < GOOD_FIT and turn.refinements < MAX_REFINEMENTS
        if clean and not rough and input.get('more') is not True and not turn.stopped:
            turn.finished = True
            codex.terminate(turn.child)
            turn.release.set()  # no need to wait for Claude Code to exit
            return {'finish': True}
        turn.set_phase('thinking')
        if not clean:
            turn.failed_builds += 1
        if turn.failed_builds >= MAX_FAILED_BUILDS:
            return {'text': f'{report}\n\nToo many failed builds in this turn. Stop calling tools and tell the user plainly what does not work.'}
        if rough:
            turn.refinements += 1
            last = turn.refinements == MAX_REFINEMENTS
            return {
                'text': f'{report}\n\nThe model built, but only {math.floor(fit * 100 + 0.5)}% of the compared points are within tolerance of the scan. '
                + f"Correct the largest differences and call the tool again{' (this is the last round: the next clean build ends the turn)' if last else ''}. "
                + 'If what remains is noise or detail that was not asked for, reply in plain text instead, saying what is left out.',
            }
        return {'text': report}

    def settle(self, turn, source_before):
        """Decide what the turn leaves behind: a commit, a revert, or just an answer."""
        project, file = turn.project, turn.file
        status = 'answer'
        commit = None
        build_error = None
        kept = True  # whether the turn's file still exists afterwards
        try:
            build = project.state(file)['build']
            changed = source_before is not None and project.source(file) != source_before
            if changed and build and build.get('ok'):
                subject = js_slice(turn.text.split('\n')[0], 72)
                commit = project.commit('prompt', subject, paths=[project.tracked(file)], body=turn.text if js_length(turn.text) > js_length(subject) else '')
                status = 'changed'
            elif changed:
                # The model was left broken: every version must build, so go back.
                build_error = (build or {}).get('error')
                with open(project.file_path(file), 'w', encoding='utf-8', newline='') as f:
                    f.write(source_before)
                project.rebuild(file, 'revert')
                status = 'reverted'
            if turn.creates and status != 'changed':
                project.drop_empty(file)
                kept = False
        except Exception as err:
            _report_error(err)
            if turn.error is None:
                turn.error = f'Application error: {err}'

        reply = (turn.summary if turn.finished else js_trim(turn.reply) or turn.summary) or ''
        entry = {
            'kind': 'prompt',
            'file': file if kept else None,
            'text': turn.text,
            'attachments': turn.attachment_names,
            'turn': turn.id if turn.attachment_names else _UNDEFINED,  # where its attachments are kept
            'pick': turn.pick if turn.pick is not None else _UNDEFINED,
            'model': turn.model,
            'reply': reply,
            'status': status,
            'stopped': turn.stopped or _UNDEFINED,
            'error': _UNDEFINED if turn.stopped or turn.error is None else turn.error,
            'buildError': f"{_text(build_error.get('type', _UNDEFINED))}: {_text(build_error.get('message', _UNDEFINED))}" if build_error else _UNDEFINED,
            'viaApiKey': turn.via_api_key or _UNDEFINED,
            'commit': commit,
            'stats': {**turn.stats, 'ms': _now() - turn.started_at, 'tools': turn.tool_calls},
            'limits': self.limits if self.limits is not None else _UNDEFINED,
        }
        _turn_log(turn, {'event': 'done', **entry, 'text': _UNDEFINED})
        self.turns.pop(turn.id, None)
        project.turn = None
        try:
            project.record(_defined(entry), 'turn.done')
        except Exception as err:
            _report_error(err)

    def tools_for(self, turn_id):
        """MCP tool list for the agent in one turn."""
        turn = self.turns.get(turn_id)
        return tools_for(scans=turn is not None and turn.has_scans is True, others=turn is not None and turn.has_others is True, look=turn is not None and turn.can_look is True)


_ROUNDING = decimal.Context(prec=400, rounding=decimal.ROUND_HALF_UP)


def _rounded(value, digits):
    """Number(value.toFixed(digits)) + 0: halves away from zero, no -0, and 5 rather than 5.0."""
    number = float(_ROUNDING.quantize(decimal.Decimal(value), decimal.Decimal(1).scaleb(-digits)))
    if number == 0:
        return 0
    return int(number) if number.is_integer() and abs(number) < 2 ** 53 else number


def _clean_pick(pick):
    """A spot the user pointed at on the model, as the page sent it; None if malformed."""
    if not isinstance(pick, dict) or not _is_vector(pick.get('point')) or not _is_vector(pick.get('normal')):
        return None
    return {
        'name': js_slice(_text(pick.get('name')), 80),
        'kind': pick['kind'] if pick.get('kind') in ('part', 'ref', 'scan') else 'part',
        'point': [_rounded(n, 2) for n in pick['point']],
        'normal': [_rounded(n, 3) for n in pick['normal']],
    }


# The argument of each tool that carries the change itself.
CHANGE_FIELD = {'edit': 'edits', 'write': 'code'}


def _change_key(name, input):
    input = input if isinstance(input, dict) else {}
    if name == 'edit':
        edits = input.get('edits') if isinstance(input.get('edits'), list) else []
        pairs = [[edit.get('old'), edit.get('new')] if isinstance(edit, dict) else [None, None] for edit in edits]
        return f'edit:{_json(pairs)}'
    code = input.get('code')
    return f"{_text(name)}:{'' if code is None else _text(code)}"


_LOG_WRITING = threading.Lock()  # log lines land whole and in the order they were made


def _turn_log(turn, record):
    """One step of a turn, appended to the project's log; the log never stops a turn."""
    try:
        file = os.path.join(turn.project.dir, STATE_DIR, LOG_FILE)
        ts = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        line = f"{json.dumps(_defined({'ts': ts, 'turn': turn.id, 'file': turn.file, **record}), ensure_ascii=False, separators=(',', ':'), default=str)}\n"
        with _LOG_WRITING:
            with open(file, 'a', encoding='utf-8', errors='backslashreplace', newline='') as f:
                f.write(line)
    except Exception:
        pass


def _rotate_log(dir):
    """Keep the log bounded: past the limit it becomes log.1.jsonl, replacing the one before."""
    file = os.path.join(dir, STATE_DIR, LOG_FILE)
    try:
        if os.path.getsize(file) > LOG_MAX_BYTES:
            os.replace(file, re.sub(r'\.jsonl$', '.1.jsonl', file))
    except OSError:
        pass  # no log yet


_JS_SPACE = '\t\n\x0b\x0c\r \xa0                　﻿'


def _streamed_value(text, key):
    """
    The value of one top-level key of a JSON object that is still being streamed,
    as soon as that value is complete; _UNDEFINED until then.
    """
    size = len(text)

    def at(i):
        return text[i] if 0 <= i < size else None  # past the end there is nothing, not an error

    def space(i):
        while i < size and text[i] in _JS_SPACE:
            i += 1
        return i

    def string_end(i):
        i += 1
        while i < size:
            if text[i] == '\\':
                i += 1
            elif text[i] == '"':
                return i + 1
            i += 1
        return -1

    def value_end(i):
        if at(i) == '"':
            return string_end(i)
        if at(i) == '{' or at(i) == '[':
            depth = 0
            while i < size:
                c = text[i]
                if c == '"':
                    i = string_end(i) - 1
                    if i < 0:
                        return -1
                elif c == '{' or c == '[':
                    depth += 1
                elif c == '}' or c == ']':
                    depth -= 1
                    if depth == 0:
                        return i + 1
                i += 1
            return -1
        while i < size:
            if text[i] in ',}' or text[i] in _JS_SPACE:
                return i
            i += 1
        return -1  # a number or literal that may still be growing

    i = space(0)
    if at(i) != '{':
        return _UNDEFINED
    i += 1
    try:
        while True:
            i = space(i)
            if at(i) == ',':
                i = space(i + 1)
            if at(i) != '"':
                return _UNDEFINED
            name_end = string_end(i)
            if name_end < 0:
                return _UNDEFINED
            name = json.loads(text[i:name_end])
            i = space(name_end)
            if at(i) != ':':
                return _UNDEFINED
            i = space(i + 1)
            if i >= size:
                return _UNDEFINED
            end = value_end(i)
            if end < 0:
                return _UNDEFINED
            if name == key:
                return json.loads(text[i:end])
            i = end
    except Exception:
        return _UNDEFINED


def _apply_edits(source, edits):
    """Apply exact-text replacements; all or nothing."""
    if not isinstance(edits, list) or len(edits) == 0:
        return {'error': '`edits` must be a non-empty list of {old, new}.'}
    text = source
    for index, edit in enumerate(edits):
        label = f'edit {index + 1}'
        old = edit.get('old') if isinstance(edit, dict) else None
        new = edit.get('new') if isinstance(edit, dict) else None
        if not isinstance(old, str) or not isinstance(new, str) or old == '':
            return {'error': f'{label}: "old" and "new" must be strings and "old" must not be empty. Nothing was changed.'}
        count = text.count(old)
        if count == 0:
            return {'error': f'{label}: the "old" text does not occur in the file (check whitespace and earlier edits in this call). Nothing was changed. The file is now:\n{js_slice(source, 16_000)}'}
        if count > 1:
            return {'error': f'{label}: the "old" text occurs {count} times; include more surrounding text so it matches once. Nothing was changed.'}
        text = text.replace(old, new, 1)
    if text == source:
        return {'error': 'These edits leave the file unchanged.'}
    return {'text': text}
