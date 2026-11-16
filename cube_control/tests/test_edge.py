import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from cube_control.edge import GatewayEdgePlan, GatewayEdgeSpec, render_gateway_edge

PROXY: Final = Path("/usr/lib/systemd/systemd-socket-proxyd")


class EdgeTests(unittest.TestCase):
    def test_public_backends_admin_paths_and_invalid_ports_are_rejected(self) -> None:
        for changes in (
            {"backend_host": "8.8.8.8"},
            {"backend_host": "0.0.0.0"},
            {"backend_host": "::"},
            {"backend_host": "fe80::1%eth0"},
            {"backend_host": "gateway.example.invalid"},
            {"backend_host": "192.168.1.10;echo unsafe"},
            {"local_port": 8443},
            {"backend_port": True},
            {"paths": ["/"]},
            {"paths": ["/credentials"]},
            {"paths": ["/v1/../credentials"]},
            {"paths": ["/%63redentials"]},
            {"paths": ["/v1", "/v1"]},
            {"backend_host": "127.0.0.1", "backend_port": 18400},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                GatewayEdgeSpec.model_validate({"backend_host": "192.168.1.10"} | changes)

    def test_plan_preserves_api_prefixes_and_never_resets_other_tailnet_services(self) -> None:
        spec: Final = GatewayEdgeSpec(backend_host="192.168.1.10", paths=("/v2", "/v1"))
        plan: Final = render_gateway_edge(spec)
        self.assertEqual(plan.scope, "tailnet_only")
        self.assertTrue(plan.gateway_auth_required)
        self.assertIn(
            (
                "tailscale",
                "serve",
                "--bg",
                "--https=8443",
                "--set-path=/v1/",
                "http://127.0.0.1:18400/v1/",
            ),
            plan.activate,
        )
        self.assertIn(("tailscale", "serve", "--https=8443", "--set-path=/v2/", "off"), plan.deactivate)
        self.assertFalse(any("reset" in command or "funnel" in command for command in plan.activate + plan.deactivate))
        self.assertEqual(GatewayEdgePlan.model_validate_json(plan.model_dump_json()), plan)

    def test_private_ipv6_target_is_bracketed_for_the_standard_relay(self) -> None:
        plan: Final = render_gateway_edge(GatewayEdgeSpec(backend_host="fd00::10"))
        self.assertIn("[fd00::10]:4000", plan.units[1].content)

    @unittest.skipUnless(PROXY.exists(), "The server's standard systemd relay binary is required")
    def test_rendered_units_are_accepted_by_the_systemd_verifier(self) -> None:
        plan: Final = render_gateway_edge(GatewayEdgeSpec(backend_host="192.168.1.10"))
        with tempfile.TemporaryDirectory() as directory:
            for unit in plan.units:
                Path(directory, unit.name).write_text(unit.content)
            result: Final = subprocess.run(
                ["systemd-analyze", "verify", *(str(Path(directory, unit.name)) for unit in plan.units)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_preparation_cli_needs_no_database_and_does_not_echo_invalid_input(self) -> None:
        environment: Final = {name: value for name, value in os.environ.items() if name != "CUBE_CONTROL_DATABASE_URL"}
        prepared: Final = subprocess.run(
            [sys.executable, "-m", "cube_control", "prepare-edge"],
            input='{"backend_host":"192.168.1.10"}',
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(GatewayEdgePlan.model_validate_json(prepared.stdout).state, "prepared")
        rejected: Final = subprocess.run(
            [sys.executable, "-m", "cube_control", "prepare-edge"],
            input=json.dumps({"backend_host": "192.168.1.10", "password": "public-canary-not-a-credential"}),
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertNotIn("public-canary-not-a-credential", rejected.stdout + rejected.stderr)

    @unittest.skipUnless(PROXY.exists(), "The server's standard systemd relay binary is required")
    def test_actual_relay_preserves_binary_request_and_half_close_without_logging_content(self) -> None:
        payload: Final = b"public-canary-authorization\r\n" + bytes(range(256)) * 512
        with socket.socket() as backend, socket.socket() as relay:
            backend.bind(("127.0.0.1", 0))
            backend.listen()
            backend.settimeout(5)
            relay.bind(("127.0.0.1", 0))
            relay.listen()

            def echo() -> None:
                with backend.accept()[0] as connection, connection.makefile("rwb") as stream:
                    stream.write(stream.read())
                    stream.flush()

            thread: Final = threading.Thread(target=echo, daemon=True)
            thread.start()
            launcher: Final = (
                "import os,sys; os.dup2(int(sys.argv[1]),3,inheritable=True); "
                "env={'LISTEN_FDS':'1','LISTEN_PID':str(os.getpid()),'LANG':'C'}; "
                "os.execve(sys.argv[2],[sys.argv[2],sys.argv[3]],env)"
            )
            process: Final = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    launcher,
                    str(relay.fileno()),
                    str(PROXY),
                    f"127.0.0.1:{backend.getsockname()[1]}",
                ],
                pass_fds=(relay.fileno(),),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                with socket.create_connection(relay.getsockname(), timeout=5) as client:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    with client.makefile("rb") as response:
                        self.assertEqual(response.read(), payload)
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            finally:
                process.terminate()
                output: Final = process.communicate(timeout=5)
            self.assertNotIn(b"public-canary-authorization", b"".join(output))
