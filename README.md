**= ElectrumX for Radiant =**
=============================


2026 The Radiant Community Devs

:Licence: MIT
:Language: Python (>= 3.8)
:Database: RocksDB (default) or LevelDB
:Version: 1.3.0

A high-performance Electrum server implementation for the Radiant blockchain.

Features
========

- **RocksDB Support**: Production-optimized database backend with lower steady-state RAM
- **Swap Index**: Native support for RSWP on-chain swap advertisement tracking
- **High Performance**: Optimized for Radiant's UTXO model and transaction volume
- **Docker Ready**: Production-ready Docker images with resource limits
- **SSL/TLS**: Built-in support for encrypted connections

Quick Start (Docker)
====================

The fastest way to deploy ElectrumX for Radiant is using Docker.

Prerequisites
-------------

- Docker and Docker Compose installed
- A running Radiant Core node with RPC access
- At least 16GB RAM recommended for initial sync

1. Clone and Configure
----------------------

.. code-block:: bash

    git clone https://github.com/radiantblockchain/electrumx.git
    cd electrumx

    # Copy the example config
    cp .env.rocksdb.example .env

    # Edit with your Radiant node credentials
    vi .env

2. Configure Environment
------------------------

Edit ``.env`` with your settings:

.. code-block:: bash

    # Required: Your Radiant Core RPC credentials
    DAEMON_URL=http://YOUR_RPC_USER:YOUR_RPC_PASSWORD@localhost:7332/

    # Network
    COIN=Radiant
    NET=mainnet

    # Database (rocksdb recommended for production)
    DB_ENGINE=rocksdb
    DB_DIRECTORY=/root/electrumdb

    # Services to expose
    SERVICES=tcp://0.0.0.0:50010,SSL://0.0.0.0:50012,rpc://

3. Generate SSL Certificates
----------------------------

For production, use proper CA-signed certificates. For testing:

.. code-block:: bash

    mkdir -p electrumdb
    openssl req -x509 -nodes -days 365 -newkey rsa:4096 \
        -keyout electrumdb/server.key \
        -out electrumdb/server.crt \
        -subj "/CN=your.domain.com"

4. Build and Run
----------------

.. code-block:: bash

    # Build the image
    docker-compose build

    # Start in background
    docker-compose up -d

    # View logs
    docker logs -f electrumx_server

    # Graceful shutdown
    docker-compose down

Manual Installation
===================

For non-Docker deployments:

.. code-block:: bash

    # Install system dependencies (Ubuntu/Debian)
    sudo apt update
    sudo apt install -y python3 python3-pip python3-dev \
        libleveldb-dev librocksdb-dev libsnappy-dev \
        libbz2-dev libzstd-dev liblz4-dev zlib1g-dev

    # Clone repository
    git clone https://github.com/radiantblockchain/electrumx.git
    cd electrumx

    # Install Python dependencies
    pip3 install -r requirements.txt

    # Set environment variables (or use a .env file)
    export COIN=Radiant
    export NET=mainnet
    export DB_ENGINE=rocksdb
    export DB_DIRECTORY=/path/to/electrumdb
    export DAEMON_URL=http://user:pass@localhost:7332/
    export SERVICES=tcp://0.0.0.0:50010,rpc://

    # Run
    python3 electrumx_server

Database Backends
=================

RocksDB (Default)
-----------------

- **~52% lower steady-state RAM** (561MB vs 1.17GB observed)
- Better write amplification control
- More tuning options
- Production recommended

LevelDB
-------

- Legacy database used by ElectrumX
- Higher steady-state RAM usage


Set ``DB_ENGINE=leveldb`` to use LevelDB instead.

RocksDB Tuning
--------------

Key environment variables for RocksDB performance:

.. code-block:: bash

    # Compression (lz4 recommended)
    ROCKSDB_COMPRESSION=lz4

    # Block cache - main read performance lever (MB)
    ROCKSDB_BLOCK_CACHE_MB=256

    # Write buffer size (bytes)
    ROCKSDB_WRITE_BUFFER_SIZE=67108864

    # Background jobs
    ROCKSDB_MAX_BACKGROUND_COMPACTIONS=4
    ROCKSDB_MAX_BACKGROUND_FLUSHES=2

    # Durability (true for production serving)
    ROCKSDB_USE_FSYNC=true

