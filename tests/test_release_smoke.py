import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_smoke", ROOT / "scripts" / "release_smoke.py"
)
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class PublicInterfaceCoverageTests(unittest.TestCase):
    def openapi(self):
        return {
            "openapi": "3.1.0",
            "paths": {
                f"/test/{index}": {"get": {"operationId": operation}}
                for index, operation in enumerate(sorted(SMOKE.EXPECTED_PUBLIC_OPERATIONS))
            },
            "components": {
                "securitySchemes": {
                    "RgwHeaderToken": {},
                    "RgwBearerToken": {},
                }
            },
        }

    def test_openapi_requires_an_explicit_test_classification_for_every_operation(self):
        operations = SMOKE.smoke_openapi(self.openapi())
        self.assertEqual(SMOKE.EXPECTED_PUBLIC_OPERATIONS, set(operations))

    def test_openapi_rejects_an_unclassified_new_public_operation(self):
        document = self.openapi()
        document["paths"]["/new"] = {"post": {"operationId": "newPublicOperation"}}
        with self.assertRaisesRegex(RuntimeError, "changed without release-smoke coverage"):
            SMOKE.smoke_openapi(document)

    def test_mcp_structured_falls_back_to_json_text_content(self):
        value = SMOKE.mcp_structured(
            {"content": [{"type": "text", "text": '{"ok":true,"count":1}'}]}
        )
        self.assertEqual({"ok": True, "count": 1}, value)


if __name__ == "__main__":
    unittest.main()
