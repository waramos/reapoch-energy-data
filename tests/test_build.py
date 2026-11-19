from datetime import UTC, date, datetime, timedelta
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

import build


DAY = date(2026, 11, 19)


def reply(day=DAY, zone='DK1', count=96, days=1):
    start = datetime.combine(day, datetime.min.time(), UTC)
    return {'records': [
        {'TimeUTC': (start + timedelta(days=d, minutes=15 * i)).isoformat(),
         'PriceArea': zone, 'DayAheadPriceEUR': -50 if i == 0 else 100}
        for d in range(days) for i in range(count)
    ]}


class FakeOpener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        assert timeout == 60
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())


def run(tmp_path, responses, **kwargs):
    opener = FakeOpener(responses)
    build.build(['energinet:DK1'], tmp_path, today=DAY, opener=opener,
                **kwargs)
    return opener


def output_path(tmp_path, day=DAY):
    return tmp_path / 'v1/prices/energinet/DK1' / f'{day}.json'


def test_shape_and_query(tmp_path):
    opener = run(tmp_path, [reply(days=2)])
    document = json.loads(output_path(tmp_path).read_text())
    assert set(document) == {
        'date', 'zone', 'source', 'unit', 'provider', 'kind', 'licence',
        'attribution', 'fetched_at', 'mirrored_at', 'terms', 'complete', 'intervals',
    }
    assert document['date'] == str(DAY)
    assert document['zone'] == 'DK1'
    assert document['source'] == 'energinet:DK1'
    assert document['provider'] == 'Energinet'
    assert document['unit'] == 'currency/kWh'
    assert document['licence'] == 'CC BY 4.0'
    assert 'https://creativecommons.org/licenses/by/4.0/' in document['attribution']
    assert document['complete'] is True
    assert len(document['intervals']) == 96
    first = datetime(2026, 11, 19, tzinfo=UTC).timestamp()
    assert document['intervals'][0] == [first, first + 900, -0.05, 'EUR']
    assert document['intervals'][-1][1] == first + 86400
    query = parse_qs(urlsplit(opener.urls[0]).query)
    assert query == {'start': ['2026-11-18T00:00'], 'end': ['2026-11-22T00:00'],
                     'filter': ['{"PriceArea": ["DK1"]}'], 'limit': ['1000']}
    assert len(opener.urls) == 1
    tomorrow = json.loads(output_path(tmp_path, DAY + timedelta(days=1)).read_text())
    assert tomorrow['complete'] is True and tomorrow['date'] == '2026-11-20'


def test_atomic_replace_and_cleanup(tmp_path, monkeypatch):
    path = tmp_path / 'day.json'
    path.write_text('{"old": true}\n')
    original = build.os.replace

    def inspect_replace(source, destination):
        assert path.read_text() == '{"old": true}\n'
        assert json.loads(build.Path(source).read_text()) == {'new': True}
        assert build.Path(source).parent == path.parent
        original(source, destination)

    monkeypatch.setattr(build.os, 'replace', inspect_replace)
    build.atomic_json(path, {'new': True})
    assert json.loads(path.read_text()) == {'new': True}
    assert list(tmp_path.iterdir()) == [path]

    def fail_replace(*args):
        raise OSError('disk failure')

    monkeypatch.setattr(build.os, 'replace', fail_replace)
    with pytest.raises(OSError):
        build.atomic_json(path, {'lost': True})
    assert json.loads(path.read_text()) == {'new': True}
    assert list(tmp_path.iterdir()) == [path]


def test_complete_days_kept(tmp_path):
    run(tmp_path, [reply(days=2)])
    before = output_path(tmp_path).read_bytes()
    opener = run(tmp_path, [])
    assert opener.urls == []
    assert output_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize('response', [
    {'records': []},
    HTTPError('https://example.invalid', 404, 'not published', {}, None),
    {'records': [{'TimeUTC': '2026-11-19T00:00:00', 'PriceArea': 'DK1',
                  'DayAheadPriceEUR': None}]},
])
def test_unpublished_day(tmp_path, response):
    run(tmp_path, [response])
    assert not output_path(tmp_path).exists()


def test_partial_can_be_completed(tmp_path):
    run(tmp_path, [reply(count=8)])
    assert json.loads(output_path(tmp_path).read_text())['complete'] is False
    run(tmp_path, [reply(count=4)])
    assert len(json.loads(output_path(tmp_path).read_text())['intervals']) == 8  # a thinner reply replaces nothing
    run(tmp_path, [reply()])
    assert json.loads(output_path(tmp_path).read_text())['complete'] is True


def test_one_request_serves_every_zone_and_both_days(tmp_path):
    both = {'records': reply(days=2)['records'] + reply(zone='DK2', days=2)['records']}
    opener = FakeOpener([both])
    build.build(['energinet:DK1', 'energinet:DK2'], tmp_path, today=DAY, opener=opener)
    assert len(opener.urls) == 1
    assert parse_qs(urlsplit(opener.urls[0]).query)['filter'] == ['{"PriceArea": ["DK1", "DK2"]}']
    written = sorted(str(p.relative_to(tmp_path / 'v1/prices/energinet')) for p in tmp_path.rglob('*.json'))
    assert written == ['DK1/2026-11-19.json', 'DK1/2026-11-20.json', 'DK2/2026-11-19.json', 'DK2/2026-11-20.json']
    assert json.loads((tmp_path / 'v1/prices/energinet/DK2/2026-11-20.json').read_text())['zone'] == 'DK2'


@pytest.mark.parametrize('bad', [
    {}, {'records': {}},
    {'records': [{'TimeUTC': 'bad', 'PriceArea': 'DK1', 'DayAheadPriceEUR': 1}]},
    reply(zone='DK2'),
    {'records': [{'TimeUTC': '2026-11-19T00:00:00', 'PriceArea': 'DK1'}]},
    {'records': [{'TimeUTC': '2026-11-19T00:00:00', 'PriceArea': 'DK1',
                  'DayAheadPriceEUR': 'NaN'}]},
    {'records': reply(count=1)['records'] * 2},
])
def test_malformed_reply_refused(tmp_path, bad):
    with pytest.raises((ValueError, TypeError)):
        run(tmp_path, [bad])
    assert not output_path(tmp_path).exists()


@pytest.mark.parametrize('error', [URLError('offline'),
    HTTPError('https://example.invalid', 500, 'failed', {}, None)])
def test_failed_request_fails(tmp_path, error):
    with pytest.raises(OSError):
        run(tmp_path, [error])


def test_main_exit_status(tmp_path, monkeypatch):
    monkeypatch.setattr('sys.argv', ['build.py', '--output', str(tmp_path)])

    def failure(*args):
        raise URLError('offline')

    monkeypatch.setattr(build, 'build', failure)
    assert build.main() == 1
    monkeypatch.setattr(build, 'build', lambda *args: None)
    assert build.main() == 0


def test_equal_prices_are_not_rewritten(tmp_path):
    run(tmp_path, [reply(count=8)])
    before = output_path(tmp_path).read_bytes()
    run(tmp_path, [reply(count=8)], now=lambda: 5.0)
    assert output_path(tmp_path).read_bytes() == before
