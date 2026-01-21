# Full Stack Deployment: Radiant Node + ElectrumX

One-command deployment for running a complete Radiant infrastructure with both the full node (radiantd) and ElectrumX server.

**This deployment is fully self-contained** - both radiantd and ElectrumX are built from source during the Docker build process:
- **Radiant Node**: Built from [Radiant-Core/Radiant-Core](https://github.com/Radiant-Core/Radiant-Core)
- **ElectrumX**: Built from [Radiant-Core/ElectrumX](https://github.com/Radiant-Core/ElectrumX)

You can deploy by downloading just this `docker/full-stack/` directory.

## Quick Start

1. **Copy environment file and configure:**
   ```bash
   cp .env.example .env
   # Edit .env with your RPC credentials
   ```

2. **Start the stack:**
   ```bash
   docker-compose up -d
   ```

3. **Monitor logs:**
   ```bash
   docker-compose logs -f
   ```

## Services

| Service | Port | Description |
|---------|------|-------------|
| radiantd | 7332 | RPC port |
| radiantd | 7333 | P2P port |
| electrumx | 50010 | TCP connections |
| electrumx | 50012 | SSL connections |
| electrumx | 8000 | RPC interface |

## Build & Sync Times

**First-time build** (compiles from source):
- radiantd: ~10-20 minutes
- electrumx: ~2-5 minutes

**Initial sync** (after build):
1. **radiantd** must fully sync the blockchain first (1-4 hours)
2. **electrumx** will wait (via healthcheck) until radiantd is ready
3. **electrumx** then indexes the blockchain (1-3 hours depending on hardware)

Monitor progress:
```bash
# Check radiantd sync status
docker exec radiantd radiant-cli -rpcuser=radiant -rpcpassword=your_pass getblockchaininfo

# Check electrumx status
docker logs -f electrumx_server
```

## Data Persistence

Data is stored in Docker volumes:
- `radiant-node-data` - Blockchain data (~50GB+)
- `electrumx-db-data` - ElectrumX index database

## Graceful Shutdown

ElectrumX requires a graceful shutdown to avoid database corruption:
```bash
docker-compose down
# Or for immediate but safe shutdown:
docker kill --signal="TERM" electrumx_server
```

## Production Recommendations

1. **Use a reverse proxy** (nginx/traefik) for SSL termination
2. **Set secure RPC credentials** in `.env`
3. **Increase CACHE_MB** if you have available RAM (improves sync speed)
4. **Use SSD storage** for both volumes
