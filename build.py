"""Fetch Energinet day-ahead prices for a static mirror (standard library only)."""

import argparse
from datetime import UTC, datetime, timedelta
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen


ENDPOINT = 'https://api.energidataservice.dk/dataset/DayAheadPrices'
SUPPORTED = ('energinet:DK1', 'energinet:DK2')
# Four days of quarter hours for two zones is 768 records; a reply this long was cut.
LIMIT = 1000
KIND = 'wholesale day-ahead price (not what a household pays: delivery, fees and taxes are not in it)'
TERMS = {
    'licence': 'CC BY 4.0',
    'commercial_reuse': 'yes',
    'personal_only': 'not confirmed',
    'needs_key': False,
    'direct_calls': 'asks not to',
    'note': 'Distributed applications should use a mirror. Retain Energinet attribution.',
    'checked': '2026-11-19',
}
ATTRIBUTION = ('Energinet (https://www.energidataservice.dk/); CC BY 4.0 '
               '(https://creativecommons.org/licenses/by/4.0/); '
               'prices converted to per kWh and UTC intervals')


def decode_reply(body, zones):
    """Validate one reply and return its intervals by zone, before any file is derived."""
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get('records'), list):
        raise ValueError('reply must contain a records array')
    records = payload['records']
    if len(records) >= LIMIT:
        raise ValueError('reply may have reached the request limit')
    by_zone = {zone: [] for zone in zones}
    for record in records:
        if not isinstance(record, dict) or record.get('PriceArea') not in by_zone:
            raise ValueError('record has wrong or missing PriceArea')
        stamp = record.get('TimeUTC')
        if not isinstance(stamp, str):
            raise ValueError('record lacks TimeUTC')
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        start = parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
        if start % 900:
            raise ValueError('timestamp is not on a UTC quarter hour')
        if 'DayAheadPriceEUR' not in record:
            raise ValueError('record lacks DayAheadPriceEUR')
        value = record['DayAheadPriceEUR']
        if value is None:
            continue  # The provider can return unpublished prices as null.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError('invalid price')
        # The provider's numbers carry single-precision noise; nine places per kWh keeps all of the price.
        price = round(float(value) / 1000, 9)
        if not math.isfinite(price):
            raise ValueError('non-finite price')
        by_zone[record['PriceArea']].append([start, start + 900, price, 'EUR'])
    for intervals in by_zone.values():
        intervals.sort()
        for previous, current in zip(intervals, intervals[1:]):
            if current[0] < previous[1]:
                raise ValueError('duplicate or overlapping intervals')
    return by_zone


def day_document(intervals, source, day, now):
    """One UTC day of one zone as the file installations read, or None when nothing is published."""
    midnight = datetime.combine(day, datetime.min.time(), UTC).timestamp()
    chosen = [i for i in intervals if midnight <= i[0] < midnight + 86400]
    if not chosen:
        return None
    return {
        'date': day.isoformat(), 'zone': source.split(':')[1], 'source': source,
        'unit': 'currency/kWh', 'provider': 'Energinet', 'kind': KIND,
        'licence': 'CC BY 4.0', 'attribution': ATTRIBUTION,
        'fetched_at': now, 'mirrored_at': now,
        'terms': dict(TERMS), 'complete': len(chosen) == 96,
        'intervals': chosen,
    }


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.', suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            json.dump(document, handle, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def build(sources, output=Path('.'), *, today=None, opener=urlopen, now=time.time):
    """Fetch today and tomorrow for every zone in ONE request, and write the days that need writing.

    One request a run is deliberate: the provider answered a second request thirty seconds after
    the first with "429 Too Many Requests" on the first real run of this builder.
    """
    if (not isinstance(sources, list) or not sources
            or any(s not in SUPPORTED for s in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError('sources must be a non-empty list of distinct supported provider:zone strings')
    today = today or datetime.now(UTC).date()
    days = [today, today + timedelta(days=1)]
    wanted = []
    for source in sources:
        provider, zone = source.split(':')
        for day in days:
            path = Path(output) / 'v1' / 'prices' / provider / zone / f'{day}.json'
            if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('complete') is True:
                print(f'{source} {day}: keeping complete day')
            else:
                wanted.append((source, zone, day, path))
    if not wanted:
        return
    zones = sorted({zone for _, zone, _, _ in wanted})
    # The service reads start and end as Danish time, so the window is a day wider on each side.
    query = urlencode({
        'start': f'{today - timedelta(days=1)}T00:00',
        'end': f'{today + timedelta(days=3)}T00:00',
        'filter': json.dumps({'PriceArea': zones}), 'limit': LIMIT,
    })
    try:
        with opener(f'{ENDPOINT}?{query}', timeout=60) as response:
            body = response.read(5_000_001)
        if len(body) > 5_000_000:
            raise ValueError('reply exceeds 5 MB')
        by_zone = decode_reply(body, zones)
    except HTTPError as error:
        if error.code != 404:
            raise
        by_zone = {zone: [] for zone in zones}
    stamp = now()
    for source, zone, day, path in wanted:
        document = day_document(by_zone[zone], source, day, stamp)
        if document is None:
            print(f'{source} {day}: not published yet')
            continue
        if path.exists():
            held = json.loads(path.read_text(encoding='utf-8'))['intervals']
            if len(held) > len(document['intervals']):
                print(f'{source} {day}: keeping the fuller file already here')
                continue
            if held == document['intervals']:
                # Rewriting equal prices would make a commit a run for a new timestamp alone.
                print(f'{source} {day}: nothing new')
                continue
        atomic_json(path, document)
        print(f'{source} {day}: wrote {len(document["intervals"])} intervals')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, default=Path(__file__).with_name('sources.json'))
    parser.add_argument('--output', type=Path, default=Path('.'))
    args = parser.parse_args()
    try:
        build(json.loads(args.sources.read_text(encoding='utf-8')), args.output)
    except (OSError, ValueError, TypeError) as error:
        print(f'Price mirror failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
