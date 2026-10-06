"""Regression checks for overlapping route permissions; no private checkout needed."""

import json
import unittest
from sync_gateway_routes import catalog


def operation(audience="player", callers=None, owner="User Management"):
    return {"tags": [owner], "x-audience": audience, "x-allowed-callers": callers or []}


class CatalogTests(unittest.TestCase):
    def spec(self, literal, pattern):
        return {
            "paths": {
                "/v1/users/me": {"get": literal},
                "/v1/users/{userId}": {"get": pattern},
            }
        }

    def test_existing_player_overlap_allowed(self):
        self.assertEqual(
            len(json.loads(catalog(self.spec(operation(), operation())))), 2
        )

    def test_different_audience_rejected_in_either_order(self):
        spec = self.spec(operation("public"), operation())
        for paths in (spec["paths"], dict(reversed(list(spec["paths"].items())))):
            with self.assertRaisesRegex(ValueError, "Conflicting Gateway permissions"):
                catalog({"paths": paths})

    def test_different_internal_caller_permissions_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting Gateway permissions"):
            catalog(
                self.spec(
                    operation("internal", ["map"]), operation("internal", ["battle"])
                )
            )

    def test_same_callers_in_different_order_allowed(self):
        self.assertEqual(
            len(
                json.loads(
                    catalog(
                        self.spec(
                            operation("internal", ["map", "battle"]),
                            operation("internal", ["battle", "map"]),
                        )
                    )
                )
            ),
            2,
        )

    def test_different_owners_and_methods_do_not_conflict(self):
        self.assertEqual(
            len(
                json.loads(
                    catalog(self.spec(operation("public"), operation(owner="Battle")))
                )
            ),
            2,
        )
        spec = self.spec(operation("public"), operation())
        spec["paths"]["/v1/users/{userId}"] = {"post": operation()}
        self.assertEqual(len(json.loads(catalog(spec))), 2)


if __name__ == "__main__":
    unittest.main()
