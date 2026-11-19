"""Lab 5 ETL logic (deployment/etl/etl.py) without a database: staging, watermarks, skipped sources."""
import contextlib
import datetime
import importlib.util
import io
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('etl', Path(__file__).resolve().parents[1] / 'deployment/etl/etl.py')
etl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(etl)

UTC = datetime.timezone.utc
RUN = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


class FakeResult:
    def __init__(self, row=None, rowcount=0):
        self.row, self.rowcount = row, rowcount

    def fetchone(self):
        return self.row


class FakeWarehouse:
    """Records statements; answers the watermark query from `watermarks`."""

    def __init__(self, watermarks=None):
        self.watermarks = watermarks or {}
        self.statements, self.staged = [], []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.startswith('SELECT last_updated_at'):
            value = self.watermarks.get(params)
            return FakeResult((value,) if value else None)
        return FakeResult(rowcount=1 if sql.lstrip().startswith(('WITH', 'INSERT INTO fact')) else 0)

    @contextlib.contextmanager
    def transaction(self):
        yield

    def cursor(self):
        warehouse = self

        class Cursor:
            def executemany(self, sql, rows):
                warehouse.staged.extend(rows)
        return Cursor()


def quietly(function, *args, **kwargs):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = function(*args, **kwargs)
    return result, [json.loads(line) for line in output.getvalue().splitlines()]


class EtlTests(unittest.TestCase):
    def test_every_source_has_a_unique_view_and_a_watermark_column(self):
        names = [(s['source'], s['view']) for s in etl.SOURCES]
        self.assertEqual(len(names), len(set(names)))
        for source in etl.SOURCES:
            self.assertTrue(source['key'] and source['updated'])

    def test_staged_rows_keep_the_row_as_json_keyed_by_its_natural_key(self):
        source = etl.source('tamagotchi', 'ownership_transfers', ('battle_id', 'pet_id'), 'transferred_at')
        rows = etl.staged_rows([{'battle_id': 'b1', 'pet_id': 'p1', 'transferred_at': '2026-11-19T10:00:00Z'}], source, RUN)
        self.assertEqual(rows[0][:3], ('tamagotchi', 'ownership_transfers', 'b1|p1'))
        self.assertEqual(json.loads(rows[0][3])['pet_id'], 'p1')
        self.assertEqual(rows[0][4], datetime.datetime(2026, 11, 19, 10, 0, tzinfo=UTC))
        self.assertEqual(rows[0][5], RUN)

    def test_reads_from_the_watermark_minus_the_look_back(self):
        watermark = datetime.datetime(2026, 11, 19, 11, 0, tzinfo=UTC)
        source = etl.source('notification', 'notifications', ('notification_id',), 'updated_at')
        warehouse = FakeWarehouse({('notification', 'notifications'): watermark})
        seen = []

        def extract(spec, since, env):
            seen.append(since)
            yield [{'notification_id': 'n1', 'updated_at': watermark + datetime.timedelta(seconds=5)}]

        self.assertEqual(etl.stage_source(warehouse, source, RUN, {}, extract), 1)
        self.assertEqual(seen, [watermark - etl.LOOK_BACK])
        advanced = [params for sql, params in warehouse.statements if 'INSERT INTO etl.watermarks' in sql]
        self.assertEqual(advanced[0][2], watermark + datetime.timedelta(seconds=5))

    def test_a_first_run_starts_from_the_beginning(self):
        seen = []

        def extract(spec, since, env):
            seen.append(since)
            return iter(())

        etl.stage_source(FakeWarehouse(), etl.SOURCES[0], RUN, {}, extract)
        self.assertEqual(seen, [etl.EPOCH - etl.LOOK_BACK])

    def test_an_unreachable_source_is_skipped_and_the_others_still_load(self):
        def extract(spec, since, env):
            if spec['source'] == 'battles':
                raise ConnectionError('connection refused\nmore detail')
            return iter([[{**{column: 'x' for column in spec['key']}, spec['updated']: '2026-11-19T10:00:00Z'}]])

        warehouse = FakeWarehouse()
        (staged, recomputed, skipped), lines = quietly(etl.run_once, warehouse, {}, extract, lambda tz: RUN)

        self.assertEqual(skipped, ['battles.battles'])
        self.assertEqual(staged, len(etl.SOURCES) - 1)
        self.assertEqual(recomputed, len(etl.FACTS))
        unavailable = [line for line in lines if line['msg'] == 'etl source unavailable']
        self.assertEqual(unavailable[0]['error'], 'connection refused')
        finished = [line for line in lines if line['msg'] == 'etl run finished']
        self.assertEqual(finished[0]['sourcesSkipped'], ['battles.battles'])
        self.assertTrue(all(line['service'] == 'etl' and line['level'] == 'info' for line in lines))

    def test_facts_are_recomputed_for_keys_changed_in_any_input(self):
        # fact_battle must also follow transfers, so a late transfer repairs its battle.
        self.assertIn("view = 'ownership_transfers'", etl.FACTS['fact_battle'].split('INSERT')[0])
        self.assertIn("view = 'pets'", etl.FACTS['fact_care_action'].split('INSERT')[0])
        self.assertIn("view = 'raids'", etl.FACTS['fact_raid_participation'].split('INSERT')[0])
        for sql in etl.FACTS.values():
            self.assertIn('%(run)s', sql)
            self.assertIn('ON CONFLICT', sql)

    def test_requires_one_mode(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            etl.main([])


if __name__ == '__main__':
    unittest.main()
