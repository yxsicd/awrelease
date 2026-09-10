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
    def endpoints(self):
        return [
            {"platform": item, "url": f"https://{item}.example.test", "channel": "prod"}
            for item in MESH.PLATFORMS
        ]

    def test_topology_authorizes_every_ephemeral_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "topology.json"
            MESH.write_topology(path, self.endpoints())
            topology = json.loads(path.read_text())
        gateway_ids = [f"gha-{item}" for item in MESH.PLATFORMS]
        self.assertEqual(gateway_ids, [item["id"] for item in topology["rgws"]])
        self.assertEqual(gateway_ids, topology["registrationPolicies"][0]["publicGatewayIds"])
        self.assertEqual([MESH.CLUSTER_ID], topology["registrationPolicies"][0]["gatewayClusterIds"])
        self.assertTrue(all(item["wsUrl"].endswith("/ws?role=upstream") for item in topology["rgws"]))

    def test_all_in_one_environment_connects_every_surviving_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory)
            endpoints = self.endpoints()
            own = endpoints[0]
            remote_gws = [
                f"{item['url'].replace('https://', 'wss://', 1)}/ws?role=upstream"
                for item in endpoints
                if item != own
            ]
            env = MESH.gateway_environment(
                state, "prod", own["platform"], own["url"], 17888,
                "all-in-one", remote_gws, "signed-token",
            )
        self.assertEqual("all-in-one", env["AGENTGW_MODE"])
        self.assertEqual("all", env["AGENTGW_UPSTREAM_MODE"])
        self.assertEqual(3, len(env["AGENTGW_REMOTE_GWS"].split(",")))
        self.assertEqual("signed-token", env["AGENTGW_ENROLLMENT_TOKEN"])

    def test_endpoint_bundle_requires_all_four_platforms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "endpoint.json").write_text(json.dumps({"gateways": self.endpoints()}))
            self.assertEqual(list(MESH.PLATFORMS), [item["platform"] for item in MESH.read_endpoints(root)])

    def test_path_router_maps_one_public_origin_to_four_backends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "haproxy.cfg"
            ports = {item: 18080 + index for index, item in enumerate(MESH.PLATFORMS)}
            MESH.write_haproxy_config(path, 19000, ports)
            config = path.read_text()
        for item in MESH.PLATFORMS:
            self.assertIn(f"path_beg /{item}", config)
            self.assertIn(f"127.0.0.1:{ports[item]}", config)


if __name__ == "__main__":
    unittest.main()
