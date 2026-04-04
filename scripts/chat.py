#!/usr/bin/env python3
"""
PAI Chat CLI — Interactive terminal client for the PAI orchestrator.

Usage:
    python scripts/chat.py                       # default role
    python scripts/chat.py --role parent          # specific role
    python scripts/chat.py --role cybersecurity_executive --secondary ai_cybersecurity_strategist
"""

import argparse
import sys
import uuid

import httpx

BASE_URL = "http://localhost:8000"

ROLES = [
    "cybersecurity_executive", "ai_cybersecurity_strategist",
    "ai_governance_practitioner", "educator_scholar",
    "solutions_architect", "proposal_strategist",
    "fitness_longevity_optimist", "aesthetics_focused_builder",
    "family_chef", "family_activity_coordinator",
    "parent", "polymath_in_training",
]


def print_banner():
    print("\n" + "=" * 60)
    print("  PAI — Personal AI Orchestrator")
    print("  Roles & agents auto-inferred from your prompt")
    print("  Type /help for commands, /quit to exit")
    print("=" * 60 + "\n")


def print_help():
    print("""
Commands:
  /role <name>        Switch primary role
  /secondary <name>   Set secondary role (or 'none' to clear)
  /roles              List all available roles
  /research <topic>   Run web research on a topic
  /clear              Clear conversation history
  /status             Show current session info
  /help               Show this help
  /quit               Exit
""")


def show_roles(client: httpx.Client):
    resp = client.get("/roles")
    if resp.status_code == 200:
        data = resp.json()
        for domain, roles in data["domains"].items():
            print(f"\n  [{domain.upper()}]")
            for r in roles:
                print(f"    - {r['role']}: {r['description'][:80]}")
    print()


def do_research(client: httpx.Client, topic: str):
    print(f"\n  Searching for: {topic}")
    print("  This may take a moment...\n")
    resp = client.post("/skills/web-research", json={
        "topic": topic,
        "max_results": 10,
        "time_filter": "m",
        "auto_ingest": True,
    }, timeout=120.0)
    if resp.status_code == 200:
        data = resp.json()
        print(f"  Found {data['total_found']} articles, ingested {data['ingested_count']} to memory")
        print(f"  Time: {data['duration_ms']:.0f}ms\n")
        for i, article in enumerate(data["articles"], 1):
            score = article.get("score", {}).get("total", 0)
            print(f"  {i}. [{score:.2f}] {article['title']}")
            print(f"     {article['url']}")
            if article.get("snippet"):
                print(f"     {article['snippet'][:120]}...")
            print()
    else:
        print(f"  Error: {resp.status_code} — {resp.text}\n")


def main():
    parser = argparse.ArgumentParser(description="PAI Chat CLI")
    parser.add_argument("--role", default=None, choices=ROLES, help="Primary role")
    parser.add_argument("--secondary", default=None, choices=ROLES, help="Secondary role")
    parser.add_argument("--base-url", default=BASE_URL, help="Orchestrator URL")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=120.0)

    # Verify connectivity
    try:
        health = client.get("/health")
        if health.status_code != 200:
            print("Warning: Orchestrator health check failed")
    except httpx.ConnectError:
        print(f"Error: Cannot connect to orchestrator at {args.base_url}")
        print("Make sure the PAI stack is running: docker compose up")
        sys.exit(1)

    primary_role = args.role
    secondary_role = args.secondary
    conversation_id = str(uuid.uuid4())
    history: list[dict] = []

    print_banner()
    if primary_role:
        print(f"  Role: {primary_role}")
    if secondary_role:
        print(f"  Secondary: {secondary_role}")
    print()

    while True:
        try:
            user_input = input("You > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        # Handle commands
        if user_input.startswith("/"):
            cmd_parts = user_input.split(maxsplit=1)
            cmd = cmd_parts[0].lower()
            cmd_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

            if cmd == "/quit":
                print("Goodbye!")
                break
            elif cmd == "/help":
                print_help()
            elif cmd == "/roles":
                show_roles(client)
            elif cmd == "/role":
                if cmd_arg in ROLES:
                    primary_role = cmd_arg
                    print(f"  Role set to: {primary_role}\n")
                else:
                    print(f"  Unknown role. Use /roles to see options.\n")
            elif cmd == "/secondary":
                if cmd_arg == "none":
                    secondary_role = None
                    print("  Secondary role cleared.\n")
                elif cmd_arg in ROLES:
                    secondary_role = cmd_arg
                    print(f"  Secondary role set to: {secondary_role}\n")
                else:
                    print(f"  Unknown role. Use /roles to see options.\n")
            elif cmd == "/research":
                if cmd_arg:
                    do_research(client, cmd_arg)
                else:
                    print("  Usage: /research <topic>\n")
            elif cmd == "/clear":
                history.clear()
                conversation_id = str(uuid.uuid4())
                print("  Conversation cleared.\n")
            elif cmd == "/status":
                print(f"  Role: {primary_role or 'default (cybersecurity_executive)'}")
                print(f"  Secondary: {secondary_role or 'none'}")
                print(f"  Conversation: {conversation_id[:8]}...")
                print(f"  History: {len(history)} messages\n")
            else:
                print(f"  Unknown command: {cmd}. Type /help for options.\n")
            continue

        # Send chat message
        payload = {
            "message": user_input,
            "conversation_id": conversation_id,
            "history": history[-20:],  # send last 20 messages
        }
        if primary_role:
            payload["role"] = primary_role
        if secondary_role:
            payload["secondary_role"] = secondary_role

        try:
            resp = client.post("/chat", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                content = data["content"]
                role_label = data["role"]
                duration = data.get("duration_ms", 0)

                print(f"\n[{role_label}] ({duration:.0f}ms)")
                print(content)
                print()

                # Update history
                history.append({"role_name": "user", "content": user_input})
                history.append({"role_name": "assistant", "content": content})
            else:
                print(f"\n  Error: {resp.status_code} — {resp.text}\n")
        except httpx.ReadTimeout:
            print("\n  Response timed out. The model may be processing a complex request.\n")
        except Exception as e:
            print(f"\n  Error: {e}\n")


if __name__ == "__main__":
    main()
