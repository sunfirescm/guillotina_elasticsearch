from guillotina.commands import Command
from guillotina.commands.utils import change_transaction_strategy
from guillotina.component import get_adapter
from guillotina.component import get_utility
from guillotina.db import ROOT_ID
from guillotina.db import TRASHED_ID
from guillotina.db.reader import reader
from guillotina.interfaces import ICatalogUtility
from guillotina.utils import get_containers
from guillotina_elasticsearch.interfaces import IIndexManager
from guillotina_elasticsearch.migration import Migrator
from guillotina_elasticsearch.utils import get_content_sub_indexes
from guillotina_elasticsearch.utils import get_installed_sub_indexes
from lru import LRU  # pylint: disable=E0611
from os.path import join

import aioelasticsearch
import aiotask_context
import asyncio
import json
import logging


logger = logging.getLogger('guillotina_elasticsearch_vacuum')

GET_CONTAINERS = 'select zoid from {objects_table} where parent_id = $1'
SELECT_BY_KEYS = '''SELECT zoid from {objects_table} where zoid = ANY($1)'''
GET_CHILDREN_BY_PARENT = """
SELECT zoid, parent_id, tid
FROM {objects_table}
WHERE of is NULL AND parent_id = ANY($1)
ORDER BY parent_id
"""

PAGE_SIZE = 1000

GET_OBS_BY_TID = """
SELECT zoid, parent_id, tid
FROM {objects_table}
WHERE of is NULL
ORDER BY tid ASC, zoid ASC
"""

CREATE_INDEX = '''
CREATE INDEX CONCURRENTLY IF NOT EXISTS
objects_tid_zoid ON {objects_table} (tid ASC, zoid ASC);'''


async def clean_orphan_indexes(container):
    search = get_utility(ICatalogUtility)
    installed_indexes = await get_installed_sub_indexes(container)
    content_indexes = [val['index'] for val in
                       await get_content_sub_indexes(container)]
    for alias_name, index in installed_indexes.items():
        if alias_name not in content_indexes:
            # delete, no longer content available
            await search.conn.indices.close(alias_name)
            await search.conn.indices.delete_alias(index, alias_name)
            await search.conn.indices.delete(index)


