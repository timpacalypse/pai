"""
Claude API Usage Exporter for Prometheus.

Reads token usage from PAI's Redis store (populated by claude_service.py).
Keys: pai:claude:spend:YYYY-MM-DD, pai:claude:tokens:YYYY-MM-DD
"""
import os
import json
import time
import logging
from datetime import date, timedelta

import redis
from prometheus_client import start_http_server, Gauge

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Today's metrics
INPUT_TOKENS = Gauge('claude_usage_input_tokens', 'Input tokens used today')
OUTPUT_TOKENS = Gauge('claude_usage_output_tokens', 'Output tokens used today')
COST_USD = Gauge('claude_usage_cost_usd', 'Estimated cost in USD today')
REQUESTS_TOTAL = Gauge('claude_usage_requests_total', 'Total API requests today')

# Rolling 30-day metrics
INPUT_TOKENS_30D = Gauge('claude_usage_input_tokens_30d', 'Input tokens last 30 days')
OUTPUT_TOKENS_30D = Gauge('claude_usage_output_tokens_30d', 'Output tokens last 30 days')
COST_USD_30D = Gauge('claude_usage_cost_usd_30d', 'Estimated cost in USD last 30 days')
REQUESTS_TOTAL_30D = Gauge('claude_usage_requests_total_30d', 'Total API requests last 30 days')


def get_redis() -> redis.Redis:
    url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
    return redis.from_url(url, decode_responses=True)


def fetch_usage() -> None:
    """Read usage data from PAI's Redis and update Prometheus metrics."""
    try:
        r = get_redis()

        # Today's usage
        today = date.today().isoformat()
        spend = float(r.get(f"pai:claude:spend:{today}") or 0)
        tokens_data = r.get(f"pai:claude:tokens:{today}")
        tokens = json.loads(tokens_data) if tokens_data else {"input": 0, "output": 0, "calls": 0}

        INPUT_TOKENS.set(tokens.get("input", 0))
        OUTPUT_TOKENS.set(tokens.get("output", 0))
        COST_USD.set(round(spend, 4))
        REQUESTS_TOTAL.set(tokens.get("calls", 0))

        # 30-day rolling totals
        total_input_30d = 0
        total_output_30d = 0
        total_spend_30d = 0.0
        total_calls_30d = 0

        for i in range(30):
            day = (date.today() - timedelta(days=i)).isoformat()
            day_spend = float(r.get(f"pai:claude:spend:{day}") or 0)
            day_tokens = r.get(f"pai:claude:tokens:{day}")
            if day_tokens:
                dt = json.loads(day_tokens)
                total_input_30d += dt.get("input", 0)
                total_output_30d += dt.get("output", 0)
                total_calls_30d += dt.get("calls", 0)
            total_spend_30d += day_spend

        INPUT_TOKENS_30D.set(total_input_30d)
        OUTPUT_TOKENS_30D.set(total_output_30d)
        COST_USD_30D.set(round(total_spend_30d, 4))
        REQUESTS_TOTAL_30D.set(total_calls_30d)

        logger.info(
            f"Today: {tokens.get('input', 0)} in / {tokens.get('output', 0)} out, "
            f"${spend:.4f} | 30d: ${total_spend_30d:.2f}"
        )

    except Exception as e:
        logger.error(f"Failed to fetch usage data: {e}")


def main():
    port = int(os.environ.get('EXPORTER_PORT', '9618'))
    interval = int(os.environ.get('SCRAPE_INTERVAL', '60'))

    logger.info(f"Starting Claude usage exporter on port {port}, refresh every {interval}s")
    start_http_server(port)

    # Initial fetch
    fetch_usage()

    while True:
        time.sleep(interval)
        fetch_usage()


if __name__ == '__main__':
    main()
