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
from pydantic import TypeAdapter, ValidationError

from cube_control.account_routes import (
    AccountResourceAdoptionSpec,
    AccountRouteSpec,
    adopt_account_resource,
    ensure_account_route,
)
from cube_control.api import create_app
from cube_control.browser_session import private_authority
from cube_control.application_access import (
    ApplicationAccessSpec,
    deliver_application_access,
    prepare_application_access,
    provision_application_access,
    revoke_application_access,
)
from cube_control.credential_binding import CredentialBindingCommand
from cube_control.credential_inventory import (
    MAX_SOURCE_BYTES,
    CredentialInventorySnapshot,
    ImportStatus,
    parse_inventory,
)
from cube_control.edge import GatewayEdgeSpec, render_gateway_edge
from cube_control.gateway import GatewayProjectionBatch, prepare_gateway
from cube_control.gateway_inventory import collect_and_record_gateway_inventory
from cube_control.gateway_store import GatewayRepository
from cube_control.github import collect_github
from cube_control.heygen import collect_heygen
from cube_control.media import JobCommand, PostgresJobManager
from cube_control.models import GatewayBatch, Identifier, ImportBatch, ManualCommand
from cube_control.native_gateway import PROJECTION_COLUMNS, prepare_native_gateway
from cube_control.openrouter import collect_openrouter
from cube_control.provider_access import Unavailable, read_private_key
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
    state_directory: Path
    destination_directory: Path
    gateway_origin: str
    source_id: str
    register: Path
    plan: Path


SOURCE_IDENTIFIER: Final = TypeAdapter[str](Identifier)


COMMAND_LIMIT: Final = 65536


def bounded_command(path: str) -> str | None:
    try:
        if path == "-":
            return sys.stdin.read(COMMAND_LIMIT + 1)
        with Path(path).open("rb") as source:
            return source.read(COMMAND_LIMIT + 1).decode()
    except (OSError, UnicodeError):
        return None


def credential_sources(register: Path, plan: Path) -> tuple[bytes, bytes] | None:
    try:
        with register.open("rb") as register_file, plan.open("rb") as plan_file:
            return register_file.read(MAX_SOURCE_BYTES + 1), plan_file.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        return None


