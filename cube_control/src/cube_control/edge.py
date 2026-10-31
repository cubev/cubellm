from ipaddress import ip_address, ip_network
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from cube_control.models import FrozenModel

PRIVATE_BACKENDS: Final = tuple(
    ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "100.64.0.0/10",
        "fc00::/7",
        "::1/128",
    )
)
ADMIN_PREFIXES: Final = frozenset(
    {
        "key",
        "credentials",
        "model",
        "team",
        "user",
        "project",
        "organization",
        "config",
        "ui",
        "sso",
        "global",
    }
)
EDGE_SERVICE: Final = "cubellm-gateway-edge"


class GatewayEdgeSpec(FrozenModel):
    backend_host: Annotated[str, Field(min_length=1, max_length=45)]
    backend_port: Annotated[int, Field(ge=1, le=65535, strict=True)] = 4000
    local_port: Annotated[int, Field(ge=1024, le=65535, strict=True)] = 18400
    https_port: Annotated[int, Field(ge=1, le=65535, strict=True)] = 8443
    paths: Annotated[
        tuple[Annotated[str, Field(pattern=r"^/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$", max_length=100)], ...],
        Field(min_length=1, max_length=16),
    ] = ("/v1",)

    @model_validator(mode="after")
    def private_backend_and_paths(self) -> Self:
        address: Final = ip_address(self.backend_host)
        if "%" in self.backend_host or not any(
            address.version == network.version and address in network for network in PRIVATE_BACKENDS
        ):
            raise ValueError("Gateway backend must be a private or loopback address")
        if self.local_port == self.https_port:
            raise ValueError("Local relay and Tailnet HTTPS ports must be different")
        if address.is_loopback and self.local_port == self.backend_port:
            raise ValueError("Local relay cannot forward to its own listening port")
        if len(frozenset(self.paths)) != len(self.paths):
            raise ValueError("Gateway paths must be unique")
        if any(path.split("/")[1].casefold() in ADMIN_PREFIXES for path in self.paths):
            raise ValueError("Administration namespaces cannot be published as application paths")
        return self


class EdgeUnit(FrozenModel):
    name: str
    content: str


class GatewayEdgePlan(FrozenModel):
    schema_version: Literal[1] = 1
    state: Literal["prepared"] = "prepared"
    scope: Literal["tailnet_only"] = "tailnet_only"
    gateway_auth_required: Literal[True] = True
    spec: GatewayEdgeSpec
    units: tuple[EdgeUnit, ...]
    activate: tuple[tuple[str, ...], ...]
    deactivate: tuple[tuple[str, ...], ...]


def render_gateway_edge(spec: GatewayEdgeSpec) -> GatewayEdgePlan:
    address: Final = ip_address(spec.backend_host)
    target: Final = (
        f"[{address.compressed}]:{spec.backend_port}" if address.version == 6 else f"{address}:{spec.backend_port}"
    )
    socket: Final = EdgeUnit(
        name=EDGE_SERVICE + ".socket",
        content=(
            "[Unit]\nDescription=CubeLLM private gateway relay socket\n\n"
            f"[Socket]\nListenStream=127.0.0.1:{spec.local_port}\nNoDelay=true\n\n"
            "[Install]\nWantedBy=sockets.target\n"
        ),
    )
    service: Final = EdgeUnit(
        name=EDGE_SERVICE + ".service",
        content=(
            "[Unit]\nDescription=CubeLLM private gateway relay\n\n[Service]\n"
            f"ExecStart=/usr/lib/systemd/systemd-socket-proxyd --exit-idle-time=5min {target}\n"
            "DynamicUser=yes\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
            "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\nIPAddressDeny=any\nIPAddressAllow=localhost\n"
            f"IPAddressAllow={address.compressed}\n"
        ),
    )
    activate: Final = tuple(
        (
            "tailscale",
            "serve",
            "--bg",
            f"--https={spec.https_port}",
            f"--set-path={path}/",
            f"http://127.0.0.1:{spec.local_port}{path}/",
        )
        for path in sorted(spec.paths)
    )
    deactivate: Final = tuple(
        ("tailscale", "serve", f"--https={spec.https_port}", f"--set-path={path}/", "off")
        for path in sorted(spec.paths)
    )
    return GatewayEdgePlan(
        spec=spec,
        units=(socket, service),
        activate=(("systemctl", "daemon-reload"), ("systemctl", "enable", "--now", socket.name)) + activate,
        deactivate=deactivate
        + (
            ("systemctl", "disable", "--now", socket.name),
            ("systemctl", "stop", service.name),
        ),
    )
