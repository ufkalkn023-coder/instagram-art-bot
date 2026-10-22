"""Pure clock derivation shared by schedule models, writers and admission."""
from datetime import datetime, timedelta, timezone

SIX_HOURLY = 'six_hourly_utc17'
LEGACY_SPACING = timedelta(hours=48)
SIX_HOUR_SPACING = timedelta(hours=6)


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Schedule clock must include a timezone')
    return result.astimezone(timezone.utc)


def control_cadence(control):
    if control.get('continuous_approvals'):
        return control['continuous_approvals'][-1]['cadence']
    if control.get('permits'):
        return control['permits'][-1]['cadence']
    return 'daily_utc1717'


def nominal_success(control):
    """A completed same-policy permit can anchor the next nominal six-hour slot."""
    completed = timestamp(control['latest_successful_feed_at'])
    matching = [p for p in control.get('permits', [])
                if p.get('publication_id') == control['latest_successful_feed_id']]
    if len(matching) != 1:
        return None
    permit = matching[0]
    outcome = permit.get('outcome')
    if (permit['cadence'] != SIX_HOURLY or permit['status'] != 'SUCCESS'
            or not outcome or outcome['classification'] != 'SUCCESS'
            or outcome['cleanup_pending'] or outcome['reconciliation_pending']
            or outcome['publication_id'] != control['latest_successful_feed_id']
            or timestamp(outcome['recorded_at']) < completed):
        return None
    slot = timestamp(permit['slot_at'])
    # Keep the derivation for historical one-hour permits immutable. New wider
    # permits use completion+6h after a delayed success to prevent catch-up bursts.
    historical_window = timestamp(permit['expires_at']) <= slot + timedelta(hours=1)
    timely_completion = SIX_HOUR_SPACING if historical_window else timedelta(hours=1)
    if not slot <= completed < slot + timely_completion:
        return None
    return permit


def eligibility_deadline(control):
    completed = timestamp(control['latest_successful_feed_at'])
    if control['schema_version'] == 1 or control_cadence(control) != SIX_HOURLY:
        return completed + LEGACY_SPACING
    permit = nominal_success(control)
    return timestamp(permit['slot_at']) + SIX_HOUR_SPACING if permit else completed + SIX_HOUR_SPACING
