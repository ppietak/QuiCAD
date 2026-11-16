# A build record as text for the agent: every line earns its tokens.

import math
from decimal import ROUND_HALF_UP, Decimal

_MISSING = object()


def _js_number(value):
    """A number as JavaScript's String(value) writes it: 5, 0.1, 1e-7, 1e+21."""
    if isinstance(value, int) and abs(value) < 10 ** 21:
        return str(value)
    value = float(value)
    if math.isnan(value):
        return 'NaN'
    if math.isinf(value):
        return 'Infinity' if value > 0 else '-Infinity'
    if value == 0:
        return '0'
    sign = '-' if value < 0 else ''
    # The shortest digits that read back as this number, and where the point goes.
    mantissa, _, exponent = repr(abs(value)).partition('e')
    whole, _, fraction = mantissa.partition('.')
    if not exponent and fraction == '0':
        fraction = ''
    digits = (whole + fraction).lstrip('0')
    point = len(whole) + (int(exponent) if exponent else 0) - (len(whole + fraction) - len(digits))
    digits = digits.rstrip('0') or '0'
    count = len(digits)
    if count <= point <= 21:
        return sign + digits + '0' * (point - count)
    if 0 < point <= 21:
        return sign + digits[:point] + '.' + digits[point:]
    if -6 < point <= 0:
        return sign + '0.' + '0' * -point + digits
    power = point - 1
    return sign + digits[0] + ('.' + digits[1:] if count > 1 else '') + 'e' + ('+' if power >= 0 else '-') + str(abs(power))


def _fixed(value, digits):
    """JavaScript's value.toFixed(digits): the exact value, halves rounded away from zero."""
    if abs(value) >= 1e21:
        return _js_number(value)
    rounded = Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    text = format(rounded, 'f')
    if rounded == 0 and not (value < 0):
        text = text.lstrip('-')
    return text


def _text(value):
    """A value inside a line of text, as a JavaScript template would spell it."""
    if value is _MISSING:
        return 'undefined'
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, list):
        return ','.join('' if item is None else _text(item) for item in value)
    if isinstance(value, dict):
        return '[object Object]'
    return str(value)


def _num(value, digits=2):
    return _js_number(float(_fixed(value, digits)))


def _size(bbox):
    return [bbox[1][i] - bbox[0][i] for i in range(3)]


def _holes_text(holes=None):
    if not holes:
        return ''
    by_diameter = {}
    for hole in holes:
        by_diameter[hole['d']] = by_diameter.get(hole['d'], 0) + 1
    items = [f'{_num(d)} x{count}' if count > 1 else _num(d) for d, count in by_diameter.items()]
    return f", holes dia {', '.join(items)}"


def _error_text(meta):
    error = meta.get('error')
    if error is None:
        error = {'type': 'Error', 'message': 'unknown'}
    line = error.get('line')
    lines = [f"ERROR{f' line {_text(line)}' if line else ''}: {_text(error.get('type', _MISSING))}: {_text(error.get('message', _MISSING))}"]
    if error.get('text'):
        lines.append(f"  {_text(error.get('line', _MISSING))} | {error['text']}")
    stack = error.get('stack')
    if stack is not None and len(stack) > 1:
        lines.append(f"  via {' > '.join(f'{_text(fn)}:{_text(line)}' for line, fn in stack)}")
    if meta.get('output'):
        lines.append(f"printed: {meta['output'][-400:]}")
    return '\n'.join(lines)


def _delta_text(meta, previous):
    """What changed in the printed parts between two successful builds."""
    if not previous or not previous.get('ok'):
        return []
    before = {p['name']: p for p in previous['parts'] if p['kind'] == 'show'}
    shown = [p for p in meta['parts'] if p['kind'] == 'show']
    lines = []
    any_change = False
    for part in shown:
        old = before.get(part['name'])
        if not old:
            any_change = True
            continue
        changes = []
        dv = part['volume'] - old['volume']
        if abs(dv) > 1e-4:
            changes.append(f"vol {_num(old['volume'], 1)} -> {_num(part['volume'], 1)} ({'+' if dv > 0 else ''}{_num(dv, 1)})")
        a = _size(old['bbox'])
        b = _size(part['bbox'])
        for i, axis in enumerate('XYZ'):
            if abs(a[i] - b[i]) > 1e-4:
                changes.append(f'{axis} {_num(a[i])} -> {_num(b[i])}')
        if changes:
            any_change = True
            lines.append(f"changed {part['name']}: {', '.join(changes)}")
    if len(before) != len(shown):
        any_change = True
    if not any_change:
        lines.append('NOTE: the printed shape is identical to the previous build (same size and volume).')
    return lines


def checks_failed(meta):
    return [check for check in meta['checks'] if not check['ok']] if meta.get('ok') else []


def scan_fit(meta):
    """
    How well a build matches the scans it declares: the smaller of the shares of
    points within tolerance, both ways, over every scan. None when it declares none.
    """
    shares = []
    for scan in meta.get('scans') or []:
        deviation = scan.get('deviation')
        if deviation:
            shares.append((deviation.get('scan_to_model') or {}).get('within'))
            shares.append((deviation.get('model_to_scan') or {}).get('within'))
    known = [share for share in shares if isinstance(share, (int, float)) and not isinstance(share, bool)]
    return min(known) if known else None


def report_for(meta, previous=None):
    if meta.get('empty'):
        return 'EMPTY: this file has no code yet. Create the model with `write`.'
    if not meta.get('ok'):
        return _error_text(meta)
    failed = checks_failed(meta)
    lines = ['BUILT, BUT CHECKS FAILED' if failed else f"OK {_fixed(meta['ms']['total'] / 1000, 2)}s"]
    refs = []
    for part in meta['parts']:
        if part['kind'] != 'show':
            refs.append(part['name'])
            continue
        x, y, z = _size(part['bbox'])
        line = f"{part['name']}: {_num(x)} x {_num(y)} x {_num(z)} mm, vol {_num(part['volume'], 1)} mm3"
        if part.get('solids') != 1:
            line += f", {_text(part.get('solids', _MISSING))} SOLIDS"
        if not part.get('valid'):
            line += ', INVALID SOLID'
        lines.append(line + _holes_text(part.get('holes')))
    if refs:
        lines.append(f"refs: {', '.join(refs)}")
    if meta['checks']:
        lines.append(f"checks: {len(meta['checks']) - len(failed)}/{len(meta['checks'])} ok")
        for check in failed:
            lines.append(f"- FAILED {_text(check.get('name', _MISSING))} ({_text(check.get('kind', _MISSING))} "
                         f"{_text(check.get('a', _MISSING))}/{_text(check.get('b', _MISSING))}): {_text(check.get('detail', _MISSING))}")
    for scan in meta.get('scans') or []:
        if scan.get('text'):
            lines.append(scan['text'])
    lines.extend(_delta_text(meta, previous))
    return '\n'.join(lines)
