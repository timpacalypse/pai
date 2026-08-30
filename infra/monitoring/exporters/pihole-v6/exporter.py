"""
Pi-hole v6 Exporter for Prometheus.

Exposes per-client blocked queries with device labels (comment > name > vendor > MAC),
top blocked domains, blocklist stats, and forward destination breakdown.
Queries the Pi-hole v6 REST API directly.
"""
import os
import time
import logging
import urllib.request
import json

from prometheus_client import start_http_server, Gauge, Info

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pihole-v6-exporter")

PIHOLE_URL = os.environ.get("PIHOLE_URL", "http://192.168.0.192")
PIHOLE_PASSWORD = os.environ.get("PIHOLE_PASSWORD", "")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9620"))
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "60"))

# ── Prometheus Metrics ────────────────────────────────────────────────────────

# Per-client metrics
CLIENT_TOTAL = Gauge(
    "pihole_client_queries_total",
    "Total queries by client",
    ["ip", "device", "mac"],
)
CLIENT_BLOCKED = Gauge(
    "pihole_client_blocked_total",
    "Blocked queries by client",
    ["ip", "device", "mac"],
)
CLIENT_BLOCK_PCT = Gauge(
    "pihole_client_blocked_percent",
    "Block percentage per client",
    ["ip", "device", "mac"],
)

# Top blocked domains
TOP_BLOCKED = Gauge(
    "pihole_top_blocked_domain",
    "Top blocked domain hit count",
    ["domain"],
)

# Blocklist stats
LIST_SIZE = Gauge(
    "pihole_list_domains",
    "Number of domains in blocklist",
    ["list_name", "list_url"],
)
LIST_ENABLED = Gauge(
    "pihole_list_enabled",
    "Whether blocklist is enabled (1=yes, 0=no)",
    ["list_name", "list_url"],
)

# Summary stats
TOTAL_QUERIES = Gauge("pihole_v6_queries_total", "Total DNS queries today")
TOTAL_BLOCKED = Gauge("pihole_v6_blocked_total", "Total blocked queries today")
BLOCK_PERCENT = Gauge("pihole_v6_blocked_percent", "Overall block percentage")
LISTS_COUNT = Gauge("pihole_v6_active_lists", "Number of enabled blocklists")
DOMAINS_BLOCKED = Gauge("pihole_v6_domains_on_blocklist", "Total domains across all blocklists")
UNIQUE_CLIENTS = Gauge("pihole_v6_unique_clients", "Unique clients seen today")

# Forward destinations
FORWARD_DEST = Gauge(
    "pihole_v6_forward_destination",
    "Queries by forward destination",
    ["destination", "name"],
)

# ── API Client ────────────────────────────────────────────────────────────────

_session_id = None
_session_expires = 0


