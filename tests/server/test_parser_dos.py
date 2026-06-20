# Regression tests for P0.3 block-parser DoS hardening.
#
# Goal: a single malformed transaction / script must never raise an uncaught
# exception out of the indexer's parse path. These are targeted, deterministic
# cases (oversize OP_PUSHDATA4 length, oversize declared input/output counts,
# 9-byte varint). A full atheris/hypothesis fuzz harness is a follow-up.

import pytest

from electrumx.lib.script import (
    Script, ScriptError, MAX_SCRIPT_PUSH, OpCodes,
)
from electrumx.lib.tx import Deserializer
from electrumx.lib.util import pack_le_uint32


# --- P0.3 #2: oversize OP_PUSHDATA* lengths are rejected (and caught) ---------

def test_get_ops_rejects_oversize_pushdata4():
    # OP_PUSHDATA4 claims 0xFFFFFFFF bytes but the buffer is tiny.
    script = bytes([OpCodes.OP_PUSHDATA4]) + b'\xff\xff\xff\xff'
    with pytest.raises(ScriptError):
        Script.get_ops(script)


def test_get_push_input_refs_rejects_oversize_pushdata4():
    script = bytes([OpCodes.OP_PUSHDATA4]) + b'\xff\xff\xff\xff'
    with pytest.raises(ScriptError):
        Script.get_push_input_refs(script)


def test_pushdata4_just_over_cap_rejected_even_if_buffer_were_huge():
    # dlen = MAX_SCRIPT_PUSH + 1; cap must fire before any slice work.
    dlen = MAX_SCRIPT_PUSH + 1
    script = bytes([OpCodes.OP_PUSHDATA4]) + pack_le_uint32(dlen)
    with pytest.raises(ScriptError):
        Script.get_ops(script)
    with pytest.raises(ScriptError):
        Script.get_push_input_refs(script)


def test_pushdata_within_cap_and_buffer_is_fine():
    # A small, well-formed push must still parse without error (cap not tripped).
    data = b'\x01\x02\x03\x04'
    script = bytes([len(data)]) + data  # direct push opcode (len < 76)
    ops = Script.get_ops(script)  # must not raise
    # get_ops yields one op for the single push; the decoded push data is present.
    assert len(ops) == 1
    flat = repr(ops[0])
    assert repr(data) in flat
    # And it is not a ref-bearing script.
    all_refs, normal_refs, singleton_refs = Script.get_push_input_refs(script)
    assert all_refs == [] and normal_refs == [] and singleton_refs == []


# --- P0.3 #2 REGRESSION: the cap must NOT drop valid large pushes -------------
# The push cap must equal Radiant's consensus element size. A smaller value
# (the original 10_000) made get_push_input_refs() raise on a legitimate large
# glyph data push while zero_refs() still indexed the UTXO, silently dropping
# the token's ref (UTXO present, refs missing -> tokens invisible). These guard
# against ever re-introducing a sub-consensus cap.

def _glyph_script_with_large_push(payload_len):
    ref = b'\xab' * 36
    push = (bytes([OpCodes.OP_PUSHDATA4]) + pack_le_uint32(payload_len)
            + b'\x00' * payload_len)
    return push + bytes([OpCodes.OP_PUSHINPUTREFSINGLETON]) + ref, ref


def test_cap_equals_consensus_element_size():
    # Must match Radiant-Core src/script/script.h MAX_SCRIPT_ELEMENT_SIZE.
    assert MAX_SCRIPT_PUSH == 32_000_000


def test_large_glyph_push_does_not_drop_ref():
    # 12 KB payload: far above the old 10 KB cap, far below consensus 32 MB.
    # get_push_input_refs must return the singleton ref (not silently drop it),
    # and zero_refs (which indexes the UTXO) must agree by also succeeding.
    script, ref = _glyph_script_with_large_push(12_000)
    all_refs, normal_refs, singleton_refs = Script.get_push_input_refs(script)
    assert singleton_refs == [ref], 'large-data glyph singleton ref was dropped'
    assert ref in all_refs
    Script.zero_refs(script)  # must not raise


