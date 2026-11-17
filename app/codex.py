# The second way to run a turn: the Codex CLI, as installed and signed in by
# the person with their ChatGPT plan. Like with Claude Code, this app never
# sees or stores credentials; it starts the CLI and gives it the turn's tools.
#
# One turn is one `codex exec`: own instructions instead of Codex's, no shell
# and none of its other built-in tools, the person's Codex settings left out.

import base64
import codecs
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time

from . import keys, settings
from .config import DEFAULT_TIER, TIERS, keep_api_keys

from .prompt import js_trim

NAME = 'codex'
# Where the CLIs usually are. A terminal has these in its PATH; an app started
# from the Dock does not get that PATH.
USUAL_PLACES = ['~/.local/bin', '~/.claude/local', '/opt/homebrew/bin', '/usr/local/bin',
                '~/.npm-global/bin', '~/.bun/bin', '~/.volta/bin', '~/bin']
KEY_VARIABLE = 'APP_TURN_KEY'  # the turn's key travels in the environment, not on the command line
# Which of Codex's models stands for each size, most wanted first.
TIER_MODELS = {
    'light': ['gpt-6.1-terra', 'gpt-6-luna'],
    'standard': ['gpt-6.1-sol', 'gpt-6-sol'],
    'strong': ['gpt-6-astra'],
}
# Would move billing from the plan to an API key or another endpoint: removed unless started with `--api-keys`.
STRIPPED_ENV = ['OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID']
# Built-in abilities a turn has no use for: each costs context on every request.
DISABLED = [
    'shell_tool', 'unified_exec', 'apps', 'plugins', 'multi_agent', 'image_generation', 'browser_use', 'computer_use',
    'in_app_browser', 'tool_suggest', 'skill_search', 'goals', 'sleep_tool', 'view_image', 'memories', 'hooks',
]
EFFORT = {'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'high', 'max': 'high'}
IMAGE_SUFFIX = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}


# Where each CLI looks for an API key of its own. `codex exec` takes CODEX_API_KEY for one run.
KEY_ENV = {'claude': ['ANTHROPIC_API_KEY'], 'codex': ['CODEX_API_KEY', 'OPENAI_API_KEY']}


def typed_key(provider):
    """The API key typed into the settings for this CLI, while turns may be billed to a key; '' otherwise."""
    return keys.get(provider) if keep_api_keys() else ''


def with_key(env, provider):
    """The environment for a CLI, with the typed key where that CLI looks for one. It comes before a key found there."""
    key = typed_key(provider)
    for name in (KEY_ENV[provider] if key else []):
        env[name] = key
    return env


def _child_env(extra=None):
    env = {**os.environ, **(extra or {})}
    for name in ([] if keep_api_keys() else STRIPPED_ENV):
        env.pop(name, None)
    return with_key(env, NAME)


# --- starting a CLI and following its output (shared with agent.py) -----------

def find(name):
    """Where a CLI is on this computer when nobody says: on the PATH, else in the usual places. None when nowhere."""
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(os.path.expanduser(place) for place in USUAL_PLACES))


def program_at(path):
    """The full path of the program `path` names, or None when there is none to run there."""
    return shutil.which(os.path.expanduser(path))


def resolve(name):
    """
    The full path of a CLI (`claude` or `codex`), or None when it is not
    installed: the one the settings name (or the CLAUDE_BIN / CODEX_BIN
    variable), else the one found on this computer. On Windows the CLIs npm
    installs are `.cmd` shims, which start only by their full name; looking the
    command up first starts those and real executables alike.
    """
    chosen = settings.get(name) or os.environ.get(f'{name.upper()}_BIN')
    return program_at(chosen) if chosen else find(name)


def exit_of(returncode):
    """A finished child's (code, signal) the way Node reports them: one of the two is None."""
    if returncode is not None and returncode < 0:
        try:
            return None, signal.Signals(-returncode).name
        except ValueError:
            return None, str(-returncode)
    return returncode, None


def terminate(child):
    """Ask a child to stop; one that is already gone is left alone."""
    if child is None:
        return
    try:
        child.terminate()
    except OSError:
        pass


def feed(child, text):
    """Write the child's whole input and close it, without waiting for the child to read it."""
    def write():
        try:
            child.stdin.write(text.encode('utf-8', 'backslashreplace'))
        except (OSError, ValueError):
            pass
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass

    threading.Thread(target=write, daemon=True).start()