def _authenticate() -> str:
    """Get a session ID from the Pi-hole v6 API."""
    global _session_id, _session_expires
    if _session_id and time.time() < _session_expires:
        return _session_id

    body = json.dumps({"password": PIHOLE_PASSWORD}).encode()
    req = urllib.request.Request(
        f"{PIHOLE_URL}/api/auth",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    session = data.get("session", {})
    _session_id = session.get("sid", "")
    validity = session.get("validity", 300)
    _session_expires = time.time() + validity - 30  # renew 30s early
    logger.info(f"Authenticated with Pi-hole, session valid {validity}s")
    return _session_id


def _api_get(endpoint: str) -> dict:
    """GET from Pi-hole v6 API with auth."""
    sid = _authenticate()
    req = urllib.request.Request(
        f"{PIHOLE_URL}/api/{endpoint}",
        headers={"sid": sid},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ── Device Label Resolution ───────────────────────────────────────────────────

_ip_to_label: dict[str, tuple[str, str]] = {}  # ip -> (device_label, mac)
_label_cache_time = 0
LABEL_CACHE_TTL = 300  # refresh device labels every 5 minutes


def _refresh_labels():
    """Build IP → (label, mac) mapping from Pi-hole network/devices + clients."""
    global _ip_to_label, _label_cache_time
    if time.time() - _label_cache_time < LABEL_CACHE_TTL:
        return

    try:
        # Get client comments (user-assigned device names)
        clients = _api_get("clients")
        mac_to_comment = {}
        for c in clients.get("clients", []):
            mac = c.get("client", "").lower()
            comment = c.get("comment", "") or ""
            if mac and comment:
                mac_to_comment[mac] = comment.strip()

        # Get network devices (MAC, vendor, IPs)
        devices = _api_get("network/devices")
        new_map = {}
        for dev in devices.get("devices", []):
            mac = dev.get("hwaddr", "").lower()
            vendor = dev.get("macVendor", "") or ""
            for ipinfo in dev.get("ips", []):
                ip = ipinfo.get("ip", "")
                hostname = ipinfo.get("name", "") or ""
                # Priority: client comment > hostname > vendor > MAC
                label = mac_to_comment.get(mac, "") or hostname or vendor or mac
                new_map[ip] = (label, mac)

        _ip_to_label = new_map
        _label_cache_time = time.time()
        logger.info(f"Refreshed device labels: {len(new_map)} IPs mapped")

    except Exception as e:
        logger.error(f"Failed to refresh device labels: {e}")


def _get_label(ip: str) -> tuple[str, str]:
    """Get (device_label, mac) for an IP."""
    _refresh_labels()
    return _ip_to_label.get(ip, (ip, ""))


# ── Metric Collection ─────────────────────────────────────────────────────────

def collect():
    """Fetch Pi-hole v6 data and update Prometheus metrics."""
    try:
        _refresh_labels()

        # Top clients (all queries)
        all_clients = _api_get("stats/top_clients?count=30")
        total_queries = all_clients.get("total_queries", 0)
        blocked_queries = all_clients.get("blocked_queries", 0)
        client_totals = {c["ip"]: c["count"] for c in all_clients.get("clients", [])}

        # Top clients (blocked only)
        blocked_clients = _api_get("stats/top_clients?blocked=true&count=30")

        # Clear old per-client metrics
        CLIENT_TOTAL._metrics.clear()
        CLIENT_BLOCKED._metrics.clear()
        CLIENT_BLOCK_PCT._metrics.clear()

        # Per-client blocked
        for client in blocked_clients.get("clients", []):
            ip = client["ip"]
            blocked = client["count"]
            label, mac = _get_label(ip)
            total = client_totals.get(ip, blocked)

            CLIENT_TOTAL.labels(ip=ip, device=label, mac=mac).set(total)
            CLIENT_BLOCKED.labels(ip=ip, device=label, mac=mac).set(blocked)
            pct = (blocked / total * 100) if total > 0 else 0
            CLIENT_BLOCK_PCT.labels(ip=ip, device=label, mac=mac).set(round(pct, 1))

        # Also add clients with queries but no blocks
        for client in all_clients.get("clients", []):
            ip = client["ip"]
            if ip not in {c["ip"] for c in blocked_clients.get("clients", [])}:
                label, mac = _get_label(ip)
                CLIENT_TOTAL.labels(ip=ip, device=label, mac=mac).set(client["count"])
                CLIENT_BLOCKED.labels(ip=ip, device=label, mac=mac).set(0)
                CLIENT_BLOCK_PCT.labels(ip=ip, device=label, mac=mac).set(0)

        # Top blocked domains
        TOP_BLOCKED._metrics.clear()
        top_domains = _api_get("stats/top_domains?blocked=true&count=20")
        for dom in top_domains.get("domains", []):
            TOP_BLOCKED.labels(domain=dom["domain"]).set(dom["count"])

        # Blocklists
        LIST_SIZE._metrics.clear()
        LIST_ENABLED._metrics.clear()
        lists = _api_get("lists")
        active_lists = 0
        total_list_domains = 0
        for lst in lists.get("lists", []):
            url = lst.get("address", "")
            # Derive short name from URL
            name = lst.get("comment") or url.split("/")[-1].split("?")[0] or url[:50]
            count = lst.get("number", 0)
            enabled = lst.get("enabled", False)
            LIST_SIZE.labels(list_name=name, list_url=url).set(count)
            LIST_ENABLED.labels(list_name=name, list_url=url).set(1 if enabled else 0)
            if enabled:
                active_lists += 1
                total_list_domains += count

        # Summary stats
        TOTAL_QUERIES.set(total_queries)
        TOTAL_BLOCKED.set(blocked_queries)
        BLOCK_PERCENT.set(round(blocked_queries / total_queries * 100, 1) if total_queries else 0)
        LISTS_COUNT.set(active_lists)
        DOMAINS_BLOCKED.set(total_list_domains)

        # Forward destinations
        FORWARD_DEST._metrics.clear()
        try:
            fwd = _api_get("stats/upstreams")
            for upstream in fwd.get("upstreams", []):
                dest_ip = upstream.get("ip", "")
                dest_name = upstream.get("name", "") or dest_ip
                count = upstream.get("count", 0)
                FORWARD_DEST.labels(destination=dest_ip, name=dest_name).set(count)
        except Exception:
            pass  # endpoint may differ

        # Unique clients
        try:
            summary = _api_get("stats/summary")
            UNIQUE_CLIENTS.set(summary.get("clients", {}).get("active", 0))
        except Exception:
            pass

        logger.info(
            f"Collected: {total_queries} queries, {blocked_queries} blocked "
            f"({blocked_queries/total_queries*100:.1f}%), {active_lists} lists"
        )

    except Exception as e:
        logger.error(f"Collection failed: {e}")


def main():
    logger.info(f"Starting Pi-hole v6 exporter on port {EXPORTER_PORT}, interval {SCRAPE_INTERVAL}s")
    start_http_server(EXPORTER_PORT)

    collect()  # initial

    while True:
        time.sleep(SCRAPE_INTERVAL)
        collect()


if __name__ == "__main__":
    main()
