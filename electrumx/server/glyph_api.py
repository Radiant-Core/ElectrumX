"""
Glyph v2 Token API Extensions for ElectrumX

This module provides RPC API methods for querying Glyph v2 tokens.
These methods can be added to the ElectrumX session handler.

Reference: https://github.com/Radiant-Core/Glyph-Token-Standards
"""

from electrumx.lib.glyph import (
    GLYPH_MAGIC,
    GlyphProtocol,
    GlyphVersion,
    parse_glyph_envelope,
    get_token_type,
    get_protocol_name,
    validate_protocols,
    is_fungible,
    is_nft,
    is_dmint,
    format_glyph_id,
    parse_glyph_id,
)
from electrumx.lib.hash import hash_to_hex_str


class GlyphAPIMixin:
    """
    Mixin class providing Glyph v2 token API methods.
    
    Add this to the ElectrumX session class to enable Glyph queries.
    
    Example:
        class ElectrumX(GlyphAPIMixin, SessionBase):
            ...
    """

    async def glyph_get_token(self, glyph_id: str):
        """
        Get token information by Glyph ID.
        
        Args:
            glyph_id: Token ID in format "txid:vout"
            
        Returns:
            Token information dict or None if not found
        """
        self.bump_cost(1.0)
        
        try:
            txid, vout = parse_glyph_id(glyph_id)
        except (ValueError, IndexError):
            return {'error': 'Invalid glyph_id format. Expected txid:vout'}
        
        # Fetch the transaction
        try:
            raw_tx = await self.daemon_request('getrawtransaction', txid, True)
        except Exception:
            return None
        
        if not raw_tx or 'vout' not in raw_tx:
            return None
        
        if vout >= len(raw_tx['vout']):
            return None
        
        output = raw_tx['vout'][vout]
        script_hex = output.get('scriptPubKey', {}).get('hex', '')
        
        if not script_hex:
            return None
        
        script_bytes = bytes.fromhex(script_hex)
        envelope = parse_glyph_envelope(script_bytes)
        
        if not envelope:
            return None
        
        result = {
            'glyph_id': glyph_id,
            'txid': txid,
            'vout': vout,
            'value': int(output.get('value', 0) * 100_000_000),
            'version': envelope.get('version'),
            'is_reveal': envelope.get('is_reveal', False),
        }
        
        if envelope.get('commit_hash'):
            result['commit_hash'] = envelope['commit_hash']
        
        if envelope.get('content_root'):
            result['content_root'] = envelope['content_root']
        
        return result

    async def glyph_get_by_ref(self, ref: str):
        """
        Get all UTXOs containing a specific reference.
        
        Args:
            ref: 36-byte reference in hex (72 characters)
            
        Returns:
            List of UTXOs with the reference
        """
        self.bump_cost(2.0)
        
        if len(ref) != 72:
            return {'error': 'Invalid ref format. Expected 72 hex characters'}
        
        try:
            ref_bytes = bytes.fromhex(ref)
        except ValueError:
            return {'error': 'Invalid hex in ref'}
        
        # Query the database for UTXOs with this reference
        utxos = await self.db.get_utxos_by_ref(ref_bytes)
        
        result = []
        for utxo in utxos:
            result.append({
                'tx_hash': hash_to_hex_str(utxo.tx_hash),
                'tx_pos': utxo.tx_pos,
                'height': utxo.height,
                'value': utxo.value,
            })
        
        return result

    async def glyph_validate_protocols(self, protocols: list):
        """
        Validate a protocol combination per Glyph v2 rules.
        
        Args:
            protocols: List of protocol IDs
            
        Returns:
            Validation result with any errors
        """
        self.bump_cost(0.1)
        
        if not isinstance(protocols, list):
            return {'valid': False, 'error': 'protocols must be a list'}
        
        valid, error = validate_protocols(protocols)
        
        result = {'valid': valid}
        if error:
            result['error'] = error
        
        # Add protocol names for convenience
        result['protocol_names'] = [get_protocol_name(p) for p in protocols]
        result['token_type'] = get_token_type(protocols)
        
        return result

    async def glyph_get_protocol_info(self):
        """
        Get information about all Glyph v2 protocols.
        
        Returns:
            Dict with protocol definitions
        """
        self.bump_cost(0.1)
        
        return {
            'version': GlyphVersion.V2,
            'magic': GLYPH_MAGIC.hex(),
            'protocols': {
                'GLYPH_FT': {
                    'id': GlyphProtocol.GLYPH_FT,
                    'name': 'Fungible Token',
                    'description': 'Standard fungible token',
                },
                'GLYPH_NFT': {
                    'id': GlyphProtocol.GLYPH_NFT,
                    'name': 'Non-Fungible Token',
                    'description': 'Unique digital asset',
                },
                'GLYPH_DAT': {
                    'id': GlyphProtocol.GLYPH_DAT,
                    'name': 'Data Storage',
                    'description': 'On-chain data storage',
                },
                'GLYPH_DMINT': {
                    'id': GlyphProtocol.GLYPH_DMINT,
                    'name': 'Decentralized Minting',
                    'description': 'Proof-of-work token distribution',
                    'requires': ['GLYPH_FT'],
                },
                'GLYPH_MUT': {
                    'id': GlyphProtocol.GLYPH_MUT,
                    'name': 'Mutable State',
                    'description': 'Updateable token metadata',
                    'requires': ['GLYPH_NFT'],
                },
                'GLYPH_BURN': {
                    'id': GlyphProtocol.GLYPH_BURN,
                    'name': 'Explicit Burn',
                    'description': 'Verifiable token destruction',
                },
                'GLYPH_CONTAINER': {
                    'id': GlyphProtocol.GLYPH_CONTAINER,
                    'name': 'Container',
                    'description': 'Collection or grouping of tokens',
                    'requires': ['GLYPH_NFT'],
                },
                'GLYPH_ENCRYPTED': {
                    'id': GlyphProtocol.GLYPH_ENCRYPTED,
                    'name': 'Encrypted Content',
                    'description': 'Private token content',
                    'requires': ['GLYPH_NFT'],
                },
                'GLYPH_TIMELOCK': {
                    'id': GlyphProtocol.GLYPH_TIMELOCK,
                    'name': 'Timelocked Reveal',
                    'description': 'Time-delayed content reveal',
                },
                'GLYPH_AUTHORITY': {
                    'id': GlyphProtocol.GLYPH_AUTHORITY,
                    'name': 'Authority Token',
                    'description': 'Delegated minting/management rights',
                    'requires': ['GLYPH_NFT'],
                },
                'GLYPH_WAVE': {
                    'id': GlyphProtocol.GLYPH_WAVE,
                    'name': 'WAVE Name',
                    'description': 'Human-readable naming',
                    'requires': ['GLYPH_NFT', 'GLYPH_MUT'],
                },
            },
        }

    async def glyph_parse_envelope(self, script_hex: str):
        """
        Parse a Glyph envelope from script hex.
        
        Args:
            script_hex: Script in hexadecimal
            
        Returns:
            Parsed envelope or None
        """
        self.bump_cost(0.5)
        
        try:
            script_bytes = bytes.fromhex(script_hex)
        except ValueError:
            return {'error': 'Invalid hex string'}
        
        envelope = parse_glyph_envelope(script_bytes)
        
        if not envelope:
            return None
        
        return envelope


# Method registration for ElectrumX
GLYPH_METHODS = {
    'glyph.get_token': 'glyph_get_token',
    'glyph.get_by_ref': 'glyph_get_by_ref',
    'glyph.validate_protocols': 'glyph_validate_protocols',
    'glyph.get_protocol_info': 'glyph_get_protocol_info',
    'glyph.parse_envelope': 'glyph_parse_envelope',
}
