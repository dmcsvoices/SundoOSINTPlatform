# Sundo Pi OSINT Platform

Autonomous OSINT monitoring and amplification platform running on Raspberry Pi 5.
Detects coordinated paid influence operations and amplifies Palestinian voices.

## Quick Start

```bash
cd /home/darren/sundo-pi
source activate.sh          # Sets PYTHONPATH and activates venv
python -m sundo.main          # Run the scheduler daemon
```

## Network Access (Tailscale)

All services bind to `0.0.0.0` for Tailscale mesh access:

| Service | Port | Access |
|---------|------|--------|
| Flask Dashboard | 15000 | `http://<pi-tailscale-ip>:15000` |
| Neo4j Bolt | 17687 | `bolt://<pi-tailscale-ip>:17687` |
| Redis | 16379 | `<pi-tailscale-ip>:16379` |

## Setup

1. **Install Neo4j** (community edition):
   ```bash
   wget -O - https://debian.neo4j.com/neotechnology.gpg.key | sudo apt-key add -
   echo 'deb https://debian.neo4j.com stable 5' | sudo tee /etc/apt/sources.list.d/neo4j.list
   sudo apt update && sudo apt install neo4j
   # Edit /etc/neo4j/neo4j.conf:
   #   server.bolt.listen_address=0.0.0.0:17687
   #   server.default_listen_address=0.0.0.0
   sudo systemctl enable neo4j
   sudo systemctl start neo4j
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Install spaCy model:**
   ```bash
   source activate.sh
   python -m spacy download en_core_web_sm
   ```

4. **Enable systemd autostart:**
   ```bash
   sudo cp systemd/sundo.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable sundo
   sudo systemctl start sundo
   ```

## Dashboard

Interactive Cytoscape.js network graph showing:
- 🔴 FARA-linked persons (red)
- 🔵 Organizations (blue)
- 🟢 Palestinian voices (green)
- Hover for credibility scores and funding links

Open `http://<pi-tailscale-ip>:15000` after starting the service.

## Project Structure

```
sundo-pi/
├── sundo/
│   ├── config.py              # Environment-based configuration
│   ├── main.py                # APScheduler entry point
│   ├── db/
│   │   ├── sqlite_store.py    # Raw ingestion store
│   │   ├── neo4j_client.py    # Graph DB interface
│   │   └── schema.py          # Neo4j constraint setup
│   ├── ingest/
│   │   ├── fara_scraper.py    # DOJ FARA scraper
│   │   ├── irs990_monitor.py  # ProPublica API
│   │   ├── social_monitor.py  # X/Twitter + TikTok
│   │   └── rss_aggregator.py  # Palestinian media feeds
│   ├── detect/
│   │   ├── timing_analysis.py # Burst detection
│   │   ├── similarity.py      # MinHash LSH dedup
│   │   ├── network_graph.py   # Graph enrichment
│   │   └── disclosure_audit.py# FTC #ad checker
│   ├── report/
│   │   ├── report_generator.py# Nightly markdown reports
│   │   ├── alert_engine.py    # ntfy.sh alerts
│   │   └── cytoscape_export.py# Graph JSON export
│   ├── amplify/
│   │   ├── voice_registry.py  # Palestinian voice scoring
│   │   ├── briefing_gen.py    # Counter-narrative briefs
│   │   ├── ftc_packager.py   # FTC complaint pre-fill
│   │   └── digest.py          # Daily email digest
│   └── dashboard/
│       ├── app.py             # Flask app (0.0.0.0:15000)
│       └── templates/
│           └── index.html     # Cytoscape.js network view
├── venv/                      # Python virtual environment
├── activate.sh                # Quick venv activation helper
├── requirements.txt
├── .env.example
├── systemd/sundo.service
└── README.md
```

## Schedules

| Job | Frequency |
|-----|-----------|
| FARA scraper | Weekly, Sunday 02:00 |
| IRS 990 monitor | Monthly, 1st 03:00 |
| RSS aggregator | Every 2 hours |
| Social monitor | Every 4 hours |
| Timing analysis | Every 6 hours |
| Similarity detection | Every 6 hours |
| Network graph | Nightly |
| Disclosure audit | Nightly |
| Report generator | Nightly |
| Cytoscape export | Nightly |
| Daily digest | Daily 07:00 |

## License

Built for the democratization of technology and access for all.
