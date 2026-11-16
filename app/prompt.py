# What the agent is told on every turn. The system prompt is the whole of its
# standing context (Claude Code's own is replaced), so it is written to be
# short: every prompt pays for it again.

import math
import re

SYSTEM_PROMPT = """You are the modeling agent inside a prompt-driven CAD editor. The user describes a change in the chat; you change the parametric model; the app rebuilds it and shows the result in a 3D view beside the chat.

# How a turn works
- You work on one model file (Python, build123d, millimetres). Its name, current text and build status are in the user message. You change only this file and you have no shell. An empty file is a new model: create it with `write`.
- A project can hold more models and scans; `<files>` lists them, each other model with its parameters and functions. When the user refers to another model ("like in the lid", "match the hole spacing of the base", "make it fit the box"), call `read` to see its code, then do what was asked in the open file: your change tools only touch the open file. `@name` in a request names one of the files in `<files>` (a model, or a scan when it ends in `.stl`). Reuse instead of copying: import the other model's functions and constants by file name (`from lid import lid, LID_D`); what it declares with show() stays its own.
- `<recent>` lists the last exchanges and what the user did in between (parameter edits, undo). The file text you are given is always the truth; do not comment on changes that were undone.
- Change it with the tools: `edit` (exact text replacements) or `write` (replace the whole file). Each call rebuilds the model and returns a build report; the 3D view updates by itself.
- Put every edit for one change into a single `edit` call, and call the tool straight away: no prose before it.
- `summary` is what the user reads: a few words in the user's language naming what changed (like a commit subject, at most one short sentence); add an assumption only if you made one that matters. Speak in the first person and never name yourself or the app. Never restate the request, no pleasantries, no code, no file names.
- A call that builds cleanly ends your turn and shows the summary. Pass `more: true` only when you must read the report and then keep working.
- If the report shows an error or a failed check, fix it and call the tool again. After three failed attempts at the same problem, stop and say plainly what does not work.
- A question gets the shortest plain-text answer that settles it (a sentence or two, no headings) and no tool call. If a request is ambiguous, take the most sensible reading, do it, and state the assumption in the summary; ask only when a wrong guess would waste real work.
- The app commits every change to the project's git history and exports the STL after each successful build, and the user can undo any step. You never run git or export anything.

# The model file
It starts with `from build123d import *` and `from system import *`.
1. Parameters first. Every tunable number is an UPPER_CASE constant at the top with a short comment: `GAP = 7.35  # gap between the lugs`. The app shows these as editable fields, so never bury a dimension inside a function. Values computed from them go below. Group them under `# --- Section` comment lines.
2. Pure part functions. Each returns a Part built at a canonical pose and reads nothing but the constants.
3. At the bottom, declare what exists:
   show(shape, "name")                 printed part: exported to STL, drawn solid
   ref(shape, "name", at=Pos(x, y, z))  reference geometry (screws, mating parts): drawn ghosted, not exported
   check_clear("a", "b")               the two parts must not overlap
   check_overlap("a", "b", why="...")  they must overlap (a thread biting in)
   check_gap("a", "b", min=0.2)        minimum distance in mm
   dim("label", (x, y, z), (x, y, z))  a dimension line in the 3D view. The view already shows the overall size and every hole diameter; add dim() only for other things the user cares about (a wall, a spacing, a gap)
Older files use Assembly().with_part() / assert_*(), build_fit() and build_printable() instead; keep that style when editing them unless asked to convert.
Keep names and comments in the language the file already uses. Z is up; printed parts sit on z = 0 unless the file says otherwise.

# build123d notes
- Algebra mode: `+` fuse, `-` cut, `&` intersect, `Pos(x, y, z) * shape`, `Rot(rx, ry, rz) * shape`, `extrude(sketch, amount=h)`, `Plane.XZ * sketch`. Primitives are centred; `align=(Align.CENTER, Align.CENTER, Align.MIN)` puts one on z = 0.
- Work in 2D first: build the outline as a sketch (`Rectangle`, `Circle`, `Polygon`, `make_hull`, `fillet(sketch.vertices(), r)`) and extrude it. That is far more robust than 3D fillets.
- `fillet(edges, r)` and `chamfer(edges, d)` fail with "command not done" when the size is too large or an edge chain ends on a tangent edge. Select edges narrowly (`part.edges().filter_by(Axis.Z)`, `.group_by(Axis.Z)[-1]`) and start smaller.
- A cut that removes nothing is silent: the report says when the shape did not change.
- A boolean can return several solids; take `.solid()` where one Solid is needed and `assert len(part.solids()) == 1` for a part that must be one piece.
- Holes: `Cylinder(d / 2, h)`; a horizontal one is `Rot(0, 90, 0) * Cylinder(...)`. Make cutters longer than the material they pass through."""

