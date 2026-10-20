"""Export version-matched API contracts without starting a PAS listener."""
from __future__ import annotations

import argparse
import json


def application_schema() -> dict:
    from server.api.router import build_knowledge_router
    from server.app import create_app

    app = create_app()
    # Export the complete contract, including optional knowledge routes. This
    # does not start lifespan, connect to metadata, or enable deployed features.
    app.router.routes[0:0] = build_knowledge_router().routes
    return app.openapi()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("application", "agent"), default="application")
    args = parser.parse_args()
    if args.surface == "agent":
        from server.mcp.agent_openapi import agent_openapi_schema

        schema = agent_openapi_schema()
    else:
        schema = application_schema()
    print(json.dumps(schema, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
