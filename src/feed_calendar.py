"""Conservative, provisional queue dates; never publication authorization."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.feed_cadence import SIX_HOURLY
from src.feed_schedule import cadence_window, next_window_at_or_after
from src.insights_storage import parse_aware_timestamp

MAX_CALENDAR_CARDS = 24


def build_feed_calendar(schedule, queue, *, now: datetime) -> list[dict]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Calendar time must include a timezone')
    expected = schedule.get('next_format')
    eligible = parse_aware_timestamp(schedule.get('next_eligible_at'))
    attempt = parse_aware_timestamp(schedule.get('next_attempt_at'))
    can_plan = (schedule.get('status') in {'READY', 'WAITING_COOLDOWN', 'WAITING_WINDOW',
                                          'WAITING_FAILURE_BACKOFF'}
                and eligible is not None and expected in {'single', 'carousel'})
    ownership_pending = any(row.get('state') in {'CLAIMED', 'QUARANTINED'} for row in queue)
    candidate = max(value for value in (now, eligible, attempt) if value is not None)
    if schedule.get('cadence') == SIX_HOURLY:
        candidate = next_window_at_or_after(candidate, SIX_HOURLY)
    result = []
    for row in queue:
        if row.get('state') == 'CONSUMED':
            continue
        if len(result) == MAX_CALENDAR_CARDS:
            break
        expiry = parse_aware_timestamp(row.get('expires_at'))
        earliest = None
        if ownership_pending:
            note = 'Sahiplenilmiş veya karantinadaki paket için kontrol gerekiyor'
        elif row.get('state') != 'READY':
            note = 'Paket kontrol bekliyor'
        elif expiry is None:
            note = 'Son kullanma zamanı doğrulanmalı'
        elif expiry <= now:
            note = 'Son kullanma süresi dolmuş'
        elif not can_plan:
            note = 'Yayın planının doğrulanması bekleniyor'
        elif row.get('publication_format') != expected:
            note = 'Bu paketin format sırası bekleniyor'
        elif expiry <= candidate:
            note = 'Olası tarihten önce son kullanma süresi doluyor'
        else:
            earliest = candidate
            note = 'Önceki yayınların başarılı tamamlanmasına bağlı olası alt sınır'
            candidate = (cadence_window(candidate, SIX_HOURLY)[0] + timedelta(hours=6)
                         if schedule.get('cadence') == SIX_HOURLY else candidate + timedelta(hours=48))
            expected = 'carousel' if expected == 'single' else 'single'
        result.append({**row, 'earliest_at': earliest, 'provisional': earliest is not None,
                       'planning_note': note})
    return result
