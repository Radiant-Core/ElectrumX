# Regression tests for P0.2 reorg fixes:
#   1. ref-loc/WAVE undo info must be read back under the same key it was
#      written (b'RU' + height), not under the plain UTXO undo key (b'U' + ...).
#   2. block_processor diff_pos must return len(hashes1), not a nonexistent
#      'hashes' name, when two hash lists fully agree.

from electrumx.server.db import DB
from electrumx.lib.util import pack_be_uint32


class FakeKVStore(dict):
    '''Minimal stand-in for a leveldb/rocksdb handle: get() returns None on miss.'''

    def get(self, key, default=None):
        return dict.get(self, key, default)


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
