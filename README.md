# alibabacloud polardb tool agentic server

Open-source MCP gateway for PolarDB MySQL instances. Provides OAuth authentication, employee/department management, instance routing, and SQL proxy execution for AI Agents.

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- Node.js 20+

### Backend

```bash
uv sync
uv run alembic upgrade head
uv run python -m server
```

Server starts on `http://localhost:18760`.

### Frontend (Development)

```bash
cd web
npm install
npm run dev
```

Dev server starts on `http://localhost:18761` with API proxy to backend.

### Docker

```bash
docker-compose up
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit as needed. All settings support environment variable override with `PAS_` prefix.

Required for first run (builtin auth):
```bash
export PAS_ADMIN_INITIAL_PASSWORD=your-password
```

## License

Apache 2.0