class Vacuum:

    def __init__(self, txn, tm, request, container, last_tid=-2):
        self.txn = txn
        self.tm = tm
        self.request = request
        self.container = container
        self.orphaned = set()
        self.missing = set()
        self.out_of_date = set()
        self.utility = get_utility(ICatalogUtility)
        self.migrator = Migrator(
            self.utility, self.container, full=True, bulk_size=10,
            lookup_index=True)
        self.index_manager = get_adapter(self.container, IIndexManager)
        self.cache = LRU(200)
        self.last_tid = last_tid
        self.use_tid_query = True
        self.last_zoid = None
        # for state tracking so we get boundries right
        self.last_result_set = []

    def get_sql(self, source):
        storage = self.txn._manager._storage
        return source.format(objects_table=storage._objects_table_name)

    async def iter_batched_es_keys(self):
        # go through one index at a time...
        indexes = [self.index_name]
        for index in self.sub_indexes:
            indexes.append(index['index'])

        for index_name in indexes:
            result = await self.utility.conn.search(
                index=index_name,
                scroll='15m',
                size=PAGE_SIZE,
                _source=False,
                body={
                    "sort": ["_doc"]
                })
            yield [r['_id'] for r in result['hits']['hits']], index_name
            scroll_id = result['_scroll_id']
            while scroll_id:
                try:
                    result = await self.utility.conn.scroll(
                        scroll_id=scroll_id,
                        scroll='5m'
                    )
                except aioelasticsearch.exceptions.TransportError:
                    # no results
                    break
                if len(result['hits']['hits']) == 0:
                    break
                yield [r['_id'] for r in result['hits']['hits']], index_name
                scroll_id = result['_scroll_id']

    async def iter_paged_db_keys(self, oids):
        if self.use_tid_query:
            conn = await self.txn.get_connection()
            async with conn.transaction():
                sql = self.get_sql(GET_OBS_BY_TID)
                cur = await conn.cursor(sql)
                results = await cur.fetch(PAGE_SIZE)
                while len(results) > 0:
                    records = []
                    for record in results:
                        if record['zoid'] in (
                                ROOT_ID, TRASHED_ID,
                                self.container._p_oid):
                            continue
                        records.append(record)
                        self.last_tid = record['tid']
                        self.last_zoid = record['zoid']
                    yield records
                    results = await cur.fetch(PAGE_SIZE)
        else:
            conn = await self.txn.get_connection()
            sql = self.get_sql(GET_CHILDREN_BY_PARENT)

            while oids:
                pos = 0
                new_oids = []
                while (pos * PAGE_SIZE) < len(oids):
                    async with conn.transaction():
                        cur = await conn.cursor(
                            sql, oids[pos:pos + PAGE_SIZE])
                        pos += PAGE_SIZE
                        page = await cur.fetch(PAGE_SIZE)
                        while page:
                            yield page
                            new_oids.extend([r['zoid'] for r in page])
                            page = await cur.fetch(PAGE_SIZE)
                oids = new_oids

    async def get_object(self, oid):
        if oid in self.cache:
            return self.cache[oid]

        try:
            result = self.txn._manager._hard_cache.get(oid, None)
        except AttributeError:
            from guillotina.db.transaction import HARD_CACHE  # noqa
            result = HARD_CACHE.get(oid, None)
        if result is None:
            result = await self.txn._cache.get(oid=oid)

        if result is None:
            result = await self.tm._storage.load(self.txn, oid)

        obj = reader(result)
        obj._p_jar = self.txn
        if result['parent_id']:
            obj.__parent__ = await self.get_object(result['parent_id'])
        return obj

    async def process_missing(self, oid, index_type='missing', folder=False):
        # need to fill in parents in order for indexing to work...
        logger.warning(f'Index {index_type} {oid}')
        try:
            obj = await self.get_object(oid)
        except KeyError:
            logger.warning(f'Could not find {oid}')
            return
        except (AttributeError, TypeError):
            logger.warning(f'Could not find {oid}', exc_info=True)
            return  # object or parent of object was removed, ignore
        try:
            if folder:
                await self.migrator.process_object(obj)
            else:
                await self.migrator.index_object(obj)
        except TypeError:
            logger.warning(f'Could not index {oid}', exc_info=True)

    async def setup(self):
        # how we're doing this...
        # 1) iterate through all es keys
        # 2) batch check obs exist in db
        # 3) iterate through all db keys
        # 4) batch check they are in elasticsearch
        # WHY this way?
        #   - uses less memory rather than getting all keys in both.
        #   - this way should allow us handle VERY large datasets

        try:
            conn = await self.txn.get_connection()
            sql = self.get_sql(CREATE_INDEX)
            async with self.txn._lock:
                await conn.execute(sql)
        except Exception:
            pass

        self.index_name = await self.index_manager.get_index_name()
        self.sub_indexes = await get_content_sub_indexes(self.container)
        self.migrator.work_index_name = self.index_name

    async def check_orphans(self):
        logger.warning(f'Checking orphans on container {self.container.id}', extra={  # noqa
            'account': self.container.id
        })
        conn = await self.txn.get_connection()
        checked = 0
        async for es_batch, index_name in self.iter_batched_es_keys():
            checked += len(es_batch)
            async with self.txn._lock:
                sql = self.get_sql(SELECT_BY_KEYS)
                records = await conn.fetch(sql, es_batch)
            db_batch = set()
            for record in records:
                db_batch.add(record['zoid'])
            orphaned = [k for k in set(es_batch) - db_batch]
            if checked % 10000 == 0:
                logger.warning(f'Checked ophans: {checked}')
            if orphaned:
                # these are keys that are in ES but not in DB so we should
                # remove them..
                self.orphaned |= set(orphaned)
                logger.warning(f'deleting orphaned {len(orphaned)}')
                conn_es = await self.utility.conn.transport.get_connection()
                # delete by query for orphaned keys...
                async with conn_es.session.post(
                        join(conn_es.base_url.human_repr(),
                             index_name, '_delete_by_query'),
                        headers={
                            'Content-Type': 'application/json'
                        },
                        data=json.dumps({
                            "query": {
                                "terms": {
                                    "_id": orphaned
                                }
                            }
                        })) as resp:  # noqa
                    try:
                        data = await resp.json()
                        if data['deleted'] != len(orphaned):
                            logger.warning(
                                f'Was only able to clean up {len(data["deleted"])} '  # noqa
                                f'instead of {len(orphaned)}')
                    except Exception:
                        logger.warning(
                            'Could not parse delete by query response. '
                            'Vacuuming might not be working')

    def get_indexes_for_oids(self, oids):
        '''
        is there something clever here to do this faster
        than iterating over all the data?
        '''
        indexes = [self.index_name]
        for index in self.sub_indexes:
            # check if tid inside sub index...
            prefix = index['oid'].rsplit('|', 1)[0]
            if prefix:
                for oid in oids:
                    if oid.startswith(prefix):
                        indexes.append(index['index'])
                        break
        return indexes

    async def check_missing(self):
        status = (f'Checking missing on container {self.container.id}, '
                  f'starting with TID: {self.last_tid}')
        logger.warning(status, extra={
            'account': self.container.id
        })
        conn = await self.txn.get_connection()
        sql = self.get_sql(GET_CONTAINERS)
        async with self.txn._lock:
            containers = await conn.fetch(sql, ROOT_ID)

        if len(containers) > 1:
            # more than 1 container, we can't optimize by querying by tids
            self.use_tid_query = False

        checked = 0
        async for batch in self.iter_paged_db_keys([self.container._p_oid]):
            oids = [r['zoid'] for r in batch]
            indexes = self.get_indexes_for_oids(oids)
            results = await self.utility.conn.search(
                ','.join(indexes), body={
                    'query': {
                        'terms': {
                            'uuid': oids
                        }
                    }
                },
                _source=False,
                stored_fields='tid,parent_uuid',
                size=PAGE_SIZE)

            es_batch = {}
            for result in results['hits']['hits']:
                oid = result['_id']
                tid = result.get('fields', {}).get('tid') or [-1]
                es_batch[oid] = {
                    'tid': int(tid[0]),
                    'parent_uuid': result.get('fields', {}).get(
                        'parent_uuid', ['_missing_'])[0]
                }
            for record in batch:
                oid = record['zoid']
                tid = record['tid']
                if oid == self.container._p_oid:
                    continue
                if oid not in es_batch:
                    self.missing.add(oid)
                    await self.process_missing(oid)
                elif tid > es_batch[oid]['tid'] and es_batch[oid]['tid'] != -1:
                    self.out_of_date.add(oid)
                    await self.process_missing(oid, index_type='out of date')
                elif record['parent_id'] != es_batch[oid]['parent_uuid']:
                    self.missing.add(oid)
                    await self.process_missing(oid, folder=True)

            checked += len(batch)
            logger.warning(
                f'Checked missing: {checked}: {self.last_tid}, '
                f'missing: {len(self.missing)}, out of date: {len(self.out_of_date)}')  # noqa

        await self.migrator.flush()
        await self.migrator.join_futures()


