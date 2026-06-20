# Regression tests for the P0.4 reorg-time UTXO desync fix.
#
# THE BUG: advance_txs gated put_utxo on the per-output script parse
# (Script.zero_refs / hashX / codeScriptHash) and skipped the output if that
# parse raised, so the UTXO was never added.  _backup_txs, however, decided
# whether to spend_utxo using a DIFFERENT predicate (is_unspendable_legacy
# only).  A consensus-valid but degenerate scriptPubKey -- the canonical case
# being b'\x05ab', a direct-push opcode claiming 5 bytes with only 2 present --
# has is_unspendable_legacy == False yet makes Script.zero_refs RAISE.  So
# advance skipped the output (never added) while backup still called spend_utxo
# on it -> ChainError 'UTXO not found' -> the reorg HALTS.
#
# THE FIX: both paths route their add/spend decision through one shared
# predicate, _output_indexable, so advance-add and backup-spend cover the
# identical output set by construction.

import inspect

import pytest

from electrumx.server import block_processor
from electrumx.server.block_processor import BlockProcessor, ChainError
from electrumx.lib.coins import Radiant
from electrumx.lib.script import Script, ScriptError, is_unspendable_legacy
from electrumx.lib.tx import Tx, TxInput, TxOutput, ZERO, MINUS_1


# The exact output the old backup predicate mishandled: a direct-push opcode
# (0x05) declaring 5 data bytes when only 2 follow -> truncated script.
DEGENERATE = b'\x05ab'

# A well-formed P2PKH script: OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG.
P2PKH = bytes([0x76, 0xa9, 0x14]) + b'\x11' * 20 + bytes([0x88, 0xac])


def _bp():
    '''A BlockProcessor with only coin wired in -- enough for _output_indexable,
    which is a pure function of (coin, pk_script).'''
    bp = BlockProcessor.__new__(BlockProcessor)
    bp.coin = Radiant
    return bp


# --- The crux: the helper catches the case the old backup predicate missed ----

def test_degenerate_output_is_the_documented_trap():
    '''b'\x05ab' is exactly the consensus-valid-but-unparsable shape: the OLD
    backup predicate (is_unspendable_legacy) says "spend it" while the parse
    advance uses to gate the add (zero_refs) RAISES -> desync.'''
    # Not unspendable: the old backup predicate would have spent it.
    assert is_unspendable_legacy(DEGENERATE) is False
    # But the put_utxo gate raises, so advance never added it.
    with pytest.raises(ScriptError):
        Script.zero_refs(DEGENERATE)
    # The shared predicate folds both facts together -> skip on both paths.
    assert _bp()._output_indexable(DEGENERATE) is False


def test_output_indexable_round_trip_helper():
    '''Single assertion bundle the task pins:
       _output_indexable(b'\x05ab') is False
       AND is_unspendable_legacy(b'\x05ab') is False
       AND Script.zero_refs(b'\x05ab') raises.'''
    bp = _bp()
    assert bp._output_indexable(DEGENERATE) is False
    assert is_unspendable_legacy(DEGENERATE) is False
    with pytest.raises(ScriptError):
        Script.zero_refs(DEGENERATE)


def test_output_indexable_accepts_normal_p2pkh():
    assert _bp()._output_indexable(P2PKH) is True


def test_output_indexable_skips_unspendable_opreturn():
    op_return = bytes([0x6a, 0x01, 0x00])  # OP_RETURN <1-byte push>
    assert is_unspendable_legacy(op_return) is True
    assert _bp()._output_indexable(op_return) is False


# --- Full advance -> backup integration: no ChainError on the trap output -----

class _FakeKV(dict):
    '''Minimal leveldb/rocksdb stand-in.'''

    def get(self, key, default=None):
        return dict.get(self, key, default)

    def iterator(self, prefix=b'', reverse=False):
        return iter([])


class _FakeHistory:
    def add_unflushed(self, *args, **kwargs):
        pass


