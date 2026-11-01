import unittest
from datetime import UTC, datetime, timedelta
from typing import Final

from inventory_fixtures import bound, gateway_inventory, snapshot
from pydantic import ValidationError

from cube_control.gateway_inventory import InventoryDeployment, InventoryRejection, InventoryRoute
from cube_control.inventory_evidence import GatewayEvidence, merge_evidence

ORIGIN: Final = "http://127.0.0.1:4000"
FIRST: Final = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
LATER: Final = FIRST + timedelta(minutes=10)
KNOWN: Final = merge_evidence(
    None,
    snapshot(
        ORIGIN,
        FIRST,
        gateway_inventory(
            FIRST,
            credentials=(bound("database", "known", "previously-known-account"),),
            routes=(InventoryRoute(origin="database", binding="unbound"),),
            deployments=(
                InventoryDeployment(origin="runtime", model_name="alias", credential_name="known", auth="unknown"),
            ),
        ),
    ),
)


def credentials(evidence: GatewayEvidence) -> list[tuple[str, str, datetime]]:
    return [(item.credential.credential_name, item.presence, item.observed_at) for item in evidence.credentials]


class MergeEvidenceTests(unittest.TestCase):
    def test_an_unavailable_source_keeps_known_records_as_retained_while_other_sources_update(self) -> None:
        merged: Final = merge_evidence(
            KNOWN,
            snapshot(
                ORIGIN,
                LATER,
                gateway_inventory(
                    LATER,
                    routes=(
                        InventoryRoute(origin="runtime", route_id="new", account_id="new-account", binding="account"),
                    ),
                    unavailable=frozenset({("credentials", "database")}),
                ),
            ),
        )
        self.assertEqual(credentials(merged), [("known", "retained", FIRST)])
        self.assertEqual(
            [(item.route.route_id, item.presence) for item in merged.routes if item.route.origin == "runtime"],
            [("new", "latest_collection")],
        )
        self.assertEqual(
            [
                (source.status, source.observed_at, source.last_readable_at)
                for source in merged.sources
                if (source.name, source.origin) == ("credentials", "database")
            ],
            [("unavailable", LATER, FIRST)],
        )

    def test_a_partial_source_replaces_matching_records_and_retains_the_rest(self) -> None:
        previous: Final = merge_evidence(
            None,
            snapshot(
                ORIGIN,
                FIRST,
                gateway_inventory(FIRST, credentials=(bound("database", "a", "one"), bound("database", "b", "two"))),
            ),
        )
        merged: Final = merge_evidence(
            previous,
            snapshot(
                ORIGIN,
                LATER,
                gateway_inventory(
                    LATER,
                    credentials=(bound("database", "a", "one"),),
                    rejected=(
                        InventoryRejection(
                            source="credentials", origin="database", index=1, reason="unreadable_fields"
                        ),
                    ),
                ),
            ),
        )
        self.assertEqual(credentials(merged), [("a", "latest_collection", LATER), ("b", "retained", FIRST)])

    def test_a_complete_empty_source_clears_obsolete_records(self) -> None:
        merged: Final = merge_evidence(KNOWN, snapshot(ORIGIN, LATER, gateway_inventory(LATER)))
        self.assertEqual((merged.credentials, merged.routes, merged.deployments), ((), (), ()))

    def test_older_or_equal_snapshots_never_replace_newer_evidence(self) -> None:
        empty_but_old: Final = snapshot(ORIGIN, LATER, gateway_inventory(FIRST))
        self.assertEqual(merge_evidence(KNOWN, empty_but_old), KNOWN)

    def test_records_without_a_stable_identity_never_erase_retained_records(self) -> None:
        merged: Final = merge_evidence(
            KNOWN,
            snapshot(
                ORIGIN,
                LATER,
                gateway_inventory(
                    LATER,
                    routes=(InventoryRoute(origin="database", binding="unbound"),),
                    deployments=(InventoryDeployment(origin="runtime", model_name="alias", auth="inline_api_key"),),
                    rejected=(
                        InventoryRejection(source="routes", origin="database", index=1, reason="not_an_object"),
                        InventoryRejection(source="deployments", origin="runtime", index=1, reason="not_an_object"),
                    ),
                ),
            ),
        )
        self.assertEqual(
            [(item.presence, item.observed_at) for item in merged.routes],
            [("latest_collection", LATER), ("retained", FIRST)],
        )
        self.assertEqual(
            [(item.deployment.credential_name, item.presence) for item in merged.deployments],
            [(None, "latest_collection"), ("known", "retained")],
        )

    def test_evidence_must_describe_every_source_exactly_once(self) -> None:
        sources: Final = KNOWN.sources
        for invalid in ((), sources[1:], (*sources[1:], sources[1]), (*sources, sources[0])):
            with self.subTest(count=len(invalid)), self.assertRaises(ValidationError):
                GatewayEvidence.model_validate({**KNOWN.model_dump(), "sources": invalid})
