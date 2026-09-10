import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cross_platform_mesh", ROOT / "scripts" / "cross_platform_mesh.py"
)
MESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MESH)


class RedundantMeshTests(unittest.TestCase):
    def bundle(self):
        return {
            "channel": "prod",
            "centralGateways": [
                {"id": name, "url": f"https://mesh.example.test/{name}"}
                for name in MESH.CENTRAL_IDS
            ],
            "platforms": list(MESH.PLATFORMS),
        }

    def test_host_topology_enrolls_mabc_only_into_the_host_rgw(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "topology.json"
            MESH.write_host_topology(path, "linux-x64", "http://127.0.0.1:17888")
            topology = json.loads(path.read_text())
        self.assertEqual(["gha-host-linux-x64"], [item["id"] for item in topology["rgws"]])
        self.assertEqual(
            ["gha-host-linux-x64"],
            topology["registrationPolicies"][0]["publicGatewayIds"],
        )
        self.assertEqual(
            "ws://127.0.0.1:17888/ws?role=upstream",
            topology["rgws"][0]["wsUrl"],
        )

    def test_host_rgw_is_a_separate_remote_process_with_two_upstreams(self):
        with tempfile.TemporaryDirectory() as directory:
            env = MESH.gateway_environment(
                pathlib.Path(directory), "prod", "remote", 17888,
                MESH.host_gateway_id("linux-x64"), "gha-host-linux-x64",
                "http://127.0.0.1:17888",
                ["wss://mesh.example.test/rgw-a/ws?role=upstream",
                 "wss://mesh.example.test/rgw-b/ws?role=upstream"], True,
            )
        self.assertEqual("remote", env["AGENTGW_MODE"])
        self.assertEqual("true", env["AGENTGW_REQUIRE_ENROLLMENT_TOKEN"])
        self.assertEqual("all", env["AGENTGW_UPSTREAM_MODE"])
        self.assertEqual(2, len(env["AGENTGW_REMOTE_GWS"].split(",")))

    def test_endpoint_bundle_requires_two_central_rgws(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "endpoint.json").write_text(json.dumps(self.bundle()))
            parsed = MESH.read_bundle(root)
        self.assertEqual(list(MESH.CENTRAL_IDS), [item["id"] for item in parsed["centralGateways"]])

    def test_path_router_maps_one_public_origin_to_two_central_backends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "haproxy.cfg"
            ports = {"rgw-a": 18080, "rgw-b": 18081}
            MESH.write_haproxy_config(path, 19000, ports)
            config = path.read_text()
        for gateway in MESH.CENTRAL_IDS:
            self.assertIn(f"path_beg /{gateway}", config)
            self.assertIn(f"127.0.0.1:{ports[gateway]}", config)

    def test_nested_peer_platform_reads_transitive_advertisement(self):
        peer = {"id": "lgw_x", "deviceName": "gha-windows-x64-123"}
        self.assertEqual("windows-x64", MESH.peer_platform(peer))


if __name__ == "__main__":
    unittest.main()