# Added only in turns that can take pictures.
LOOK_PROMPT = """

# Seeing
`look` returns a picture of the model as it now builds (or of a scan), from a named side or any direction. The build report and the code are usually enough; look when shape or position cannot be settled from them: an unfamiliar model, "the left one", a change that is easy to get wrong in space. One picture from the right side beats several; never look after every edit."""

# Added to the system prompt only in turns whose scans can be measured.
SCAN_PROMPT = """

# Scans
Scans are triangle meshes (STL) the user put in the project: real objects, or parts made elsewhere. They are read-only references; `<files>` lists them with what is already known about each.
- When the request refers to one ("the same diameter as in the arm"), take the number from its notes if it is there. Otherwise find it with `measure`: the overview first (size, flat faces, round holes and bosses with their axes and diameters), then a section or a probe only where the overview does not settle it. Measure what the request needs, nothing more.
- Every number you take from a scan, whether from its overview, a section or a probe, is remembered: list it in `learned` of the `edit` or `write` call that uses it, or call `note` when you change nothing. One fact each, in words that will still make sense later: what the feature is, where it is, its size ("pivot bore: axis Y through x=12 z=40, diameter 8.1, read as 8"). Notes come back with every later request in place of the overview, so nothing is looked up twice.
- Scans are noisy and printed parts shrink: round to sensible values (10.02 is 10, 2.97 is 3) and put the value in the model as a named parameter with a comment saying where it comes from.
- Reproducing a scan as a model: recover the construction, not the surface. Decide which simple features the object is made of and build those where the scan is, in the same position and orientation; declare `scan("name.stl")` at the bottom of the file, and every build then reports how far the model is from the scan, both ways. Correct the largest differences first, stop when what remains is noise, and say in the summary what you left out. Never rebuild the mesh point by point."""

MORE_HINT = 'true: return the report and let me continue instead of ending the turn on success.'

VECTOR = {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3}

MEASURE_TOOL = {
    'name': 'measure',
    'description': 'Measure a scan; changes nothing. Only `scan`: its overview (size, flat faces, round holes and bosses, a few sections). With `at` and `axis` or `normal`: the outline where that plane cuts it (closed loops, circles, sizes). With `point`: the surface nearest that point (flat, round with axis and diameter, wall thickness there).',
    'inputSchema': {
        'type': 'object',
        'properties': {
            'scan': {'type': 'string', 'description': 'File name of the scan.'},
            'axis': {'type': 'string', 'enum': ['x', 'y', 'z'], 'description': 'Section: the plane is perpendicular to this axis.'},
            'normal': {**VECTOR, 'description': 'Section: the normal of the plane, instead of `axis`, for a tilted cut.'},
            'at': {'type': 'number', 'description': 'Section: position of the plane along the axis or normal, mm.'},
            'point': {**VECTOR, 'description': 'Probe: a point on or near the surface, mm.'},
        },
        'required': ['scan'],
    },
}

