# See the file "LICENSE" for information about the copyright
# and warranty status of this software.

import array
import ast
import asyncio
import struct
import time
from bisect import bisect_left
from collections import defaultdict, namedtuple
from functools import partial

import plyvel

from server.cache import FSCache, UTXOCache, NO_CACHE_ENTRY
from server.daemon import DaemonError
from lib.hash import hash_to_str
from lib.script import ScriptPubKey
from lib.util import chunks, LoggedClass


def formatted_time(t):
    '''Return a number of seconds as a string in days, hours, mins and
    secs.'''
    t = int(t)
    return '{:d}d {:02d}h {:02d}m {:02d}s'.format(
        t // 86400, (t % 86400) // 3600, (t % 3600) // 60, t % 60)


UTXO = namedtuple("UTXO", "tx_num tx_pos tx_hash height value")


class ChainError(Exception):
    pass


class Prefetcher(LoggedClass):
    '''Prefetches blocks (in the forward direction only).'''

    def __init__(self, daemon, height):
        super().__init__()
        self.daemon = daemon
        self.semaphore = asyncio.Semaphore()
        self.queue = asyncio.Queue()
        self.queue_size = 0
        # Target cache size.  Has little effect on sync time.
        self.target_cache_size = 10 * 1024 * 1024
        self.fetched_height = height
        self.recent_sizes = [0]

    async def get_blocks(self):
        '''Returns a list of prefetched blocks.'''
        blocks, total_size = await self.queue.get()
        self.queue_size -= total_size
        return blocks

    async def clear(self, height):
        '''Clear prefetched blocks and restart from the given height.

        Used in blockchain reorganisations.  This coroutine can be
        called asynchronously to the _prefetch coroutine so we must
        synchronize.
        '''
        with await self.semaphore:
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue_size = 0
            self.fetched_height = height

    async def start(self):
        '''Loop forever polling for more blocks.'''
        self.logger.info('prefetching blocks...')
        while True:
            while self.queue_size < self.target_cache_size:
                try:
                    with await self.semaphore:
                        await self._prefetch()
                except DaemonError as e:
                    self.logger.info('ignoring daemon errors: {}'.format(e))
            await asyncio.sleep(2)

    def _prefill_count(self, room):
        ave_size = sum(self.recent_sizes) // len(self.recent_sizes)
        count = room // ave_size if ave_size else 0
        return max(count, 10)

    async def _prefetch(self):
        '''Prefetch blocks if there are any to prefetch.'''
        daemon_height = await self.daemon.height()
        max_count = min(daemon_height - self.fetched_height, 4000)
        count = min(max_count, self._prefill_count(self.target_cache_size))
        first = self.fetched_height + 1
        hex_hashes = await self.daemon.block_hex_hashes(first, count)
        if not hex_hashes:
            return

        blocks = await self.daemon.raw_blocks(hex_hashes)
        sizes = [len(block) for block in blocks]
        total_size = sum(sizes)
        self.queue.put_nowait((blocks, total_size))
        self.queue_size += total_size
        self.fetched_height += len(blocks)

        # Keep 50 most recent block sizes for fetch count estimation
        self.recent_sizes.extend(sizes)
        excess = len(self.recent_sizes) - 50
        if excess > 0:
            self.recent_sizes = self.recent_sizes[excess:]


class BlockProcessor(LoggedClass):
    '''Process blocks and update the DB state to match.

    Employ a prefetcher to prefetch blocks in batches for processing.
    Coordinate backing up in case of chain reorganisations.
    '''

    def __init__(self, env, daemon):
        super().__init__()

        self.daemon = daemon

        # Meta
        self.utxo_MB = env.utxo_MB
        self.hist_MB = env.hist_MB
        self.next_cache_check = 0
        self.last_flush = time.time()
        self.coin = env.coin
        self.caught_up = False
        self.reorg_limit = env.reorg_limit

        # Chain state (initialize to genesis in case of new DB)
        self.db_height = -1
        self.db_tx_count = 0
        self.db_tip = b'\0' * 32
        self.flush_count = 0
        self.utxo_flush_count = 0
        self.wall_time = 0

        # Open DB and metadata files.  Record some of its state.
        self.db = self.open_db(self.coin)
        self.tx_count = self.db_tx_count
        self.height = self.db_height
        self.tip = self.db_tip

        # Caches to be flushed later.  Headers and tx_hashes have one
        # entry per block
        self.history = defaultdict(partial(array.array, 'I'))
        self.history_size = 0
        self.backup_hash168s = set()
        self.utxo_cache = UTXOCache(self, self.db, self.coin)
        self.fs_cache = FSCache(self.coin, self.height, self.tx_count)
        self.prefetcher = Prefetcher(daemon, self.height)

        # Redirected member func
        self.get_tx_hash = self.fs_cache.get_tx_hash

        # Log state
        self.logger.info('{}/{} height: {:,d} tx count: {:,d} '
                         'flush count: {:,d} utxo flush count: {:,d} '
                         'sync time: {}'
                         .format(self.coin.NAME, self.coin.NET, self.height,
                                 self.tx_count, self.flush_count,
                                 self.utxo_flush_count,
                                 formatted_time(self.wall_time)))
        self.logger.info('reorg limit of {:,d} blocks'
                         .format(self.reorg_limit))
        self.logger.info('flushing UTXO cache at {:,d} MB'
                         .format(self.utxo_MB))
        self.logger.info('flushing history cache at {:,d} MB'
                         .format(self.hist_MB))

        self.clean_db()

    def coros(self, force_backup=False):
        if force_backup:
            return [self.force_chain_reorg(True), self.prefetcher.start()]
        else:
            return [self.start(), self.prefetcher.start()]

    async def start(self):
        '''External entry point for block processing.

        A simple wrapper that safely flushes the DB on clean
        shutdown.
        '''
        try:
            await self.advance_blocks()
        finally:
            self.flush(True)

    async def advance_blocks(self):
        '''Loop forever processing blocks in the forward direction.'''
        while True:
            blocks = await self.prefetcher.get_blocks()
            for block in blocks:
                if not self.advance_block(block):
                    await self.handle_chain_reorg()
                    self.caught_up = False
                    break
                await asyncio.sleep(0)   # Yield

            if self.height != self.daemon.cached_height():
                continue

            if not self.caught_up:
                self.caught_up = True
                self.logger.info('caught up to height {:,d}'
                                 .format(self.height))

            # Flush everything when in caught-up state as queries
            # are performed on DB not in-memory
            self.flush(True)

    async def force_chain_reorg(self, to_genesis):
        try:
            await self.handle_chain_reorg(to_genesis)
        finally:
            self.flush(True)

    async def handle_chain_reorg(self, to_genesis=False):
        # First get all state on disk
        self.logger.info('chain reorg detected')
        self.flush(True)
        self.logger.info('finding common height...')
        hashes = await self.reorg_hashes(to_genesis)
        # Reverse and convert to hex strings.
        hashes = [hash_to_str(hash) for hash in reversed(hashes)]
        for hex_hashes in chunks(hashes, 50):
            blocks = await self.daemon.raw_blocks(hex_hashes)
            self.backup_blocks(blocks)
        self.logger.info('backed up to height {:,d}'.format(self.height))
        await self.prefetcher.clear(self.height)
        self.logger.info('prefetcher reset')

    async def reorg_hashes(self, to_genesis):
        '''Return the list of hashes to back up beacuse of a reorg.

        The hashes are returned in order of increasing height.'''
        def match_pos(hashes1, hashes2):
            for n, (hash1, hash2) in enumerate(zip(hashes1, hashes2)):
                if hash1 == hash2:
                    return n
            return -1

        start = self.height - 1
        count = 1
        while start > 0:
            self.logger.info('start: {:,d} count: {:,d}'.format(start, count))
            hashes = self.fs_cache.block_hashes(start, count)
            hex_hashes = [hash_to_str(hash) for hash in hashes]
            d_hex_hashes = await self.daemon.block_hex_hashes(start, count)
            n = match_pos(hex_hashes, d_hex_hashes)
            if n >= 0 and not to_genesis:
                start += n + 1
                break
            count = min(count * 2, start)
            start -= count

        # Hashes differ from height 'start'
        count = (self.height - start) + 1

        self.logger.info('chain was reorganised for {:,d} blocks from '
                         'height {:,d} to height {:,d}'
                         .format(count, start, start + count - 1))

        return self.fs_cache.block_hashes(start, count)

    def open_db(self, coin):
        db_name = '{}-{}'.format(coin.NAME, coin.NET)
        try:
            db = plyvel.DB(db_name, create_if_missing=False,
                           error_if_exists=False, compression=None)
        except:
            db = plyvel.DB(db_name, create_if_missing=True,
                           error_if_exists=True, compression=None)
            self.logger.info('created new database {}'.format(db_name))
        else:
            self.logger.info('successfully opened database {}'.format(db_name))
            self.read_state(db)

        return db

    def read_state(self, db):
        state = db.get(b'state')
        state = ast.literal_eval(state.decode())
        if state['genesis'] != self.coin.GENESIS_HASH:
            raise ChainError('DB genesis hash {} does not match coin {}'
                             .format(state['genesis_hash'],
                                     self.coin.GENESIS_HASH))
        self.db_height = state['height']
        self.db_tx_count = state['tx_count']
        self.db_tip = state['tip']
        self.flush_count = state['flush_count']
        self.utxo_flush_count = state['utxo_flush_count']
        self.wall_time = state['wall_time']

    def clean_db(self):
        '''Clean out stale DB items.

        Stale DB items are excess history flushed since the most
        recent UTXO flush (only happens on unclean shutdown), and aged
        undo information.
        '''
        if self.flush_count < self.utxo_flush_count:
            raise ChainError('DB corrupt: flush_count < utxo_flush_count')
        with self.db.write_batch(transaction=True) as batch:
            if self.flush_count > self.utxo_flush_count:
                self.logger.info('DB shut down uncleanly.  Scanning for '
                                 'excess history flushes...')
                self.remove_excess_history(batch)
                self.utxo_flush_count = self.flush_count
            self.remove_stale_undo_items(batch)
            self.flush_state(batch)

    def remove_excess_history(self, batch):
        prefix = b'H'
        unpack = struct.unpack
        keys = []
        for key, hist in self.db.iterator(prefix=prefix):
            flush_id, = unpack('>H', key[-2:])
            if flush_id > self.utxo_flush_count:
                keys.append(key)

        self.logger.info('deleting {:,d} history entries'
                         .format(len(keys)))
        for key in keys:
            batch.delete(key)

    def remove_stale_undo_items(self, batch):
        prefix = b'U'
        unpack = struct.unpack
        cutoff = self.db_height - self.reorg_limit
        keys = []
        for key, hist in self.db.iterator(prefix=prefix):
            height, = unpack('>I', key[-4:])
            if height > cutoff:
                break
            keys.append(key)

        self.logger.info('deleting {:,d} stale undo entries'
                         .format(len(keys)))
        for key in keys:
            batch.delete(key)

    def flush_state(self, batch):
        '''Flush chain state to the batch.'''
        now = time.time()
        self.wall_time += now - self.last_flush
        self.last_flush = now
        state = {
            'genesis': self.coin.GENESIS_HASH,
            'height': self.db_height,
            'tx_count': self.db_tx_count,
            'tip': self.db_tip,
            'flush_count': self.flush_count,
            'utxo_flush_count': self.utxo_flush_count,
            'wall_time': self.wall_time,
        }
        batch.put(b'state', repr(state).encode())

    def flush_utxos(self, batch):
        self.logger.info('flushing UTXOs: {:,d} txs and {:,d} blocks'
                         .format(self.tx_count - self.db_tx_count,
                                 self.height - self.db_height))
        self.utxo_cache.flush(batch)
        self.utxo_flush_count = self.flush_count
        self.db_tx_count = self.tx_count
        self.db_height = self.height
        self.db_tip = self.tip

    def assert_flushed(self):
        '''Asserts state is fully flushed.'''
        assert self.tx_count == self.db_tx_count
        assert not self.history
        assert not self.utxo_cache.cache
        assert not self.utxo_cache.db_cache
        assert not self.backup_hash168s

    def flush(self, flush_utxos=False):
        '''Flush out cached state.

        History is always flushed.  UTXOs are flushed if flush_utxos.'''
        if self.height == self.db_height:
            self.logger.info('nothing to flush')
            self.assert_flushed()
            return

        flush_start = time.time()
        last_flush = self.last_flush
        tx_diff = self.tx_count - self.db_tx_count

        # Write out the files to the FS before flushing to the DB.  If
        # the DB transaction fails, the files being too long doesn't
        # matter.  But if writing the files fails we do not want to
        # have updated the DB.
        if self.height > self.db_height:
            self.fs_cache.flush(self.height, self.tx_count)

        with self.db.write_batch(transaction=True) as batch:
            # History first - fast and frees memory.  Flush state last
            # as it reads the wall time.
            if self.height > self.db_height:
                self.flush_history(batch)
            else:
                self.backup_history(batch)
            if flush_utxos:
                self.flush_utxos(batch)
            self.flush_state(batch)
            self.logger.info('committing transaction...')

        # Update and put the wall time again - otherwise we drop the
        # time it took to commit the batch
        self.flush_state(self.db)

        flush_time = int(self.last_flush - flush_start)
        self.logger.info('flush #{:,d} to height {:,d} txs: {:,d} took {:,d}s'
                         .format(self.flush_count, self.height, self.tx_count,
                                 flush_time))

        # Catch-up stats
        if not self.caught_up and tx_diff > 0:
            daemon_height = self.daemon.cached_height()
            txs_per_sec = int(self.tx_count / self.wall_time)
            this_txs_per_sec = 1 + int(tx_diff / (self.last_flush - last_flush))
            if self.height > self.coin.TX_COUNT_HEIGHT:
                tx_est = (daemon_height - self.height) * self.coin.TX_PER_BLOCK
            else:
                tx_est = ((daemon_height - self.coin.TX_COUNT_HEIGHT)
                          * self.coin.TX_PER_BLOCK
                          + (self.coin.TX_COUNT - self.tx_count))

            self.logger.info('tx/sec since genesis: {:,d}, '
                             'since last flush: {:,d}'
                             .format(txs_per_sec, this_txs_per_sec))
            self.logger.info('sync time: {}  ETA: {}'
                             .format(formatted_time(self.wall_time),
                                     formatted_time(tx_est / this_txs_per_sec)))

    def flush_history(self, batch):
        self.logger.info('flushing history')
        assert not self.backup_hash168s

        self.flush_count += 1
        flush_id = struct.pack('>H', self.flush_count)

        for hash168, hist in self.history.items():
            key = b'H' + hash168 + flush_id
            batch.put(key, hist.tobytes())

        self.logger.info('{:,d} history entries in {:,d} addrs'
                         .format(self.history_size, len(self.history)))

        self.history = defaultdict(partial(array.array, 'I'))
        self.history_size = 0

    def backup_history(self, batch):
        self.logger.info('backing up history to height {:,d}  tx_count {:,d}'
                         .format(self.height, self.tx_count))

        # Drop any NO_CACHE entry
        self.backup_hash168s.discard(NO_CACHE_ENTRY)
        assert not self.history

        nremoves = 0
        for hash168 in sorted(self.backup_hash168s):
            prefix = b'H' + hash168
            deletes = []
            puts = {}
            for key, hist in self.db.iterator(reverse=True, prefix=prefix):
                a = array.array('I')
                a.frombytes(hist)
                # Remove all history entries >= self.tx_count
                idx = bisect_left(a, self.tx_count)
                nremoves += len(a) - idx
                if idx > 0:
                    puts[key] = a[:idx].tobytes()
                    break
                deletes.append(key)

            for key in deletes:
                batch.delete(key)
            for key, value in puts.items():
                batch.put(key, value)

        self.logger.info('removed {:,d} history entries from {:,d} addresses'
                         .format(nremoves, len(self.backup_hash168s)))
        self.backup_hash168s = set()

    def cache_sizes(self):
        '''Returns the approximate size of the cache, in MB.'''
        # Good average estimates based on traversal of subobjects and
        # requesting size from Python (see deep_getsizeof).  For
        # whatever reason Python O/S mem usage is typically +30% or
        # more, so we scale our already bloated object sizes.
        one_MB = int(1048576 / 1.3)
        utxo_cache_size = len(self.utxo_cache.cache) * 187
        db_cache_size = len(self.utxo_cache.db_cache) * 105
        hist_cache_size = len(self.history) * 180 + self.history_size * 4
        utxo_MB = (db_cache_size + utxo_cache_size) // one_MB
        hist_MB = hist_cache_size // one_MB

        self.logger.info('cache stats at height {:,d}  daemon height: {:,d}'
                         .format(self.height, self.daemon.cached_height()))
        self.logger.info('  entries: UTXO: {:,d}  DB: {:,d}  '
                         'hist addrs: {:,d}  hist size {:,d}'
                         .format(len(self.utxo_cache.cache),
                                 len(self.utxo_cache.db_cache),
                                 self.history_size,
                                 len(self.history)))
        self.logger.info('  size: {:,d}MB  (UTXOs {:,d}MB hist {:,d}MB)'
                         .format(utxo_MB + hist_MB, utxo_MB, hist_MB))
        return utxo_MB, hist_MB

    def undo_key(self, height):
        '''DB key for undo information at the given height.'''
        return b'U' + struct.pack('>I', height)

    def write_undo_info(self, height, undo_info):
        '''Write out undo information for the current height.'''
        self.db.put(self.undo_key(height), undo_info)

    def read_undo_info(self, height):
        '''Read undo information from a file for the current height.'''
        return self.db.get(self.undo_key(height))

    def advance_block(self, block):
        # We must update the fs_cache before calling advance_txs() as
        # the UTXO cache uses the fs_cache via get_tx_hash() to
        # resolve compressed key collisions
        header, tx_hashes, txs = self.coin.read_block(block)
        self.fs_cache.advance_block(header, tx_hashes, txs)
        prev_hash, header_hash = self.coin.header_hashes(header)
        if prev_hash != self.tip:
            return False

        self.tip = header_hash
        self.height += 1
        undo_info = self.advance_txs(tx_hashes, txs)
        if self.daemon.cached_height() - self.height <= self.reorg_limit:
            self.write_undo_info(self.height, b''.join(undo_info))

        # Check if we're getting full and time to flush?
        now = time.time()
        if now > self.next_cache_check:
            self.next_cache_check = now + 60
            utxo_MB, hist_MB = self.cache_sizes()
            if utxo_MB >= self.utxo_MB or hist_MB >= self.hist_MB:
                self.flush(utxo_MB >= self.utxo_MB)

        return True

    def advance_txs(self, tx_hashes, txs):
        put_utxo = self.utxo_cache.put
        spend_utxo = self.utxo_cache.spend
        undo_info = []

        # Use local vars for speed in the loops
        history = self.history
        tx_num = self.tx_count
        coin = self.coin
        parse_script = ScriptPubKey.from_script
        pack = struct.pack

        for tx, tx_hash in zip(txs, tx_hashes):
            hash168s = set()
            tx_numb = pack('<I', tx_num)

            # Spend the inputs
            if not tx.is_coinbase:
                for txin in tx.inputs:
                    cache_value = spend_utxo(txin.prev_hash, txin.prev_idx)
                    undo_info.append(cache_value)
                    hash168s.add(cache_value[:21])

            # Add the new UTXOs
            for idx, txout in enumerate(tx.outputs):
                # Get the hash168.  Ignore scripts we can't grok.
                hash168 = parse_script(txout.pk_script, coin).hash168
                if hash168:
                    hash168s.add(hash168)
                    put_utxo(tx_hash + pack('<H', idx),
                             hash168 + tx_numb + pack('<Q', txout.value))

            # Drop any NO_CACHE entry
            hash168s.discard(NO_CACHE_ENTRY)
            for hash168 in hash168s:
                history[hash168].append(tx_num)
            self.history_size += len(hash168s)
            tx_num += 1

        self.tx_count = tx_num

        return undo_info

    def backup_blocks(self, blocks):
        '''Backup the blocks and flush.

        The blocks should be in order of decreasing height.
        A flush is performed once the blocks are backed up.
        '''
        self.logger.info('backing up {:,d} blocks'.format(len(blocks)))
        self.assert_flushed()

        for block in blocks:
            header, tx_hashes, txs = self.coin.read_block(block)
            prev_hash, header_hash = self.coin.header_hashes(header)
            if header_hash != self.tip:
                raise ChainError('backup block {} is not tip {} at height {:,d}'
                                 .format(hash_to_str(header_hash),
                                         hash_to_str(self.tip), self.height))

            self.backup_txs(tx_hashes, txs)
            self.fs_cache.backup_block()
            self.tip = prev_hash
            self.height -= 1

        self.logger.info('backed up to height {:,d}'.format(self.height))
        self.flush(True)

    def backup_txs(self, tx_hashes, txs):
        # Prevout values, in order down the block (coinbase first if present)
        # undo_info is in reverse block order
        undo_info = self.read_undo_info(self.height)
        n = len(undo_info)

        # Use local vars for speed in the loops
        pack = struct.pack
        put_utxo = self.utxo_cache.put
        spend_utxo = self.utxo_cache.spend
        hash168s = self.backup_hash168s

        rtxs = reversed(txs)
        rtx_hashes = reversed(tx_hashes)

        for tx_hash, tx in zip(rtx_hashes, rtxs):
            # Spend the outputs
            for idx, txout in enumerate(tx.outputs):
                cache_value = spend_utxo(tx_hash, idx)
                hash168s.add(cache_value[:21])

            # Restore the inputs
            if not tx.is_coinbase:
                for txin in reversed(tx.inputs):
                    n -= 33
                    undo_item = undo_info[n:n+33]
                    put_utxo(txin.prev_hash + pack('<H', txin.prev_idx),
                             undo_item)
                    hash168s.add(undo_item[:21])

        assert n == 0
        self.tx_count -= len(txs)

    @staticmethod
    def resolve_limit(limit):
        if limit is None:
            return -1
        assert isinstance(limit, int) and limit >= 0
        return limit

    def get_history(self, hash168, limit=1000):
        '''Generator that returns an unpruned, sorted list of (tx_hash,
        height) tuples of transactions that touched the address,
        earliest in the blockchain first.  Includes both spending and
        receiving transactions.  By default yields at most 1000 entries.
        Set limit to None to get them all.
        '''
        limit = self.resolve_limit(limit)
        prefix = b'H' + hash168
        for key, hist in self.db.iterator(prefix=prefix):
            a = array.array('I')
            a.frombytes(hist)
            for tx_num in a:
                if limit == 0:
                    return
                yield self.get_tx_hash(tx_num)
                limit -= 1

    def get_balance(self, hash168):
        '''Returns the confirmed balance of an address.'''
        return sum(utxo.value for utxo in self.get_utxos(hash168, limit=None))

    def get_utxos(self, hash168, limit=1000):
        '''Generator that yields all UTXOs for an address sorted in no
        particular order.  By default yields at most 1000 entries.
        Set limit to None to get them all.
        '''
        limit = self.resolve_limit(limit)
        unpack = struct.unpack
        prefix = b'u' + hash168
        utxos = []
        for k, v in self.db.iterator(prefix=prefix):
            (tx_pos, ) = unpack('<H', k[-2:])

            for n in range(0, len(v), 12):
                if limit == 0:
                    return
                (tx_num, ) = unpack('<I', v[n:n+4])
                (value, ) = unpack('<Q', v[n+4:n+12])
                tx_hash, height = self.get_tx_hash(tx_num)
                yield UTXO(tx_num, tx_pos, tx_hash, height, value)
                limit -= 1

    def get_utxos_sorted(self, hash168):
        '''Returns all the UTXOs for an address sorted by height and
        position in the block.'''
        return sorted(self.get_utxos(hash168, limit=None))

    def get_current_header(self):
        '''Returns the current header as a dictionary.'''
        return self.fs_cache.encode_header(self.height)
