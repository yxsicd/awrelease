import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


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

    def test_hosted_edge_topology_uses_secure_websocket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "topology.json"
            MESH.write_host_topology(path, "windows-x64", "https://mesh.example.test/edge-windows-x64")
            value = json.loads(path.read_text())
        self.assertEqual("wss://mesh.example.test/edge-windows-x64/ws?role=upstream", value["rgws"][0]["wsUrl"])

    def test_mac_and_windows_select_linux_hosted_gateway_without_native_process(self):
        for platform in MESH.HOSTED_PLATFORMS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                bundle = self.bundle()
                bundle["hostedEdgeGateways"] = [{"platform": platform,
                    "url": "https://mesh.example.test/edge-" + platform,
                    "nodeId": "rgw_fixture", "gatewayPlatform": "linux-x64"}]
                endpoints = root / "endpoint.json"; endpoints.write_text(json.dumps(bundle))
                with mock.patch.object(MESH, "download_agentgw") as download, \
                     mock.patch.object(MESH, "start_detached") as start, \
                     mock.patch.object(MESH, "wait_json", return_value={"nodeId": "rgw_fixture"}):
                    MESH.edge_start(root / "edge", endpoints, platform)
                download.assert_not_called(); start.assert_not_called()
                result = json.loads((root / "edge/edge.json").read_text())
                self.assertEqual("prod", result["channel"])
                self.assertIsNone(result["pid"])
                self.assertTrue(result["hostedLinuxGateway"])
                self.assertEqual("linux-x64", result["gatewayPlatform"])

    def test_coordinator_starts_two_central_and_two_hosted_linux_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            child = mock.Mock(pid=123)
            with mock.patch.object(MESH, "download_agentgw", return_value=root / "agentgw-server-linux-x64"), \
                 mock.patch.object(MESH, "artifact_key", return_value="linux-x64-musl"), \
                 mock.patch.object(MESH, "free_port", side_effect=range(18080, 18085)), \
                 mock.patch.object(MESH.subprocess, "run"), \
                 mock.patch.object(MESH, "start_detached", return_value=child) as start, \
                 mock.patch.object(MESH, "start_public_tunnel", return_value=(child, "https://mesh.example.test")), \
                 mock.patch.object(MESH, "wait_json", return_value={"nodeId": "rgw_fixture"}):
                MESH.central_start(root, "prod")
            runtime = json.loads((root / "runtime.json").read_text())
            bundle = json.loads((root / "endpoint.json").read_text())
            config = (root / "haproxy.cfg").read_text()
        self.assertEqual(5, start.call_count)  # One router plus four Linux gateways.
        self.assertEqual(2, len(runtime["centralGateways"]))
        self.assertEqual(2, len(runtime["hostedEdgeGateways"]))
        self.assertEqual(set(MESH.HOSTED_PLATFORMS), {item["platform"] for item in bundle["hostedEdgeGateways"]})
        for platform in MESH.HOSTED_PLATFORMS:
            self.assertIn("path_beg /edge-" + platform, config)

    def test_failover_retains_command_file_and_recovery_checks_for_all_twelve_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = {"binary": "fixture-server", "centralGateways": [
                {"id": name, "pid": index + 1, "port": 18080 + index}
                for index, name in enumerate(MESH.CENTRAL_IDS)]}
            (root / "runtime.json").write_text(json.dumps(runtime))
            (root / "endpoint.json").write_text(json.dumps(self.bundle()))
            (root / "rgw-a").mkdir(); (root / "rgw-a/gateway-env.json").write_text("{}")
            peers = [{"id": f"lgw_{platform}_{role}", "deviceName": f"gha-{platform}-fixture"}
                     for platform in MESH.PLATFORMS for role in ("ma", "mb", "mc")]
            with mock.patch.object(MESH, "stop_pid") as stopped, \
                 mock.patch.object(MESH, "wait_endpoint_down") as down, \
                 mock.patch.object(MESH, "wait_for_nested_mabc", return_value=([{}] * 4, peers)), \
                 mock.patch.object(MESH, "route", return_value={"ok": True}) as route, \
                 mock.patch.object(MESH, "verify_exec_and_file", return_value=3) as effects, \
                 mock.patch.object(MESH, "start_detached", return_value=mock.Mock(pid=99)), \
                 mock.patch.object(MESH, "wait_json"):
                MESH.gateway_failover_test(root, root / "endpoint.json", root / "result.json")
            result = json.loads((root / "result.json").read_text())
        stopped.assert_called_once_with(1); down.assert_called_once()
        self.assertEqual(12, effects.call_count)
        self.assertEqual(24, route.call_count)
        self.assertEqual(12, result["failoverExecFilePeerCount"])
        self.assertEqual(12, result["recoveredMabcCount"])
        self.assertTrue(result["failedEndpointObservedDown"])

    def test_nested_peer_platform_reads_transitive_advertisement(self):
        peer = {"id": "lgw_x", "deviceName": "gha-windows-x64-123"}
        self.assertEqual("windows-x64", MESH.peer_platform(peer))

    def test_exec_falls_back_to_poll_when_windows_returns_a_running_handle(self):
        completed = {
            "ok": True,
            "found": True,
            "state": "completed",
            "record": {"exitCode": 0, "stdout": "done"},
        }
        with mock.patch.object(MESH, "route", return_value=completed) as routed, \
             mock.patch.object(MESH.time, "sleep"):
            result = MESH.wait_exec_result(
                "https://mesh.example.test/rgw-a",
                "lgw_windows",
                {"ok": True, "running": True, "execId": "exec-1", "pollAfterMs": 250},
                "upstream_local_peer",
            )
        self.assertEqual(completed, result)
        routed.assert_called_once_with(
            "https://mesh.example.test/rgw-a",
            "lgw_windows",
            "admin.system.exec.poll",
            {"execId": "exec-1"},
            "upstream_local_peer",
        )


if __name__ == "__main__":
    unittest.main()