def main() -> int:
    parser: Final[argparse.ArgumentParser] = argparse.ArgumentParser(prog="cubellm-control")
    sub: Final = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("import-gateway")
    sub.add_parser("project-gateway")
    sub.add_parser("project-native-gateway")
    sub.add_parser("native-gateway-projection")
    sub.add_parser("prepare-edge")
    prepare_access_parser: Final = sub.add_parser("prepare-application")
    prepare_access_parser.add_argument("--state-directory", required=True, type=Path)
    provision_access_parser: Final = sub.add_parser("provision-application")
    provision_access_parser.add_argument("--state-directory", required=True, type=Path)
    provision_access_parser.add_argument("--config", required=True, type=Path)
    revoke_access_parser: Final = sub.add_parser("revoke-application")
    revoke_access_parser.add_argument("--state-directory", required=True, type=Path)
    revoke_access_parser.add_argument("--config", required=True, type=Path)
    route_parser: Final = sub.add_parser("provision-account-route")
    route_parser.add_argument("--config", required=True, type=Path)
    adopt_parser: Final = sub.add_parser("adopt-account-resource")
    adopt_parser.add_argument("--config", required=True, type=Path)
    deliver_access_parser: Final = sub.add_parser("deliver-application")
    deliver_access_parser.add_argument("--state-directory", required=True, type=Path)
    deliver_access_parser.add_argument("--destination-directory", required=True, type=Path)
    deliver_access_parser.add_argument("--config", required=True, type=Path)
    manage_parser: Final = sub.add_parser("manage")
    manage_parser.add_argument("path", nargs="?", default="-")
    manage_parser.add_argument("--actor", default=getpass.getuser())
    binding_parser: Final = sub.add_parser("bind-credential")
    binding_parser.add_argument("path", nargs="?", default="-")
    binding_parser.add_argument("--actor", default=getpass.getuser())
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
    github_parser: Final = sub.add_parser("collect-github")
    github_parser.add_argument("--config", required=True, type=Path)
    github_parser.add_argument("--account-id", default="github-api")
    github_parser.add_argument("--account-name", default="GitHub API")
    inventory_parser: Final = sub.add_parser("collect-gateway-inventory")
    inventory_parser.add_argument("--config", required=True, type=Path)
    inventory_parser.add_argument("--gateway-origin", required=True)
    credential_parser: Final = sub.add_parser("import-credential-inventory")
    credential_parser.add_argument("--source-id", required=True)
    credential_parser.add_argument("--register", required=True, type=Path)
    credential_parser.add_argument("--plan", required=True, type=Path)
    import_parser: Final = sub.add_parser("import")
    import_parser.add_argument("path", nargs="?", default="-")
    serve_parser: Final = sub.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=4001)
    args: Final = CommandArguments()
    parser.parse_args(namespace=args)
    if args.command == "prepare-application":
        raw_application: Final = sys.stdin.read(65537)
        try:
            if len(raw_application) > 65536:
                raise ValueError("Application specification exceeds size limit")
            application_spec: Final = ApplicationAccessSpec.model_validate_json(raw_application)
        except ValueError:
            print("Application specification rejected; input values are not logged", file=sys.stderr)
            return 2
        application_prepared: Final = prepare_application_access(application_spec, args.state_directory)
        print(application_prepared.model_dump_json())
        return 0 if application_prepared.status == "prepared" else 1
    if args.command == "provision-account-route":
        raw_route: Final = sys.stdin.read(65537)
        try:
            if len(raw_route) > 65536:
                raise ValueError("Account route specification exceeds size limit")
            route_spec: Final = AccountRouteSpec.model_validate_json(raw_route)
        except ValueError:
            print("Account route specification rejected; input values are not logged", file=sys.stderr)
            return 2
        route_key: Final = read_private_key(args.config, "LITELLM_MASTER_KEY", "Gateway")
        if isinstance(route_key, Unavailable):
            print(route_key.message, file=sys.stderr)
            return 2
        route_result: Final = ensure_account_route(route_spec, route_key)
        print(route_result.model_dump_json())
        return 0 if route_result.status in ("created", "existing") else 1
    if args.command == "adopt-account-resource":
        raw_adoption: Final = sys.stdin.read(65537)
        try:
            if len(raw_adoption) > 65536:
                raise ValueError("Resource adoption specification exceeds size limit")
            adoption_spec: Final = AccountResourceAdoptionSpec.model_validate_json(raw_adoption)
        except ValueError:
            print("Resource adoption specification rejected; input values are not logged", file=sys.stderr)
            return 2
        adoption_key: Final = read_private_key(args.config, "LITELLM_MASTER_KEY", "Gateway")
        if isinstance(adoption_key, Unavailable):
            print(adoption_key.message, file=sys.stderr)
            return 2
        adoption_result: Final = adopt_account_resource(adoption_spec, adoption_key)
        print(adoption_result.model_dump_json())
        return 0 if adoption_result.status in ("adopted", "existing") else 1
    if args.command in ("provision-application", "deliver-application", "revoke-application"):
        master_key: Final = read_private_key(args.config, "LITELLM_MASTER_KEY", "Gateway")
        if isinstance(master_key, Unavailable):
            print(master_key.message, file=sys.stderr)
            return 2
        if args.command == "revoke-application":
            application_revoked: Final = revoke_application_access(args.state_directory, master_key)
            print(application_revoked.model_dump_json())
            return 0 if application_revoked.status in ("blocked", "deleted") else 1
        if args.command == "deliver-application":
            application_delivered: Final = deliver_application_access(
                args.state_directory,
                args.destination_directory,
                master_key,
            )
            print(application_delivered.model_dump_json())
            return 0 if application_delivered.status == "delivered" else 1
        application_provisioned: Final = provision_application_access(args.state_directory, master_key)
        print(application_provisioned.model_dump_json())
        return 0 if application_provisioned.status == "provisioned" else 1
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
    if args.command == "native-gateway-projection":
        print(json.dumps({"schema_version": 1, "columns": PROJECTION_COLUMNS}))
        return 0
    if args.command in {"project-gateway", "project-native-gateway"}:
        raw_projection: Final = sys.stdin.read(8_000_001)
        if len(raw_projection) > 8_000_000:
            print("Gateway projection exceeds size limit", file=sys.stderr)
            return 2
        try:
            projection: Final = GatewayProjectionBatch.model_validate_json(raw_projection)
        except ValidationError:
            print("Gateway projection rejected; source values are not logged", file=sys.stderr)
            return 2
        projected: Final = (
            prepare_native_gateway(projection)
            if args.command == "project-native-gateway"
            else prepare_gateway(projection)
        )
        print(projected.model_dump_json())
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
    if args.command == "collect-github":
        try:
            github_batch: Final = collect_github(
                args.config, datetime.now(UTC), account_id=args.account_id, account_name=args.account_name
            )
        except ValidationError:
            print("Provider account rejected; input values are not logged", file=sys.stderr)
            return 2
        print(github_batch.model_dump_json())
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
    if args.command == "collect-gateway-inventory":
        inventory_key: Final = read_private_key(args.config, "LITELLM_MASTER_KEY", "Gateway")
        if isinstance(inventory_key, Unavailable):
            print(inventory_key.message, file=sys.stderr)
            return 2
        try:
            inventory_result: Final = collect_and_record_gateway_inventory(
                repository, args.gateway_origin, inventory_key, datetime.now(UTC)
            )
        except psycopg.Error:
            print("Inventory store unavailable", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "status": inventory_result.status,
                    "message": inventory_result.message,
                    "sources": [
                        source.model_dump(mode="json")
                        for source in (inventory_result.snapshot.inventory.sources if inventory_result.snapshot else ())
                    ],
                }
            )
        )
        return 0 if inventory_result.status == "collected" else 1
    if args.command == "import-credential-inventory":
        try:
            SOURCE_IDENTIFIER.validate_python(args.source_id)
        except ValidationError:
            print("Credential inventory source ID rejected", file=sys.stderr)
            return 2
        attempted_at: Final = datetime.now(UTC)
        sources_read: Final = credential_sources(args.register, args.plan)
        parsed: Final = (
            parse_inventory(sources_read[0], sources_read[1], args.source_id, attempted_at)
            if sources_read is not None
            else None
        )
        outcome: Final[CredentialInventorySnapshot | tuple[ImportStatus, str]] = (
            ("unreadable", "Credential inventory files are not readable")
            if parsed is None
            else ("rejected", parsed)
            if isinstance(parsed, str)
            else parsed
        )
        try:
            status: Final = repository.record_credential_import(args.source_id, attempted_at, outcome)
        except psycopg.Error:
            print("Inventory store unavailable", file=sys.stderr)
            return 1
        if not isinstance(outcome, CredentialInventorySnapshot):
            print(json.dumps({"status": status, "message": outcome[1]}))
            return 2
        print(
            json.dumps(
                {
                    "status": status,
                    "scanned_at": outcome.scanned_at.isoformat(),
                    "services": len(outcome.services),
                    "candidates": len(outcome.candidates),
                    "quarantined": len(outcome.quarantined),
                    "incomplete_hosts": sum(1 for host in outcome.hosts if not host.complete),
                    "validity": outcome.validity,
                }
            )
        )
        return 0
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
    if args.command == "bind-credential":
        raw_binding: Final = bounded_command(args.path)
        if raw_binding is None:
            print("Binding command is not readable", file=sys.stderr)
            return 2
        if len(raw_binding) > COMMAND_LIMIT:
            print("Binding command exceeds size limit", file=sys.stderr)
            return 2
        try:
            binding_command: Final = CredentialBindingCommand.model_validate_json(raw_binding)
        except ValidationError:
            print("Binding command rejected: invalid data; input values are not logged", file=sys.stderr)
            return 2
        bound: Final = repository.bind_credential(binding_command, args.actor, datetime.now(UTC))
        print(json.dumps({"status": bound.status, "version": bound.version, "message": bound.message}))
        return 0 if bound.status in ("applied", "duplicate") else 1
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
    token_file: Final = os.environ.get("CUBE_CONTROL_ADMIN_TOKEN_FILE", "")
    admin_token: Final = (
        read_private_key(Path(token_file), "CUBE_CONTROL_ADMIN_TOKEN", "Verwaltung") if token_file else None
    )
    if isinstance(admin_token, Unavailable):
        print(admin_token.message, file=sys.stderr)
        return 2
    authority: Final = (
        private_authority(
            admin_token,
            os.environ.get("CUBE_CONTROL_MANAGEMENT_ORIGIN", ""),
            os.environ.get("CUBE_CONTROL_OPERATOR", getpass.getuser()),
        )
        if admin_token is not None
        else None
    )
    if admin_token is not None and authority is None:
        print(
            "Browser-Verwaltung abgelehnt: Token mit mindestens 32 Zeichen, gültige CUBE_CONTROL_MANAGEMENT_ORIGIN "
            "(https oder http nur auf Loopback) und gültiger Bedienername erforderlich",
            file=sys.stderr,
        )
        return 2
    repository.initialize()
    uvicorn.run(
        create_app(
            repository,
            usage_reader=PostgresUsageReader(dsn),
            job_reader=PostgresJobManager(dsn),
            authority=authority,
        ),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )
    return 0