LOOK_TOOL = {
    'name': 'look',
    'description': 'A picture of the model as it builds now, or of a scan. Changes nothing.',
    'inputSchema': {
        'type': 'object',
        'properties': {
            'of': {'type': 'string', 'description': 'File name of a scan to look at. Leave out for the model.'},
            'view': {'type': 'string', 'enum': ['iso', 'top', 'bottom', 'front', 'back', 'left', 'right'], 'description': 'A named side. Default iso.'},
            'direction': {**VECTOR, 'description': 'Instead of `view`: the direction to look along, e.g. [0, 1, -0.5].'},
            'scans': {'type': 'boolean', 'description': 'For the model: also draw the project\'s scans it declares, in a second tone.'},
        },
    },
}

NOTE_TOOL = {
    'name': 'note',
    'description': 'Remember one fact you measured about a scan, for later requests. One short line.',
    'inputSchema': {
        'type': 'object',
        'properties': {
            'scan': {'type': 'string', 'description': 'File name of the scan.'},
            'text': {'type': 'string', 'description': 'The finding: what, where, how big.'},
        },
        'required': ['scan', 'text'],
    },
}

READ_TOOL = {
    'name': 'read',
    'description': 'The full code of another model of this project, read-only. Use it when the request refers to that model or you need a size or shape from it.',
    'inputSchema': {
        'type': 'object',
        'properties': {'file': {'type': 'string', 'description': 'File name as listed in <files>, e.g. lid.py'}},
        'required': ['file'],
    },
}

CHANGE_TOOLS = [
    {
        'name': 'edit',
        'description': 'Replace exact text in the model file, then rebuild. Each `old` must occur exactly once: include enough surrounding text.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'edits': {
                    'type': 'array',
                    'minItems': 1,
                    'items': {
                        'type': 'object',
                        'properties': {'old': {'type': 'string'}, 'new': {'type': 'string'}},
                        'required': ['old', 'new'],
                    },
                },
                'summary': {'type': 'string', 'description': 'For the user: what changed, a few words, their language.'},
                'more': {'type': 'boolean', 'description': MORE_HINT},
            },
            'required': ['edits', 'summary'],
        },
    },
    {
        'name': 'write',
        'description': 'Replace the whole model file, then rebuild. For a new model or a rewrite; otherwise prefer `edit`.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'code': {'type': 'string'},
                'summary': {'type': 'string', 'description': 'For the user: what changed, a few words, their language.'},
                'more': {'type': 'boolean', 'description': MORE_HINT},
            },
            'required': ['code', 'summary'],
        },
    },
]

# The tools of a turn; measuring exists only where there is something to measure, looking where pictures can be made.
# With scans in play a change can carry what it took from them, so the finding
# is kept without a call of its own.
LEARNED = {
    'type': 'array',
    'description': 'Every number this change takes from a scan, to remember for later requests.',
    'items': {
        'type': 'object',
        'properties': {
            'scan': {'type': 'string', 'description': 'File name of the scan.'},
            'text': {'type': 'string', 'description': 'What, where, how big: "mounting holes: 3 × ⌀4.5 through the base".'},
        },
        'required': ['scan', 'text'],
    },
}
CHANGE_TOOLS_WITH_SCANS = [
    {**tool, 'inputSchema': {**tool['inputSchema'], 'properties': {**tool['inputSchema']['properties'], 'learned': LEARNED}}}
    for tool in CHANGE_TOOLS
]


def tools_for(*, scans, others, look):
    return [
        *(CHANGE_TOOLS_WITH_SCANS if scans else CHANGE_TOOLS),
        *([READ_TOOL] if others else []),
        *([LOOK_TOOL] if look else []),
        *([MEASURE_TOOL, NOTE_TOOL] if scans else []),
    ]


def system_prompt_for(*, scans, look):
    return SYSTEM_PROMPT + (LOOK_PROMPT if look else '') + (SCAN_PROMPT if scans else '')