def watch(child, on_line, on_close):
    """
    Follow a child on two threads, one per pipe. Each line of its stdout goes
    to `on_line` as soon as it is complete (and what is left when the stream
    ends). When both pipes are drained and the child has exited,
    `on_close(code, signal, stderr)` is called with the end of what it wrote
    to stderr.
    """
    tail = ['']

    def read_stderr():
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                chunk = child.stderr.read1(65536)
                if not chunk:
                    break
                tail[0] = (tail[0] + decoder.decode(chunk))[-2000:]
        except (OSError, ValueError):
            pass

    errors = threading.Thread(target=read_stderr, daemon=True)

    def read_stdout():
        try:
            for raw in child.stdout:  # lines end at "\n" only, as the protocol's do
                on_line(raw.decode('utf-8', 'replace').rstrip('\n'))
        except (OSError, ValueError):
            pass
        errors.join()
        code, sig = exit_of(child.wait())
        on_close(code, sig, tail[0])

    errors.start()
    threading.Thread(target=read_stdout, daemon=True).start()


_NOT_BASE64 = re.compile(r'[^A-Za-z0-9+/]')


def decode_base64(text):
    """Base64 as leniently as Node's Buffer.from(text, 'base64'): either alphabet, padding optional, stray characters skipped."""
    data = str('' if text is None else text).split('=', 1)[0].replace('-', '+').replace('_', '/')
    data = _NOT_BASE64.sub('', data)
    if len(data) % 4 == 1:
        data = data[:-1]
    return base64.b64decode(data + '=' * (-len(data) % 4))


