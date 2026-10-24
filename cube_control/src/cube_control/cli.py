import argparse
import json
import os
import sys
from pathlib import Path
from typing import Final

import uvicorn
from pydantic import ValidationError

from cube_control.api import create_app
from cube_control.models import ImportBatch
from cube_control.store import PostgresRepository


def main() -> int:
    parser: Final[argparse.ArgumentParser] = argparse.ArgumentParser(prog="cubellm-control")
    sub: Final = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    import_parser: Final = sub.add_parser("import")
    import_parser.add_argument("path", nargs="?", default="-")
    serve_parser: Final = sub.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=4001)
    args: Final = parser.parse_args()
    dsn: Final[str] = os.environ.get("CUBE_CONTROL_DATABASE_URL", "")
    if not dsn:
        print("CUBE_CONTROL_DATABASE_URL is not configured", file=sys.stderr)
        return 2
    repository: Final[PostgresRepository] = PostgresRepository(dsn)
    if args.command == "init":
        repository.initialize()
        print("CubeLLM Control schema ready")
        return 0
    if args.command == "import":
        raw: Final[str] = sys.stdin.read(8_000_001) if args.path == "-" else Path(args.path).read_text()
        if len(raw) > 8_000_000:
            print("Import exceeds size limit", file=sys.stderr)
            return 2
        try:
            batch: Final[ImportBatch] = ImportBatch.model_validate_json(raw)
        except ValidationError:
            print("Import rejected: invalid data; input values are not logged", file=sys.stderr)
            return 2
        result: Final = repository.import_batch(batch)
        print(json.dumps({"ok": result.ok, "inserted": result.inserted, "message": result.message}))
        return 0 if result.ok else 1
    repository.initialize()
    uvicorn.run(create_app(repository), host=args.host, port=args.port, access_log=False, log_level="warning")
    return 0
