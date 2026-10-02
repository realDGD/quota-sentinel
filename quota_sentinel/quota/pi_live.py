"""Strict provider metadata normalization; stored snapshots cannot enter here."""
import math
import re
from datetime import datetime
from .models import ProviderQuota, QuotaWindow, QuotaNormalizationError

def _bad():
    raise QuotaNormalizationError('invalid Pi live quota metadata')

def _number(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        _bad()
    return value

def _epoch(value):
    if type(value) is int:
        return value
    if not isinstance(value, str):
        _bad()
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            _bad()
        return int(date.timestamp())
    except (ValueError, OverflowError):
        _bad()

def _window(remaining, reset, captured_at, duration):
    percent = _number(remaining, 100)
    epoch = _epoch(reset)
    if not captured_at-3600 <= epoch <= captured_at+2*duration+86400:
        _bad()
    return QuotaWindow(int(math.floor(percent+0.5)), epoch)

def normalize_pi_live(provider, payload, *, captured_at):
    if type(captured_at) is not int or captured_at < 946684800 or not isinstance(payload, dict):
        _bad()
    try:
        monthly = None
        if provider == 'codex':
            rate = payload['rate_limit']
            windows = []
            for name, duration in (('primary_window', 18000), ('secondary_window', 604800)):
                value = rate[name]
                if type(value['limit_window_seconds']) is not int or value['limit_window_seconds'] != duration:
                    _bad()
                windows.append(_window(100-_number(value['used_percent'],100), value['reset_at'], captured_at, duration))
            five, weekly = windows
        elif provider == 'opencode':
            usage = payload['usage']
            def api(name, duration):
                value = usage[name]
                return _window(100-_number(value['percent'],100), value['resetsAt'], captured_at, duration)
            five, weekly = api('rolling',18000), api('weekly',604800)
            if 'monthly' in usage:
                monthly = api('monthly',2678400)
        elif provider == 'antigravity':
            windows = {}
            for group in payload['groups']:
                label = group['displayName'].strip().lower()
                if label in ('claude', 'claude quota'):
                    continue
                if label not in ('gemini', 'gemini models', 'gemini quota'):
                    _bad()
                for bucket in group['buckets']:
                    window = re.sub(r'[\s_-]', '', bucket['window'].lower())
                    if window in ('5h','5hours','fivehour','fivehours','rolling'):
                        name, duration = 'five', 18000
                    elif window in ('weekly','week','7d','7days','sevenDays'.lower()):
                        name, duration = 'weekly', 604800
                    else:
                        _bad()
                    if name in windows:
                        _bad()
                    fraction = _number(bucket['remainingFraction'],1)
                    windows[name] = _window(100*fraction,bucket['resetTime'],captured_at,duration)
            five, weekly = windows['five'], windows['weekly']
        else:
            _bad()
        return ProviderQuota('Pi · live', True, False, captured_at, five, weekly, monthly)
    except (KeyError, TypeError, AttributeError):
        _bad()