def _capture(args):
    command = resolve(NAME)
    if not command:
        return {'out': '', 'missing': True}
    try:
        done = subprocess.run([command, *args], env=_child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        return {'out': '', 'missing': True}
    except OSError:
        return {'out': '', 'missing': False}
    return {'out': done.stdout.decode('utf-8', 'replace'), 'missing': False}


def account_status():
    """Whether Codex is installed and signed in with a ChatGPT plan."""
    captured = _capture(['login', 'status'])
    out = captured['out']
    if captured['missing']:
        return {'installed': False, 'loggedIn': False, 'subscription': False, 'authMethod': 'none', 'plan': None}
    logged_in = bool(re.search(r'logged in', out, re.I)) and not re.search(r'not logged in', out, re.I)
    chatgpt = logged_in and bool(re.search(r'chatgpt', out, re.I))
    return {
        'installed': True,
        'loggedIn': logged_in,
        'subscription': chatgpt,
        'authMethod': 'ChatGPT' if chatgpt else 'API key' if logged_in else 'none',
        'plan': 'ChatGPT' if chatgpt else None,
    }


def models():
    """The models Codex offers this account, the first few it lists, as the page's choices."""
    out = _capture(['debug', 'models'])['out']
    listed = []
    try:
        found = json.loads(out[out.find('{'):]).get('models')
        listed = found if isinstance(found, list) else []
    except Exception:
        pass  # the default below
    visible = [model for model in listed if isinstance(model, dict) and model.get('visibility') == 'list' and isinstance(model.get('slug'), str)]
    visible.sort(key=lambda model: model.get('priority') if model.get('priority') is not None else 0)
    by_slug = {model['slug']: model for model in visible}
    # One model per size: the first of the preferred ones this account has, else
    # whatever is left in Codex's own order.
    chosen = {}
    for tier in TIERS:
        slug = next((slug for slug in TIER_MODELS.get(tier['id'], []) if slug in by_slug), None)
        if slug:
            chosen[tier['id']] = by_slug[slug]
    left = [model for model in visible if all(model is not taken for taken in chosen.values())]
    for tier in sorted(TIERS, key=lambda tier: tier['id'] != DEFAULT_TIER):
        if tier['id'] not in chosen and left:
            chosen[tier['id']] = left.pop(0)
    offered = [
        {
            'id': chosen[tier['id']]['slug'],
            'label': chosen[tier['id']]['display_name'] if chosen[tier['id']].get('display_name') is not None else chosen[tier['id']]['slug'],
            'hint': tier['hint'],
            'provider': 'codex',
            'tier': tier['id'],
        }
        for tier in TIERS if tier['id'] in chosen
    ]
    # An empty id leaves the choice to Codex.
    return offered if offered else [{'id': 'codex-default', 'label': 'Codex', 'hint': '', 'provider': 'codex', 'tier': DEFAULT_TIER, 'unnamed': True}]


def _now():
    return int(time.time() * 1000)


def converse(turn, content, *, system, tools, cwd, model, log, on_phase):
    """
    Run Codex for a turn and translate its event stream. `content` is the
    message as composed for the turn (a text block, then images); `tools` is
    where the turn's tool server listens.

    Returns when Codex has exited or `turn.release` was set.
    """
    made = []  # files written for this run

    def prepare():
        os.makedirs(cwd, exist_ok=True)
        instructions = os.path.join(cwd, f'instructions-{turn.id}.md')
        with open(instructions, 'w', encoding='utf-8', newline='') as f:
            f.write(system)
        made.append(instructions)
        images = []
        for index, block in enumerate(item for item in content if item.get('type') == 'image'):
            source = block.get('source') or {}
            file = os.path.join(cwd, f"image-{turn.id}-{index}.{IMAGE_SUFFIX.get(source.get('media_type'), 'png')}")
            with open(file, 'wb') as f:
                f.write(decode_base64(source.get('data')))
            made.append(file)
            images.append(file)
        return instructions, images

    def done():
        for file in made:
            try:
                os.remove(file)
            except OSError:
                pass
        turn.release.set()

    try:
        instructions, images = prepare()

        def toml(value):
            return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML string

        args = [
            'exec',
            *(arg for file in images for arg in ('-i', file)),
            '--json', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config', '--ignore-rules',
            '-s', 'read-only',
            *(['-m', model] if model else []),
            '-c', f'model_instructions_file={toml(instructions)}',
            '-c', f"model_reasoning_effort={toml(EFFORT.get(turn.effort, 'low') if turn.thinking else 'low')}",
            '-c', 'approval_policy="never"',
            '-c', 'web_search="disabled"',
            '-c', f"mcp_servers.cad.url={toml(tools['url'])}",
            '-c', f'mcp_servers.cad.bearer_token_env_var={toml(KEY_VARIABLE)}',
            '-c', 'mcp_servers.cad.default_tools_approval_mode="approve"',
            *(arg for feature in DISABLED for arg in ('--disable', feature)),
        ]
        state = {'completed': False, 'failure': None}

        def on_message(msg):
            if not isinstance(msg, dict):
                return
            kind = msg.get('type')
            if isinstance(kind, str) and kind.startswith('item.'):
                turn.mark('firstTokenMs')
            item = msg.get('item') or {}
            if kind == 'thread.started':
                turn.mark('readyMs')
            elif kind == 'item.started' and item.get('type') == 'mcp_tool_call':
                turn.count('requests', 1)  # each tool call is the outcome of one request
                turn.mark('toolStartMs')
                on_phase(item.get('tool'))
            elif kind == 'item.started' and item.get('type') == 'reasoning':
                turn.set_phase('thinking')
            elif kind == 'item.completed' and item.get('type') == 'agent_message' and item.get('text'):
                # Messages arrive whole; the last one is the answer.
                if turn.reply:
                    turn.project.emit({'type': 'turn.text', 'reset': True})
                turn.reply = js_trim(item['text'])
                turn.set_phase('writing')
                turn.project.emit({'type': 'turn.text', 'delta': turn.reply})
            elif kind == 'turn.completed':
                state['completed'] = True
                usage = msg.get('usage') or {}
                cached = usage.get('cached_input_tokens') or 0
                turn.count('requests', 1)  # the request that wrote the answer
                turn.count('in', max(0, (usage.get('input_tokens') or 0) - cached))
                turn.count('cacheRead', cached)
                turn.count('out', usage.get('output_tokens') or 0)
                log({'event': 'result', 'requests': turn.stats['requests']})
            elif kind == 'turn.failed' or kind == 'error':
                error = msg.get('error')
                message = error.get('message') if isinstance(error, dict) else None
                if message is None:
                    message = msg.get('message')
                state['failure'] = message if message is not None else 'unknown error'
                log({'event': 'result', 'isError': True, 'result': state['failure']})

        def on_line(line):
            if not line.strip():
                return
            try:
                on_message(json.loads(line))
            except Exception:
                pass  # not a protocol line

        def on_close(code, sig, stderr):
            try:
                record = {'event': 'codex.exit', 'code': code}
                if sig is not None:
                    record['signal'] = sig
                if stderr.strip():
                    record['stderr'] = stderr.strip()
                if turn.finished:
                    record['finishedEarly'] = True
                log(record)
                if not turn.finished and not turn.stopped and not turn.error:
                    if state['failure']:
                        turn.error = state['failure']
                    elif not state['completed']:
                        turn.error = stderr.strip().split('\n')[-1] or f"Codex exited with code {'null' if code is None else code}."
            finally:
                done()

        # Under the turn's lock, so a stop cannot fall between the check and the start.
        with turn.lock:
            if turn.stopped:
                return done()
            command = resolve(NAME)
            try:
                if not command:
                    raise FileNotFoundError(f'spawn {NAME} ENOENT')
                child = subprocess.Popen(
                    [command, *args], cwd=cwd, env=_child_env({KEY_VARIABLE: tools['key']}),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except OSError as err:
                log({'event': 'codex.error', 'message': str(err)})
                turn.error = 'The codex command was not found.' if isinstance(err, FileNotFoundError) else str(err)
                return done()
            turn.child = child

        watch(child, on_line, on_close)
        feed(child, '\n\n'.join(item['text'] for item in content if item.get('type') == 'text'))
    except Exception as err:
        if turn.error is None:
            turn.error = str(err)
        return done()
    turn.release.wait()