TEXT_ATTACHMENT_LIMIT = 30_000
RECENT_TURNS = 4
RECENT_LINES = 16
OUTLINE_ITEMS = 14  # parameters, and functions, named per other model in <files>

# --- text exactly as JavaScript made it ---------------------------------------
# The texts below were composed by JavaScript and must come out the same byte
# for byte, so whitespace, string lengths and numbers follow its rules here.

_UNDEFINED = object()  # a key that is not there, where JavaScript printed "undefined"

# What `\s` and trim() mean in JavaScript (Python's own idea of whitespace differs slightly).
JS_SPACE = '\t\n\x0b\x0c\r \xa0  -     　﻿'
_SPACES = re.compile(f'[{JS_SPACE}]+')
_TRIM = re.compile(f'^[{JS_SPACE}]+|[{JS_SPACE}]+$')
# Line ends as JavaScript's `^`, `$` and `.` see them.
_LINE_START = r'(?:^|(?<=[\r  ]))'
_LINE_END = r'(?=[\n\r  ]|\Z)'
_NOT_LINE_END = r'[^\n\r  ]'


def js_trim(text):
    """String.prototype.trim()."""
    return _TRIM.sub('', text)


def js_length(text):
    """The length of a JavaScript string: UTF-16 units, so a character beyond the BMP counts as two."""
    return len(text) + sum(1 for char in text if char > '￿')


def js_slice(text, end):
    """text.slice(0, end), counted in UTF-16 units; a character the cut would split is left out."""
    if len(text) <= end and js_length(text) <= end:
        return text
    units = 0
    for index, char in enumerate(text):
        units += 2 if char > '￿' else 1
        if units > end:
            return text[:index]
    return text