# --- P0.3 #3: declared input/output counts are sanity-capped ------------------

def _tx_prefix_with_input_count(count_varint_bytes):
    # version(4) + <input count varint> ... (no actual inputs supplied)
    return b'\x01\x00\x00\x00' + count_varint_bytes


def test_oversize_declared_input_count_raises_valueerror_not_memoryerror():
    # 0xFE => next 4 bytes are a uint32 count. Claim ~4 billion inputs in a
    # buffer with no room for any. Must raise ValueError, never OOM/hang.
    raw = _tx_prefix_with_input_count(b'\xfe\xff\xff\xff\xff')
    with pytest.raises(ValueError):
        Deserializer(raw).read_tx()


def test_oversize_declared_output_count_raises_valueerror():
    # version(4) + 0 inputs + output count = 0xFFFFFFFF via 0xFE prefix
    raw = b'\x01\x00\x00\x00' + b'\x00' + b'\xfe\xff\xff\xff\xff'
    with pytest.raises(ValueError):
        Deserializer(raw).read_tx()


def test_nine_byte_varint_huge_count_raises_valueerror():
    # 0xFF => next 8 bytes are a uint64 count (a 9-byte varint). A huge value
    # must be rejected by the remaining-buffer sanity cap.
    huge = (1 << 60).to_bytes(8, 'little')
    raw = _tx_prefix_with_input_count(b'\xff' + huge)
    with pytest.raises(ValueError):
        Deserializer(raw).read_tx()


def test_small_valid_counts_still_parse():
    # A real, tiny tx: version, 0 inputs, 0 outputs, locktime. Must parse clean.
    raw = (b'\x01\x00\x00\x00'  # version 1
           + b'\x00'            # 0 inputs
           + b'\x00'            # 0 outputs
           + b'\x00\x00\x00\x00')  # locktime
    tx = Deserializer(raw).read_tx()
    assert tx.inputs == [] and tx.outputs == []


# --- P0.3 #1: no malformed-script exception escapes the indexer parse path ----

def test_malformed_scripts_only_raise_caught_exception_types():
    '''advance_txs catches (ScriptError, AssertionError, ValueError,
    IndexError). Verify the script parsers only ever raise within that set for
    a spread of malformed inputs, so the per-tx try/except can never let one
    escape and halt the indexer.'''
    caught = (ScriptError, AssertionError, ValueError, IndexError)
    malformed = [
        bytes([OpCodes.OP_PUSHDATA4]) + b'\xff\xff\xff\xff',   # oversize len
        bytes([OpCodes.OP_PUSHDATA2]) + b'\xff\xff',           # truncated len
        bytes([OpCodes.OP_PUSHDATA1]),                         # missing len byte
        bytes([75]) + b'\x00\x00',                             # push 75 want, 2 have
        bytes([OpCodes.OP_PUSHINPUTREF]) + b'\x00' * 4,        # ref wants 36, 4 have
    ]
    for script in malformed:
        for fn in (Script.get_ops, Script.get_push_input_refs, Script.zero_refs):
            try:
                fn(script)
            except caught:
                pass  # acceptable: advance_txs handles these
            except Exception as e:  # noqa: BLE001  - the bug we are guarding against
                pytest.fail(
                    f'{fn.__name__} raised uncatchable {type(e).__name__}: {e}')


def test_advance_txs_wraps_output_parsing_in_try_except():
    '''Source-level guard: the per-output parsing in advance_txs must be wrapped
    so one malformed tx cannot halt the indexer.'''
    import inspect
    from electrumx.server import block_processor

    src = inspect.getsource(block_processor.BlockProcessor.advance_txs)
    assert 'except (ScriptError, AssertionError, ValueError, IndexError)' in src
    assert 'continue' in src  # malformed output is skipped, loop continues