class VacuumCommand(Command):
    description = 'Run vacuum on elasticearch'
    vacuum_klass = Vacuum
    state = {}

    def get_parser(self):
        parser = super(VacuumCommand, self).get_parser()
        parser.add_argument(
            '--continuous', help='Continuously vacuum', action='store_true')
        parser.add_argument('--sleep', help='Time in seconds to sleep',
                            default=10 * 60, type=int)
        return parser

    async def run(self, arguments, settings, app):
        change_transaction_strategy('none')
        self.request._db_write_enabled = True
        self.request._message.headers['Host'] = 'localhost'
        await asyncio.gather(
            self.do_check(arguments, 'check_missing'),
            self.do_check(arguments, 'check_orphans'))

    async def do_check(self, arguments, check_name):
        aiotask_context.set('request', self.request)
        first_run = True
        while arguments.continuous or first_run:
            if not first_run:
                await asyncio.sleep(arguments.sleep)
            else:
                first_run = False
            async for txn, tm, container in get_containers(self.request):
                try:
                    kwargs = {}
                    if container._p_oid in self.state:
                        kwargs = self.state[container._p_oid]
                    vacuum = self.vacuum_klass(
                        txn, tm, self.request, container, **kwargs)
                    await vacuum.setup()
                    func = getattr(vacuum, check_name)
                    await func()
                    if vacuum.last_tid > 0:
                        self.state[container._p_oid] = {
                            'last_tid': vacuum.last_tid
                        }
                    logger.warning(f'''Finished vacuuming with results:
Orphaned cleaned: {len(vacuum.orphaned)}
Missing added: {len(vacuum.missing)}
Out of date fixed: {len(vacuum.out_of_date)}
''')
                    await clean_orphan_indexes(container)
                except Exception:
                    logger.error('Error vacuuming', exc_info=True)
                finally:
                    await tm.abort(txn=txn)
