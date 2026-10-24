from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Literal

import psycopg
from psycopg import Connection

from cube_control.models import GatewayBatch, GatewayRecord, GatewayScan, GatewayScanView

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS cube_control.gateway_scans (
    id text PRIMARY KEY, source_id text NOT NULL, sequence bigint NOT NULL,
    fingerprint text NOT NULL, payload jsonb NOT NULL, UNIQUE(source_id,sequence)
);
CREATE TABLE IF NOT EXISTS cube_control.gateway_records (
    source_id text NOT NULL, source_key text NOT NULL, content_hash text NOT NULL,
    payload jsonb NOT NULL, first_sequence bigint NOT NULL, last_sequence bigint NOT NULL,
    PRIMARY KEY(source_id,source_key)
);
CREATE TABLE IF NOT EXISTS cube_control.gateway_changes (
    source_id text NOT NULL, source_key text NOT NULL, sequence bigint NOT NULL,
    previous_hash text, content_hash text NOT NULL, payload jsonb NOT NULL,
    PRIMARY KEY(source_id,source_key,sequence)
);
"""


@dataclass(frozen=True, slots=True)
class GatewayResult:
    status: Literal["applied", "duplicate", "conflict", "rejected", "unavailable"]
    sequence: int = 0
    changed: int = 0
    message: str = ""


def latest_scans(connection: Connection[tuple[str, ...]]) -> tuple[GatewayScanView, ...]:
    return tuple(
        GatewayScanView(scan=GatewayScan.model_validate_json(row[0]), sequence=int(row[1]))
        for row in connection.execute(
            "SELECT DISTINCT ON (source_id) payload::text,sequence::text FROM cube_control.gateway_scans "
            "ORDER BY source_id,sequence DESC"
        )
    )


def current_records(connection: Connection[tuple[str, ...]]) -> tuple[GatewayRecord, ...]:
    return tuple(
        GatewayRecord.model_validate_json(row[0])
        for row in connection.execute(
            "SELECT payload::text FROM cube_control.gateway_records "
            "WHERE (payload->>'occurred_at')::timestamptz >= date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
            "AND (payload->>'occurred_at')::timestamptz < (date_trunc('month',now() AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC'"
        )
    )


@dataclass(frozen=True, slots=True)
class GatewayRepository:
    dsn: str

    def connect(self) -> Connection[tuple[str, ...]]:
        return psycopg.connect(self.dsn, connect_timeout=5)

    def import_batch(self, batch: GatewayBatch) -> GatewayResult:
        fingerprint: Final = sha256(batch.model_dump_json().encode()).hexdigest()
        try:
            with self.connect() as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('cube-control-import'))")
                previous_scan: Final = connection.execute(
                    "SELECT fingerprint,sequence::text FROM cube_control.gateway_scans WHERE id=%s",
                    (batch.scan.scan_id,),
                ).fetchone()
                if previous_scan:
                    if previous_scan[0] == fingerprint:
                        return GatewayResult("duplicate", int(previous_scan[1]))
                    return GatewayResult("rejected", message="Scan identity conflict")
                head: Final = connection.execute(
                    "SELECT COALESCE(max(sequence),0)::text FROM cube_control.gateway_scans WHERE source_id=%s",
                    (batch.scan.source_id,),
                ).fetchone()
                sequence: Final[int] = int(head[0]) if head else 0
                if sequence != batch.scan.expected_sequence:
                    return GatewayResult("conflict", sequence, message="Refresh source before another scan")
                next_sequence: Final = sequence + 1
                connection.execute(
                    "INSERT INTO cube_control.gateway_scans(id,source_id,sequence,fingerprint,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
                    (
                        batch.scan.scan_id,
                        batch.scan.source_id,
                        next_sequence,
                        fingerprint,
                        batch.scan.model_dump_json(),
                    ),
                )
                changed: Final[int] = sum(
                    self.mirror(connection, batch.scan.source_id, next_sequence, record) for record in batch.records
                )
                return GatewayResult("applied", next_sequence, changed)
        except psycopg.Error:
            return GatewayResult("unavailable", message="Gateway import result uncertain; retry the same scan ID")

    def mirror(
        self, connection: Connection[tuple[str, ...]], source_id: str, sequence: int, record: GatewayRecord
    ) -> int:
        payload: Final = record.model_dump_json()
        content_hash: Final = sha256(payload.encode()).hexdigest()
        previous: Final = connection.execute(
            "SELECT content_hash FROM cube_control.gateway_records WHERE source_id=%s AND source_key=%s",
            (source_id, record.source_key),
        ).fetchone()
        if previous and previous[0] == content_hash:
            connection.execute(
                "UPDATE cube_control.gateway_records SET last_sequence=%s WHERE source_id=%s AND source_key=%s",
                (sequence, source_id, record.source_key),
            )
            return 0
        connection.execute(
            "INSERT INTO cube_control.gateway_changes(source_id,source_key,sequence,previous_hash,content_hash,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
            (source_id, record.source_key, sequence, previous[0] if previous else None, content_hash, payload),
        )
        connection.execute(
            "INSERT INTO cube_control.gateway_records(source_id,source_key,content_hash,payload,first_sequence,last_sequence) "
            "VALUES (%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(source_id,source_key) DO UPDATE "
            "SET content_hash=EXCLUDED.content_hash,payload=EXCLUDED.payload,last_sequence=EXCLUDED.last_sequence",
            (source_id, record.source_key, content_hash, payload, sequence, sequence),
        )
        return 1
