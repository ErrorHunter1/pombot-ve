"""Erzeugt Netzwerk-Konfigurationen (netplan/cloud-init v2, systemd-networkd, ifupdown).

`ips` hat das Format: [{"address": "203.0.113.10", "prefix": 24, "gateway": "203.0.113.1", "version": 4}]
"""
import ipaddress


def _gateway_on_link(ip: dict) -> bool:
    if not ip.get("gateway"):
        return False
    net = ipaddress.ip_network(f"{ip['address']}/{ip['prefix']}", strict=False)
    return ipaddress.ip_address(ip["gateway"]) not in net


def v2_ethernet(ips: list[dict], dns: list[str], match: dict | None = None) -> dict:
    """Ethernet-Eintrag im netplan/cloud-init-v2-Format."""
    eth: dict = {}
    if match:
        eth["match"] = match
        eth["set-name"] = "eth0"
    if not ips:
        eth["dhcp4"] = True
        return eth
    eth["dhcp4"] = False
    eth["dhcp6"] = False
    eth["addresses"] = [f"{ip['address']}/{ip['prefix']}" for ip in ips]
    routes = []
    for ip in ips:
        if not ip.get("gateway"):
            continue
        route = {"to": "0.0.0.0/0" if ip["version"] == 4 else "::/0", "via": ip["gateway"]}
        if _gateway_on_link(ip):
            route["on-link"] = True
        routes.append(route)
    if routes:
        eth["routes"] = routes
    if dns:
        eth["nameservers"] = {"addresses": dns}
    return eth


def networkd_unit(ips: list[dict], dns: list[str]) -> str:
    lines = ["[Match]", "Name=eth0", "", "[Network]"]
    if not ips:
        lines.append("DHCP=yes")
        return "\n".join(lines) + "\n"
    lines.append("IPv6AcceptRA=no")
    for ip in ips:
        lines.append(f"Address={ip['address']}/{ip['prefix']}")
    for server in dns:
        lines.append(f"DNS={server}")
    for ip in ips:
        if ip.get("gateway"):
            lines += ["", "[Route]", f"Gateway={ip['gateway']}"]
            if _gateway_on_link(ip):
                lines.append("GatewayOnLink=yes")
    return "\n".join(lines) + "\n"


def ifupdown(ips: list[dict], dns: list[str]) -> str:
    lines = ["auto lo", "iface lo inet loopback", "", "auto eth0"]
    if not ips:
        lines.append("iface eth0 inet dhcp")
        return "\n".join(lines) + "\n"
    for ip in ips:
        family = "inet" if ip["version"] == 4 else "inet6"
        lines.append(f"iface eth0 {family} static")
        lines.append(f"    address {ip['address']}/{ip['prefix']}")
        if ip.get("gateway"):
            if _gateway_on_link(ip):
                flag = "" if ip["version"] == 4 else "-6 "
                lines.append(f"    post-up ip {flag}route add {ip['gateway']} dev eth0")
                lines.append(f"    post-up ip {flag}route add default via {ip['gateway']} dev eth0")
            else:
                lines.append(f"    gateway {ip['gateway']}")
    if dns:
        lines.append(f"    dns-nameservers {' '.join(dns)}")
    return "\n".join(lines) + "\n"
