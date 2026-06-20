# Regression tests for P0.2 reorg fixes:
#   1. ref-loc/WAVE undo info must be read back under the same key it was
#      written (b'RU' + height), not under the plain UTXO undo key (b'U' + ...).
#   2. block_processor diff_pos must return len(hashes1), not a nonexistent
#      'hashes' name, when two hash lists fully agree.
# Plus the P0.4 follow-up:
#   3. clear_excess_undo_info must ALSO garbage-collect the b'RU' ref-loc undo
#      keys (they sort under b'R', not b'U', so the b'U'-only sweep left them to
#      grow unboundedly) using the same keep-window logic.

import types

from electrumx.server.db import DB
from electrumx.lib.util import pack_be_uint32


class _Batch:
    '''Context-manager batch matching the storage write_batch() interface.'''

    def __init__(self, store):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def delete(self, key):
        self._store.pop(key, None)


class FakeKVStore(dict):
    '''Minimal stand-in for a leveldb/rocksdb handle: get() returns None on miss.'''

    def get(self, key, default=None):
        return dict.get(self, key, default)

    def iterator(self, prefix=b'', reverse=False):
        # Mirror the real backend: yield (key, value) for keys under `prefix`
        # in ascending key order (clear_excess_undo_info relies on the ascending
        # height sort within a prefix to break early).
        items = sorted((k, v) for k, v in self.items() if k.startswith(prefix))
        if reverse:
            items = list(reversed(items))
        return iter(items)

    def write_batch(self):
        return _Batch(self)


def _bare_db():
    '''A DB instance with only the bits these unit tests touch.

    DB.__init__ chdirs and opens real storage, so build an uninitialised
    instance and wire in an in-memory utxo_db. The key-builder and
    read/flush undo methods are pure functions of (height, utxo_db).
    '''
    db = DB.__new__(DB)
    db.utxo_db = FakeKVStore()
    return db


def test_ref_loc_undo_key_uses_RU_prefix():
    db = _bare_db()
    # Writer key and reader key must agree, and use the b'RU' prefix.
    assert db.ref_loc_undo_key(123) == b'RU' + pack_be_uint32(123)
    # And must NOT collide with the plain UTXO undo key.
    assert db.ref_loc_undo_key(123) != db.undo_key(123)


def test_ref_loc_undo_roundtrip():
    '''Write ref-loc undo info via the writer, read it back via the patched
    reader. Pre-fix the reader looked under b'U' + height and got None.'''
    db = _bare_db()
    height = 4567

    # Two synthetic (ref(36) + loc(32)) = 68-byte undo entries for this height.
    ref_a = b'\xaa' * 36
    loc_a = b'\x11' * 32
    ref_b = b'\xbb' * 36
    loc_b = b'\x22' * 32
    undo_info = [ref_a + loc_a, ref_b + loc_b]

    def batch_put(key, value):
        db.utxo_db[key] = value

    db.flush_ref_loc_undo_infos(batch_put, [(undo_info, height)])

    # The data must live under the b'RU' key...
    assert db.utxo_db.get(db.ref_loc_undo_key(height)) is not None
    # ...and the plain UTXO undo key must be empty for this height.
    assert db.utxo_db.get(db.undo_key(height)) is None

    # The patched reader must find it.
    read_back = db.read_ref_loc_undo_info(height)
    assert read_back == b''.join(undo_info)

    # And it must round-trip in 68-byte chunks (ref + loc) intact.
    assert read_back[0:36] == ref_a
    assert read_back[36:68] == loc_a
    assert read_back[68:104] == ref_b
    assert read_back[104:136] == loc_b


def test_ref_loc_undo_missing_height_returns_none():
    db = _bare_db()
    assert db.read_ref_loc_undo_info(999999) is None


# --- P0.2 #2: diff_pos must reference hashes1, not 'hashes' --------------------

def _diff_pos(hashes1, hashes2):
    '''Copy of the inner diff_pos from BlockProcessor._calc_reorg_range so we
    can unit-test the fix without spinning up the whole processor. Must stay in
    sync with block_processor.py; the regression is the final return value.'''
    for n, (hash1, hash2) in enumerate(zip(hashes1, hashes2)):
        if hash1 != hash2:
            return n
    return len(hashes1)


def test_diff_pos_all_match_returns_length():
    hashes = [b'a', b'b', b'c']
    # Pre-fix this raised NameError('hashes') because the local was hashes1.
    assert _diff_pos(hashes, list(hashes)) == 3


def test_diff_pos_first_difference():
    assert _diff_pos([b'a', b'b', b'c'], [b'a', b'X', b'c']) == 1


def test_block_processor_diff_pos_no_stray_name():
    '''Guard against the original bug regressing in the real source: the inner
    function must not reference a bare 'hashes' free variable.'''
    import inspect
    from electrumx.server import block_processor

    src = inspect.getsource(block_processor.BlockProcessor._calc_reorg_range)
    assert 'return len(hashes1)' in src
    assert 'return len(hashes)\n' not in src


# --- P0.4 #3: clear_excess_undo_info must GC the b'RU' ref-loc undo keys -------

def _gc_db(db_height, reorg_limit):
    '''A DB wired up just enough to run clear_excess_undo_info: an in-memory
    utxo_db, a db_height, and an env exposing reorg_limit (min_undo_height uses
    it).  The logger is a no-op so we don't depend on logging config.'''
    db = _bare_db()
    db.db_height = db_height
    db.env = types.SimpleNamespace(reorg_limit=reorg_limit)
    db.logger = types.SimpleNamespace(info=lambda *a, **k: None)
    return db


def test_clear_excess_undo_info_gcs_ru_keys():
    '''The ref-loc undo keys (b'RU' + height) sort under b'R', so the original
    b'U'-only sweep never reached them and they grew unboundedly.  The GC must
    now drop b'RU' undo strictly below the keep window while preserving both
    b'U' and b'RU' undo inside it.'''
    reorg_limit = 10
    db_height = 100
    db = _gc_db(db_height, reorg_limit)
    # Keep window: heights >= min_undo_height(100) = 100 - 10 + 1 = 91.
    min_height = db.min_undo_height(db_height)
    assert min_height == 91

    stale_h = 50      # well below the window -> must be deleted
    in_window_h = 95  # inside the window -> must be kept

    db.utxo_db[db.undo_key(stale_h)] = b'u-stale'
    db.utxo_db[db.ref_loc_undo_key(stale_h)] = b'ru-stale'
    db.utxo_db[db.undo_key(in_window_h)] = b'u-keep'
    db.utxo_db[db.ref_loc_undo_key(in_window_h)] = b'ru-keep'

    db.clear_excess_undo_info()

    # Stale entries of BOTH prefixes are gone...
    assert db.undo_key(stale_h) not in db.utxo_db
    assert db.ref_loc_undo_key(stale_h) not in db.utxo_db, \
        'b\'RU\' stale ref-loc undo was never garbage-collected (the bug)'
    # ...and in-window entries of BOTH prefixes survive.
    assert db.undo_key(in_window_h) in db.utxo_db
    assert db.ref_loc_undo_key(in_window_h) in db.utxo_db


def test_clear_excess_undo_info_sweeps_both_prefixes_in_source():
    '''Source-level guard: the GC must iterate both b'U' and b'RU' prefixes.'''
    import inspect
    src = inspect.getsource(DB.clear_excess_undo_info)
    assert "prefix=b'U'" in src
    assert "prefix=b'RU'" in src
