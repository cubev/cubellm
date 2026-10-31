import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import psycopg
import uvicorn
from pydantic import ValidationError

from cube_control.api import create_app
from cube_control.edge import GatewayEdgeSpec, render_gateway_edge
from cube_control.gateway import GatewayProjectionBatch, prepare_gateway
from cube_control.gateway_store import GatewayRepository
from cube_control.heygen import collect_heygen
from cube_control.media import JobCommand, PostgresJobManager
from cube_control.models import GatewayBatch, ImportBatch, ManualCommand
from cube_control.openrouter import collect_openrouter
from cube_control.store import PostgresRepository
from cube_control.usage import PostgresUsageReader


class CommandArguments(argparse.Namespace):
    command: str
    path: str
    actor: str
    config: Path
    account_id: str
    job_id: str
    after: int
    limit: int
    host: str
    port: int
    account_name: str


def main() -> int:
    parser: Final[argparse.ArgumentParser] = argparse.ArgumentParser(prog="cubellm-control")
    sub: Final = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("import-gateway")
    sub.add_parser("project-gateway")
    sub.add_parser("prepare-edge")
    manage_parser: Final = sub.add_parser("manage")
    manage_parser.add_argument("path", nargs="?", default="-")
    manage_parser.add_argument("--actor", default=getpass.getuser())
    job_parser: Final = sub.add_parser("manage-job")
    job_parser.add_argument("path", nargs="?", default="-")
    job_parser.add_argument("--actor", default=getpass.getuser())
    history_parser: Final = sub.add_parser("job-history")
    history_parser.add_argument("account_id")
    history_parser.add_argument("job_id")
    history_parser.add_argument("--after", type=int, default=0)
    history_parser.add_argument("--limit", type=int, default=50)
    collect_parser: Final = sub.add_parser("collect-openrouter")
    collect_parser.add_argument("--config", required=True, type=Path)
    collect_parser.add_argument("--account-id", default="openrouter-api")
    collect_parser.add_argument("--account-name", default="OpenRouter API")
    heygen_parser: Final = sub.add_parser("collect-heygen")
    heygen_parser.add_argument("--config", required=True, type=Path)
    heygen_parser.add_argument("--account-id", default="heygen-api")
    heygen_parser.add_argument("--account-name", default="HeyGen API")
    import_parser: Final = sub.add_parser("import")
    import_parser.add_argument("path", nargs="?", default="-")
    serve_parser: Final = sub.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=4001)
    args: Final = CommandArguments()
    parser.parse_args(namespace=args)
    if args.command == "prepare-edge":
        raw_edge: Final = sys.stdin.read(65537)
        if len(raw_edge) > 65536:
            print("Gateway edge specification exceeds size limit", file=sys.stderr)
            return 2
        try:
            edge: Final = GatewayEdgeSpec.model_validate_json(raw_edge)
        except ValidationError:
            print("Gateway edge specification rejected; input values are not logged", file=sys.stderr)
            return 2
        print(render_gateway_edge(edge).model_dump_json())
        return 0
    if args.command == "collect-openrouter":
        try:
            batch: Final = collect_openrouter(
                args.config, datetime.now(UTC), account_id=args.account_id, account_name=args.account_name
            )
        except ValidationError:
            print("Provider account rejected; input values are not logged", file=sys.stderr)
            return 2
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
    if args.command == "collect-heygen":
        try:
            heygen_batch: Final = collect_heygen(
                args.config, datetime.now(UTC), account_id=args.account_id, account_name=args.account_name
            )
        except ValidationError:
            print("Provider account rejected; input values are not logged", file=sys.stderr)
            return 2
        print(heygen_batch.model_dump_json())
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
    if args.command == "manage-job":
        with sys.stdin if args.path == "-" else Path(args.path).open() as job_input:
            raw_job: Final = job_input.read(65537)
        if len(raw_job) > 65536:
            print("Job command exceeds size limit", file=sys.stderr)
            return 2
        try:
            job_command: Final = JobCommand.model_validate_json(raw_job)
        except ValidationError:
            print("Job command rejected; input values are not logged", file=sys.stderr)
            return 2
        job_result: Final = PostgresJobManager(dsn).manage(job_command, args.actor, datetime.now(UTC))
        print(json.dumps({"status": job_result.status, "version": job_result.version, "message": job_result.message}))
        return 0 if job_result.status in ("applied", "duplicate") else 1
    if args.command == "job-history":
        try:
            history: Final = PostgresJobManager(dsn).history(args.account_id, args.job_id, args.after, args.limit)
        except ValueError:
            print("Invalid job history query", file=sys.stderr)
            return 2
        except psycopg.Error:
            print("Job history unavailable", file=sys.stderr)
            return 1
        print(history.model_dump_json())
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
        create_app(repository, usage_reader=PostgresUsageReader(dsn), job_reader=PostgresJobManager(dsn)),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )
    return 0
