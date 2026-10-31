import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import uvicorn
from pydantic import ValidationError

from cube_control.api import create_app
from cube_control.gateway import GatewayProjectionBatch, prepare_gateway
from cube_control.gateway_store import GatewayRepository
from cube_control.models import GatewayBatch, ImportBatch, ManualCommand
from cube_control.openrouter import collect_openrouter
from cube_control.store import PostgresRepository
from cube_control.usage import PostgresUsageReader


def main() -> int:
    parser: Final[argparse.ArgumentParser] = argparse.ArgumentParser(prog="cubellm-control")
    sub: Final = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("import-gateway")
    sub.add_parser("project-gateway")
    manage_parser: Final = sub.add_parser("manage")
    manage_parser.add_argument("path", nargs="?", default="-")
    manage_parser.add_argument("--actor", default=getpass.getuser())
    collect_parser: Final = sub.add_parser("collect-openrouter")
    collect_parser.add_argument("--config", required=True, type=Path)
    import_parser: Final = sub.add_parser("import")
    import_parser.add_argument("path", nargs="?", default="-")
    serve_parser: Final = sub.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=4001)
    args: Final = parser.parse_args()
    if args.command == "collect-openrouter":
        batch: Final = collect_openrouter(args.config, datetime.now(UTC))
        print(batch.model_dump_json())
        return 0
    if args.command == "project-gateway":
        raw_projection: Final = sys.stdin.read(8_000_001)
        if len(raw_projection) > 8_000_000:
            print("Gateway projection exceeds size limit", file=sys.stderr)
            return 2
        try:
            projection: Final = GatewayProjectionBatch.model_validate_json(raw_projection)
        except ValidationError:
            print("Gateway projection rejected; source values are not logged", file=sys.stderr)
            return 2
        print(prepare_gateway(projection).model_dump_json())
        return 0
    dsn: Final[str] = os.environ.get("CUBE_CONTROL_DATABASE_URL", "")
    if not dsn:
        print("CUBE_CONTROL_DATABASE_URL is not configured", file=sys.stderr)
        return 2
    repository: Final[PostgresRepository] = PostgresRepository(dsn)
    if args.command == "init":
        repository.initialize()
        print("CubeLLM Control schema ready")
        return 0
    if args.command == "import-gateway":
        raw_gateway: Final = sys.stdin.read(8_000_001)
        if len(raw_gateway) > 8_000_000:
            print("Gateway batch exceeds size limit", file=sys.stderr)
            return 2
        try:
            gateway_batch: Final = GatewayBatch.model_validate_json(raw_gateway)
        except ValidationError:
            print("Gateway import rejected; source data is not logged", file=sys.stderr)
            return 2
        imported: Final = GatewayRepository(dsn).import_batch(gateway_batch)
        print(
            json.dumps(
                {
                    "status": imported.status,
                    "sequence": imported.sequence,
                    "changed": imported.changed,
                    "message": imported.message,
                }
            )
        )
        return 0 if imported.status in ("applied", "duplicate") else 1
    if args.command == "manage":
        raw_command: Final[str] = sys.stdin.read(65537) if args.path == "-" else Path(args.path).read_text()
        if len(raw_command) > 65536:
            print("Management command exceeds size limit", file=sys.stderr)
            return 2
        try:
            command: Final = ManualCommand.model_validate_json(raw_command)
        except ValidationError:
            print("Management command rejected: invalid data; input values are not logged", file=sys.stderr)
            return 2
        managed: Final = repository.manage(command, args.actor, datetime.now(UTC))
        print(json.dumps({"status": managed.status, "version": managed.version, "message": managed.message}))
        return 0 if managed.status in ("applied", "duplicate") else 1
    if args.command == "import":
        raw: Final[str] = sys.stdin.read(8_000_001) if args.path == "-" else Path(args.path).read_text()
        if len(raw) > 8_000_000:
            print("Import exceeds size limit", file=sys.stderr)
            return 2
        try:
            import_batch: Final[ImportBatch] = ImportBatch.model_validate_json(raw)
        except ValidationError:
            print("Import rejected: invalid data; input values are not logged", file=sys.stderr)
            return 2
        result: Final = repository.import_batch(import_batch)
        print(json.dumps({"ok": result.ok, "inserted": result.inserted, "message": result.message}))
        return 0 if result.ok else 1
    repository.initialize()
    uvicorn.run(
        create_app(repository, usage_reader=PostgresUsageReader(dsn)),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )
    return 0