See ``docs/environment.rst`` for all available options.

Production Recommendations
==========================

Security
--------

1. **Enable Rate Limiting**: Never set ``COST_SOFT_LIMIT=0`` or ``COST_HARD_LIMIT=0`` in production

   .. code-block:: bash

       COST_SOFT_LIMIT=1000
       COST_HARD_LIMIT=10000

2. **Use Strong RPC Credentials**: Generate random passwords for ``DAEMON_URL``

3. **SSL Certificates**: Use CA-signed certificates for public servers

4. **Run as Non-Root**: Set ``ALLOW_ROOT=false`` when possible

5. **Firewall**: Only expose necessary ports (50010/tcp, 50012/ssl)

Performance
-----------

1. **Use RocksDB** for lower steady-state memory

2. **Tune Cache Size** based on available RAM:

   .. code-block:: bash

       CACHE_MB=10000  # For 16GB+ RAM systems

3. **Set Resource Limits** in Docker:

   .. code-block:: yaml

       deploy:
         resources:
           limits:
             memory: 12G

4. **Use SSD Storage** for the database directory

Monitoring
----------

Monitor these metrics in production:

- RSS memory usage
- Database size (``du -sh /path/to/electrumdb``)
- Sync status via RPC
- Connection count

RPC Commands
============

ElectrumX exposes an RPC interface (default port 8000):

.. code-block:: bash

    # Inside Docker container
    docker exec electrumx_server python3 electrumx_rpc getinfo

    # Or using the script directly
    ./electrumx_rpc getinfo
    ./electrumx_rpc sessions
    ./electrumx_rpc peers

Common RPC commands:

- ``getinfo`` - Server status and sync progress
- ``sessions`` - Connected client sessions
- ``peers`` - Known peer servers
- ``stop`` - Graceful shutdown

Swap Index (RSWP)
=================

ElectrumX supports indexing on-chain swap advertisements:

.. code-block:: bash

    # Enable swap indexing
    SWAP_INDEX=1
    SWAP_HISTORY_BLOCKS=10000
    SWAP_CACHE_MB=10

Swap RPC methods:

- ``getopenorders(token_ref, limit, offset)``
- ``getswaphistory(token_ref, limit, offset)``
- ``getswapcount(token_ref)``

Troubleshooting
===============

"Connection refused" to daemon
------------------------------

Ensure Radiant Core is running with RPC enabled:

.. code-block:: bash

    # In radiant.conf
    server=1
    rpcuser=youruser
    rpcpassword=yourpassword
    rpcallowip=127.0.0.1
    rpcport=7332

High memory usage during sync
-----------------------------

This is normal. Memory usage drops significantly after initial sync completes.
With RocksDB, steady-state RAM is typically under 500MB.

Slow initial sync
-----------------

Initial sync can take 1-2 hours depending on hardware. To speed up:

- Increase ``CACHE_MB``
- Use SSD storage
- Ensure Radiant Core is fully synced first

"Module not found: rocksdb"
---------------------------

Install the Python RocksDB bindings:

.. code-block:: bash

    pip3 install Cython python-rocksdb

Docker permission errors
------------------------

Ensure the ``electrumdb`` directory is writable:

.. code-block:: bash

    mkdir -p electrumdb
    chmod 755 electrumdb

Documentation
=============

- Full environment variables: ``docs/environment.rst``
- Architecture: ``docs/architecture.rst``
- Performance notes: ``docs/PERFORMANCE-NOTES``
- API protocol: ``docs/protocol-*.rst``

Contributing
============

1. Fork the repository
2. Create a feature branch
3. Run tests: ``pytest tests/``
4. Submit a pull request

License
=======

MIT License. See ``LICENCE`` file for details.

Links
=====

- `Radiant Blockchain <https://radiantblockchain.org>`_
- `Original ElectrumX <https://github.com/kyuupichan/electrumx>`_
- `ElectrumX Documentation <https://electrumx.readthedocs.io/>`_
