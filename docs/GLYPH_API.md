# Glyph v2 API Reference

## Overview

ElectrumX-Core provides RPC methods for querying Glyph v2 tokens. All methods are prefixed with `glyph.` and are available through the standard ElectrumX JSON-RPC interface.

## Methods

### glyph.get_token

Get token information by Glyph ID.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `glyph_id` | string | Token ID in format `txid:vout` |

**Returns:**
```json
{
  "glyph_id": "abc123...def:0",
  "txid": "abc123...def",
  "vout": 0,
  "value": 100000000,
  "version": 2,
  "is_reveal": true,
  "commit_hash": "...",
  "content_root": "..."
}
```

**Example:**
```javascript
const result = await client.request('glyph.get_token', ['abc123...def:0']);
```

---

### glyph.get_by_ref

Get all UTXOs containing a specific reference.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `ref` | string | 36-byte reference in hex (72 characters) |

**Returns:**
```json
[
  {
    "tx_hash": "abc123...",
    "tx_pos": 0,
    "height": 123456,
    "value": 100000000
  }
]
```

---

### glyph.validate_protocols

Validate a protocol combination per Glyph v2 rules.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `protocols` | array | List of protocol IDs |

**Returns:**
```json
{
  "valid": true,
  "protocol_names": ["Fungible Token", "Decentralized Minting"],
  "token_type": "dMint FT"
}
```

**Error Response:**
```json
{
  "valid": false,
  "error": "FT and NFT are mutually exclusive"
}
```

---

### glyph.get_protocol_info

Get information about all Glyph v2 protocols.

**Parameters:** None

**Returns:**
```json
{
  "version": 2,
  "magic": "676c79",
  "protocols": {
    "GLYPH_FT": {
      "id": 1,
      "name": "Fungible Token",
      "description": "Standard fungible token"
    },
    "GLYPH_NFT": {
      "id": 2,
      "name": "Non-Fungible Token",
      "description": "Unique digital asset"
    },
    "GLYPH_DAT": {
      "id": 3,
      "name": "Data Storage",
      "description": "On-chain data storage"
    },
    "GLYPH_DMINT": {
      "id": 4,
      "name": "Decentralized Minting",
      "description": "Proof-of-work token distribution",
      "requires": ["GLYPH_FT"]
    },
    "GLYPH_MUT": {
      "id": 5,
      "name": "Mutable State",
      "description": "Updateable token metadata",
      "requires": ["GLYPH_NFT"]
    },
    "GLYPH_BURN": {
      "id": 6,
      "name": "Explicit Burn",
      "description": "Verifiable token destruction"
    },
    "GLYPH_CONTAINER": {
      "id": 7,
      "name": "Container",
      "description": "Collection or grouping of tokens",
      "requires": ["GLYPH_NFT"]
    },
    "GLYPH_ENCRYPTED": {
      "id": 8,
      "name": "Encrypted Content",
      "description": "Private token content",
      "requires": ["GLYPH_NFT"]
    },
    "GLYPH_TIMELOCK": {
      "id": 9,
      "name": "Timelocked Reveal",
      "description": "Time-delayed content reveal",
      "requires": ["GLYPH_ENCRYPTED"]
    },
    "GLYPH_AUTHORITY": {
      "id": 10,
      "name": "Authority Token",
      "description": "Delegated minting/management rights",
      "requires": ["GLYPH_NFT"]
    },
    "GLYPH_WAVE": {
      "id": 11,
      "name": "WAVE Name",
      "description": "Human-readable naming",
      "requires": ["GLYPH_NFT", "GLYPH_MUT"]
    }
  }
}
```

---

### glyph.parse_envelope

Parse a Glyph envelope from script hex.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `script_hex` | string | Script in hexadecimal |

**Returns:**
```json
{
  "is_reveal": true,
  "version": 2,
  "protocols": [1, 4],
  "metadata_bytes": "...",
  "commit_hash": "...",
  "content_root": "..."
}
```

---

## Protocol IDs

| ID | Name | Description |
|----|------|-------------|
| 1 | GLYPH_FT | Fungible Token |
| 2 | GLYPH_NFT | Non-Fungible Token |
| 3 | GLYPH_DAT | Data Storage |
| 4 | GLYPH_DMINT | Decentralized Minting (requires FT) |
| 5 | GLYPH_MUT | Mutable State (requires NFT) |
| 6 | GLYPH_BURN | Explicit Burn (requires FT or NFT) |
| 7 | GLYPH_CONTAINER | Container/Collection (requires NFT) |
| 8 | GLYPH_ENCRYPTED | Encrypted Content (requires NFT) |
| 9 | GLYPH_TIMELOCK | Timelocked Reveal (requires ENCRYPTED) |
| 10 | GLYPH_AUTHORITY | Issuer Authority (requires NFT) |
| 11 | GLYPH_WAVE | WAVE Naming (requires NFT + MUT) |

## Protocol Combination Rules

Per Glyph v2 spec Section 3.5:

- **FT and NFT are mutually exclusive** - A token cannot be both fungible and non-fungible
- **DMINT requires FT** - Decentralized minting only applies to fungible tokens
- **MUT requires NFT** - Mutable state only applies to non-fungible tokens
- **CONTAINER requires NFT** - Containers must be NFTs
- **ENCRYPTED requires NFT** - Encrypted content requires NFT
- **TIMELOCK requires ENCRYPTED** - Timelocks require encryption
- **AUTHORITY requires NFT** - Authority tokens are NFTs
- **WAVE requires NFT + MUT** - WAVE names are mutable NFTs
- **BURN requires FT or NFT** - Burn is an action marker, not a standalone type

## Error Handling

All methods return `null` if the requested item is not found. For validation errors, a dict with an `error` key is returned:

```json
{
  "error": "Invalid glyph_id format. Expected txid:vout"
}
```

## Cost

Each method has an associated cost that counts towards rate limiting:

| Method | Cost |
|--------|------|
| glyph.get_token | 1.0 |
| glyph.get_by_ref | 2.0 |
| glyph.validate_protocols | 0.1 |
| glyph.get_protocol_info | 0.1 |
| glyph.parse_envelope | 0.5 |

---

*Reference: [Glyph v2 Token Standard](https://github.com/Radiant-Core/Glyph-Token-Standards)*