def js_number(value):
    """A number as JavaScript prints it: 5 for 5.0, no exponent between 1e-6 and 1e21."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return 'NaN'
    if math.isinf(value):
        return 'Infinity' if value > 0 else '-Infinity'
    if value == 0:
        return '0'
    shortest = repr(abs(value))  # the same shortest digits that round-trip
    mantissa, _, exponent = shortest.partition('e')
    whole, _, fraction = mantissa.partition('.')
    digits = whole + fraction
    point = len(whole) + (int(exponent) if exponent else 0)  # the value is 0.<digits> * 10 ** point
    stripped = digits.lstrip('0')
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip('0')
    count = len(digits)
    sign = '-' if value < 0 else ''
    if count <= point <= 21:
        return sign + digits + '0' * (point - count)
    if 0 < point <= 21:
        return f'{sign}{digits[:point]}.{digits[point:]}'
    if -6 < point <= 0:
        return f'{sign}0.{"0" * -point}{digits}'
    power = point - 1
    return f'{sign}{digits[0]}{"." + digits[1:] if count > 1 else ""}e{"+" if power >= 0 else "-"}{abs(power)}'


def js_string(value):
    """String(value), as a template literal would put it into a text."""
    if isinstance(value, str):
        return value
    if value is _UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    if isinstance(value, (bool, int, float)):
        return js_number(value)
    if isinstance(value, (list, tuple)):
        return ','.join('' if item is None else js_string(item) for item in value)
    return '[object Object]'


def _clip(text, limit):
    flat = js_trim(_SPACES.sub(' ', js_string('' if text is None or text is _UNDEFINED else text)))
    return f'{js_slice(flat, limit)}…' if js_length(flat) > limit else flat


# The last few exchanges and what the user did in between, oldest first.
def _recent_lines(journal):
    prompts = [entry for entry in journal if entry.get('kind') == 'prompt']
    if prompts:
        first = prompts[-RECENT_TURNS] if len(prompts) >= RECENT_TURNS else prompts[0]
        start = next(index for index, entry in enumerate(journal) if entry is first)
    else:
        start = len(journal)
    lines = []
    for entry in journal[start:]:
        kind = entry.get('kind')
        if kind == 'prompt':
            outcome = ' [it did not build, so it was rolled back]' if entry.get('status') == 'reverted' else ' [the user stopped it]' if entry.get('stopped') else ''
            lines.append(f"user: {_clip(entry.get('text'), 300)}")
            lines.append(f"you: {_clip(entry.get('reply'), 300) or '(no reply)'}{outcome}")
        elif kind == 'param':
            lines.append(f"[the user set {js_string(entry.get('name', _UNDEFINED))}: {js_string(entry.get('from', _UNDEFINED))} -> {js_string(entry.get('to', _UNDEFINED))} in the parameters panel]")
        elif kind == 'undo':
            lines.append('[the user undid the last change]')
        elif kind == 'redo':
            lines.append('[the user brought the undone change back]')
        elif kind == 'restore':
            lines.append(f"[the user went back to an earlier version: \"{_clip(entry.get('subject'), 80)}\"]")
        elif kind == 'scan.add':
            lines.append(f"[the user uploaded the scan {js_string(entry.get('name', _UNDEFINED))}]")
        elif kind == 'scan.remove':
            lines.append(f"[the user removed the scan {js_string(entry.get('name', _UNDEFINED))}]")
    return lines[-RECENT_LINES:]


_PARAM = re.compile(
    rf'{_LINE_START}([A-Z][A-Z0-9_]*)[{JS_SPACE}]*=[{JS_SPACE}]*([^#\n]+?)[{JS_SPACE}]*(?:#{_NOT_LINE_END}*)?{_LINE_END}',
    re.M,
)
_DEF = re.compile(rf'{_LINE_START}def[{JS_SPACE}]+([A-Za-z_][A-Za-z0-9_]*)[{JS_SPACE}]*\(', re.M)


def _outline(source):
    """
    What another model offers, in one line: its parameters and its functions.
    Enough to know what the user means by "like in the lid" and what to read.
    """
    params = [f'{m[1]}={_clip(m[2], 24)}' for m in _PARAM.finditer(source)]
    defs = [f'{m[1]}()' for m in _DEF.finditer(source)]

    def cut(items, most):
        return [*items[:most], f'… {len(items) - most} more'] if len(items) > most else items

    found = [
        f"parameters {', '.join(cut(params, OUTLINE_ITEMS))}" if params else None,
        f"functions {', '.join(cut(defs, OUTLINE_ITEMS))}" if defs else None,
    ]
    return '; '.join(item for item in found if item) or 'no parameters or functions'


def _files_text(file, files, siblings, scans):
    lines = [f'this file: {file}']
    others = [name for name in files if name != file]
    if others:
        lines.append('other models (read one with the `read` tool when the request refers to it):')
        for name in others:
            sibling = next((item for item in siblings if item['name'] == name), None)
            lines.append(f"- {name}: {_outline(sibling['source']) if sibling else 'not readable'}")
    if scans:
        lines.append('scans (read-only meshes):')
        for scan in scans:
            about = '; '.join(item for item in [
                'this file reproduces it' if scan.get('declared') else None,
                scan.get('summary'),
                'overview above' if scan.get('text') else None,
            ] if item)
            lines.append(f"- {scan['name']}{f': {about}' if about else ''}")
            notes = scan.get('notes')
            for note in ('' if notes is None else notes).split('\n'):
                if note:
                    lines.append(f'  {note}')
        if any(scan.get('unreadable') for scan in scans):
            lines.append('(you cannot measure scans yet, so say so if asked about one, and work from dimensions the user gives)')
    return '\n'.join(lines)


# The picture of the view around a picked spot, as the page names it.
PICK_VIEW = 'widok-wskazanego-miejsca.jpg'


def _pick_text(pick, view=None):
    kind = pick.get('kind')
    name = js_string(pick.get('name', _UNDEFINED))
    what = f'the scan {name}' if kind == 'scan' else f'the reference part "{name}"' if kind == 'ref' else f'the part "{name}"'
    axes = ['X', 'Y', 'Z']
    normal = pick['normal']
    # The axis the normal points along most; of equals the first, as a stable sort leaves it.
    main = max(range(len(normal)), key=lambda i: (abs(normal[i]), -i))
    facing = f" (a face looking {'+' if normal[main] > 0 else '-'}{axes[main]})" if abs(normal[main]) > 0.95 else ''
    return (
        f"The user right-clicked a point on {what} at ({', '.join(js_string(n) for n in pick['point'])}) mm, surface normal ({', '.join(js_string(n) for n in normal)}){facing}. "
        + (f"The surface there is {js_string(pick['face'])}. " if pick.get('face') else '')
        + (f'The attached picture {view} is the part of the view the user was looking at, with the spot marked by a soft round shade. ' if view else '')
        + 'Words like "here", "this", "tu", "to", "ten", "ta" in the request refer to that spot and the feature of the model it belongs to: work out from the code which feature made that surface, and change that one. '
        + 'If it is one of several identical features, change only it, giving it its own parameter, unless the request says all of them. Say in the summary which feature you took it to be.'
    )


def compose_message(*, file, source, report, journal, files=(), siblings=(), scans=(), pick=None, text, attachments):
    """
    The user message of a turn: the scans in play, the model as it is now, how
    it builds, the last few exchanges (turns do not share a session, so this is
    the only memory), attachments, and the request itself.

    `files` are the names of the project's models; `scans` are
    [{ name, declared, unreadable?, summary?, notes?, text?, image? }]: every
    scan comes with what was noted about it, and the ones the turn is about
    also with their overview and a picture.
    """
    parts = []
    # What never changes goes first, so the start of the message can be cached.
    for scan in scans:
        if scan.get('text'):
            parts.append(f"<scan name=\"{scan['name']}\">\n{scan['text']}\n</scan>")
    if len(files) > 1 or len(scans):
        parts.append(f'<files>\n{_files_text(file, files, siblings, scans)}\n</files>')
    body = source.rstrip('\n') if js_trim(source) else '(empty: no code yet)'
    parts.append(f'<{file}>\n{body}\n</{file}>')
    parts.append(f'<build>\n{report}\n</build>')

    recent = _recent_lines(journal)
    if recent:
        parts.append('<recent>\n' + '\n'.join(recent) + '\n</recent>')

    images = [
        {'label': f"{scan['name']} (the scan from four sides: isometric, top, front, right)", 'type': 'image/png', 'data': scan['image']}
        for scan in scans if scan.get('image')
    ]
    for attachment in attachments:
        if attachment['kind'] == 'text':
            content = f"{js_slice(attachment['text'], TEXT_ATTACHMENT_LIMIT)}\n[cut]" if js_length(attachment['text']) > TEXT_ATTACHMENT_LIMIT else attachment['text']
            parts.append(f"<attachment name=\"{attachment['name']}\">\n{content}\n</attachment>")
        elif attachment['kind'] == 'image':
            images.append({'label': attachment['name'], 'type': attachment['type'], 'data': attachment['data']})
        else:
            parts.append(f"<attachment name=\"{attachment['name']}\">(binary file, {js_string(attachment['size'])} bytes: its content is not readable here)</attachment>")
    if images:
        parts.append(f"Attached images, in order: {'; '.join(image['label'] for image in images)}")
    if pick:
        view = next((image['label'] for image in images if image['label'] == PICK_VIEW), None)
        parts.append(f'<picked>\n{_pick_text(pick, view)}\n</picked>')
    parts.append(f'<request>\n{text}\n</request>')

    return [
        {'type': 'text', 'text': '\n\n'.join(parts)},
        *({'type': 'image', 'source': {'type': 'base64', 'media_type': image['type'], 'data': image['data']}} for image in images),
    ]
