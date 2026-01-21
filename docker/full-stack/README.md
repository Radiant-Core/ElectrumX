# Full Stack Deployment: Radiant Node + ElectrumX

One-command deployment for running a complete Radiant infrastructure with both the full node (radiantd) and ElectrumX server.

**This deployment is self-contained** - it clones ElectrumX directly from GitHub during build. You can deploy by downloading just this `docker/full-stack/` directory.

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

## Initial Sync

The first sync will take time:
1. **radiantd** must fully sync the blockchain first
2. **electrumx** will wait (via healthcheck) until radiantd is ready
3. **electrumx** then indexes the blockchain (can take hours/days depending on hardware)

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