class _FakeDB:
    def __init__(self):
        self.utxo_db = _FakeKV()
        self.history = _FakeHistory()
        self.tx_counts = []
        self._undo = {}
        self._ref_loc_undo = {}

    def read_undo_info(self, height):
        return self._undo.get(height, b'')

    def read_ref_loc_undo_info(self, height):
        return self._ref_loc_undo.get(height, b'')


def _integration_bp():
    bp = BlockProcessor.__new__(BlockProcessor)
    bp.coin = Radiant
    bp.db = _FakeDB()
    bp.utxo_cache = {}
    bp.ref_cache = {}
    bp.ref_mint_cache = {}
    bp.ref_loc_cache = {}
    bp.data_cache = {}
    bp.db_deletes = []
    bp.touched = set()
    bp.tx_count = 0
    bp.tx_hashes = []
    bp.height = 0
    return bp


def test_advance_then_backup_with_degenerate_output_no_chainerror():
    '''A block whose tx pays both a normal output and the degenerate b'\x05ab'
    output must advance then back up cleanly.  Pre-fix, backup tried to
    spend_utxo the degenerate output advance never added -> ChainError, halting
    the reorg.  The coinbase (generation) input keeps undo_info empty so the
    output spend path -- the regression surface -- is exercised in isolation.'''
    bp = _integration_bp()

    coinbase_in = TxInput(ZERO, MINUS_1, b'\x00', 0xffffffff)
    tx = Tx(1, [coinbase_in],
            [TxOutput(5000, P2PKH), TxOutput(0, DEGENERATE)], 0)
    tx_hash = b'\xcd' * 32
    txs = [(tx, tx_hash)]

    # Advance: only the P2PKH output is indexed; the degenerate one is skipped.
    undo_info, ref_loc_undo_info = bp.advance_txs(txs, is_unspendable_legacy)
    assert len(bp.utxo_cache) == 1, \
        'exactly one output (the P2PKH) should have been put_utxo`d'

    # Wire the undo info up for the backup at the now-applied height.
    bp.height = 1
    bp.tx_count = 1
    bp.db._undo[1] = b''.join(undo_info)
    bp.db._ref_loc_undo[1] = b''.join(ref_loc_undo_info)

    # Backup must not raise: it spends exactly the outputs advance added.
    bp._backup_txs(txs, is_unspendable_legacy)
    assert len(bp.utxo_cache) == 0, \
        'backup should have spent exactly the output advance added'


def test_backup_would_chainerror_if_degenerate_spent_unconditionally():
    '''Proves the test above actually guards the bug: had backup followed the
    OLD predicate (is_unspendable_legacy says spendable) it would spend_utxo a
    UTXO advance never created -> ChainError.  We trigger that condition
    directly to confirm spend_utxo raises on the missing output.'''
    bp = _integration_bp()
    assert is_unspendable_legacy(DEGENERATE) is False  # old path would spend it
    with pytest.raises(ChainError):
        bp.spend_utxo(b'\xcd' * 32, 1)  # never put_utxo'd -> not found


# --- Source-level guard: both paths route through the one shared predicate ----

def test_advance_and_backup_route_through_output_indexable():
    '''Structural guarantee: the add/spend decision in BOTH advance_txs and
    _backup_txs must go through _output_indexable, so the two paths cover the
    identical output set by construction (not by catch-and-continue alone).'''
    advance_src = inspect.getsource(BlockProcessor.advance_txs)
    backup_src = inspect.getsource(BlockProcessor._backup_txs)

    assert 'self._output_indexable(' in advance_src, \
        'advance_txs must gate put_utxo on _output_indexable'
    assert 'self._output_indexable(' in backup_src, \
        'backup_txs must gate spend_utxo on _output_indexable'

    # The helper itself must use the identical parse + exception set advance
    # uses to gate put_utxo, so it cannot disagree with the put_utxo gate.
    helper_src = inspect.getsource(BlockProcessor._output_indexable)
    assert 'Script.zero_refs(' in helper_src
    assert '(ScriptError, AssertionError, ValueError, IndexError)' in helper_src
    assert 'is_unspendable_legacy(' in helper_src


def test_block_processor_module_imports_chainerror():
    '''Guard: ChainError must remain importable for the reorg path it guards.'''
    assert hasattr(block_processor, 'ChainError')
