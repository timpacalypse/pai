# PAI Network Monitoring Dashboard

Grafana + Prometheus stack for network and API usage monitoring.  
**Deployed on:** Synology NAS @ `192.168.0.5` via SSH + docker compose.

## Data Sources

| Source | Exporter | Metrics |
|--------|----------|---------|
| Pi-hole (192.168.0.192) | pihole-exporter | DNS queries, blocked ads, top domains |
| TP-Link Router | SNMP exporter | Interface traffic, errors, uptime |
| Claude API | Custom exporter | Tokens, cost, requests per model |

## Deploy to Synology

```bash
cd infra/monitoring

# First time: create .env on the NAS
cp .env.example .env
# Edit .env with your actual values, then:
scp .env tim@192.168.0.5:/volume1/docker/pai-monitoring/.env

# Deploy (syncs files + builds + starts)
./deploy.sh
# Or with a custom user: ./deploy.sh myuser@192.168.0.5
```

After deploy:
- **Grafana:** http://192.168.0.5:3001 (default: admin/admin)
- **Prometheus:** http://192.168.0.5:9090

### Remote directory layout

```
/volume1/docker/pai-monitoring/
├── docker-compose.yml
├── .env
├── prometheus/prometheus.yml
├── snmp/snmp.yml
├── grafana/provisioning/...
├── exporters/claude-usage/...
└── data/
    ├── grafana/    (persistent)
    └── prometheus/ (persistent)
```

## Setup Notes

### Pi-hole
Get your API token from: Pi-hole Admin → Settings → API → Show API token

### TP-Link Router (SNMP)
1. Log into your TP-Link router admin panel
2. Go to Advanced → System Tools → SNMP Settings (or similar)
3. Enable SNMP v2c
4. Set community string to `public` (or update `.env` and `snmp.yml`)
5. If SNMP isn't available on your model, the router panels will be empty — you can remove that section later

### Claude API
Uses your existing `ANTHROPIC_API_KEY`. The usage API endpoint availability depends on your account type (organization vs individual). The exporter handles both gracefully.

## Architecture

```
Synology NAS (192.168.0.5)
┌─────────────┐     ┌────────────┐     ┌─────────────────┐
│   Grafana   │◄────│ Prometheus │◄────│ pihole-exporter  │◄── Pi-hole (192.168.0.192)
│  :3001      │     │  :9090     │     │  :9617           │
└─────────────┘     │            │     ├─────────────────┤
                    │            │◄────│ snmp-exporter    │◄── TP-Link Router
                    │            │     │  :9116           │
                    │            │     ├─────────────────┤
                    │            │◄────│ claude-exporter  │◄── Anthropic API
                    └────────────┘     │  :9618           │
                                       └─────────────────┘
```

## Updating

After making changes locally, just re-run:
```bash
./deploy.sh
```
This rsyncs the config and rebuilds any changed images on the NAS.

## Adding More Data Sources

To add new exporters, add them to `docker-compose.yml` and a corresponding scrape job in `prometheus/prometheus.yml`.
