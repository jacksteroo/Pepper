#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

source "$REPO_ROOT/.venv/bin/activate"

python - <<'EOF'
import asyncio
import sys
sys.path.insert(0, ".")

from agent.config import settings
from agent.db import init_db, get_db
from agent.memory import MemoryManager
from sqlalchemy.ext.asyncio import async_sessionmaker
from agent.db import _engine

async def main():
    await init_db(settings)

    from agent.db import _session_factory
    manager = MemoryManager(db_session_factory=_session_factory)
    result = await manager.reset_all()
    if result.get("ok"):
        print(f"✓ {result['message']}")
    else:
        print(f"✗ {result.get('error')}", file=sys.stderr)
        sys.exit(1)

asyncio.run(main())
EOF
