import pathlib
import sys
import unittest
from unittest import mock
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_artifacts as ARTIFACTS


def manifest(service, role=None):
    expected = ARTIFACTS.GATEWAY if service == "agentgw-server" else ARTIFACTS.CLIENT
    value = {"schemaVersion": 1, "service": service, "channel": "main", "gitSha": "a" * 12,
             "gitDirty": False, "buildProfile": "release",
             "artifacts": {key: {"filename": name, "target": target, "size": 42,
                                 "sha256": "b" * 64, "compression": "none"}
                           for key, (name, target) in expected.items()}}
    if role is not None:
        value.update(buildRole=role, buildFeatures=[])
    return value


class PublicRoleSelectionTests(unittest.TestCase):
    def test_prefers_independent_server_manifest(self):
        server = manifest("agentgw-server", "gateway")
        with mock.patch.object(ARTIFACTS, "load_manifest", return_value=server) as loaded:
            self.assertEqual(server, ARTIFACTS.gateway_manifest("main"))
        loaded.assert_called_once_with("agentgw-server", "main", "yxsicd/awrelease", None)

    def test_only_missing_server_can_use_unlabelled_legacy(self):
        legacy = manifest("agentgw")
        with mock.patch.object(ARTIFACTS, "load_manifest", side_effect=[ARTIFACTS.ManifestMissing(), legacy]):
            self.assertEqual(legacy, ARTIFACTS.gateway_manifest("main"))
        for service in ("auto", "agentgw"):
            values = [ARTIFACTS.ManifestMissing(), manifest("agentgw", "client")] if service == "auto" else [manifest("agentgw", "client")]
            with self.subTest(service=service), mock.patch.object(ARTIFACTS, "load_manifest", side_effect=values):
                with self.assertRaisesRegex(RuntimeError, "client release cannot serve as gateway"):
                    ARTIFACTS.gateway_manifest("main", service=service)

    def test_strict_server_missing_does_not_fall_back(self):
        with mock.patch.object(ARTIFACTS, "load_manifest", side_effect=ARTIFACTS.ManifestMissing()) as loaded:
            with self.assertRaises(ARTIFACTS.ManifestMissing):
                ARTIFACTS.gateway_manifest("main", service="agentgw-server")
        self.assertEqual(1, loaded.call_count)

    def test_transport_auth_json_and_invalid_server_never_fall_back(self):
        for error in (OSError("transport"), ValueError("JSON"),
                      urllib.error.HTTPError("https://example.test", 403, "denied", {}, None)):
            with self.subTest(error=error), mock.patch.object(ARTIFACTS, "load_manifest", side_effect=error) as loaded:
                with self.assertRaises(type(error)):
                    ARTIFACTS.gateway_manifest("main")
                self.assertEqual(1, loaded.call_count)
        invalid = manifest("agentgw-server", "client")
        with mock.patch.object(ARTIFACTS, "load_manifest", return_value=invalid) as loaded:
            with self.assertRaisesRegex(RuntimeError, "role mismatch"):
                ARTIFACTS.gateway_manifest("main")
        self.assertEqual(1, loaded.call_count)

    def test_server_complete_set_names_target_and_hash_are_required(self):
        for failure in ("partial", "windows", "client-name", "sha", "source", "channel"):
            value = manifest("agentgw-server", "gateway")
            if failure == "partial": value["artifacts"].pop("linux-arm64-musl")
            if failure == "windows": value["artifacts"]["linux-x64-musl"]["target"] = "x86_64-pc-windows-gnu"
            if failure == "client-name": value["artifacts"]["linux-x64-musl"]["filename"] = "agentgw-linux-x64"
            if failure == "sha": value["artifacts"]["linux-x64-musl"]["sha256"] = "bad"
            if failure == "source": value["gitDirty"] = True
            if failure == "channel": value["channel"] = "prod"
            with self.subTest(failure=failure), self.assertRaises(RuntimeError):
                ARTIFACTS.validate_manifest(value, "agentgw-server", "main")

    def test_client_manifest_cannot_activate_heavy_gateway_feature(self):
        value = manifest("agentgw", "client")
        value["buildFeatures"] = ["memory-next-gateway"]
        with self.assertRaisesRegex(RuntimeError, "gateway dependencies"):
            ARTIFACTS.validate_manifest(value, "agentgw", "main")

    def test_http_404_is_distinct_from_other_failures(self):
        for status in (404, 403):
            error = urllib.error.HTTPError("https://example.test", status, "fixture", {}, None)
            with mock.patch.object(ARTIFACTS.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(ARTIFACTS.ManifestMissing if status == 404 else urllib.error.HTTPError):
                    ARTIFACTS.load_manifest("agentgw-server", "main")


if __name__ == "__main__":
    unittest.main()
