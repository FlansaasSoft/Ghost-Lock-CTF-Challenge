#!/usr/bin/env python3
"""Linux-only exercise networking on NEW, deployment-marked machines.

DigitalOcean's NICs/routes are left intact. Fixed unicast VXLAN peers carry the
simulated Ethernet LANs. Only the router bridges peers and routes between LANs.
The analyst's mirror is a separate OVS output port; ingress there is discarded
by OVS's mirror semantics, so it cannot become a second injection path.
"""
from __future__ import annotations
import ipaddress
import json
from pathlib import Path
import subprocess
import sys


def command(*args, check=True):
    result = subprocess.run(list(args), capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result


def validate(config, marker):
    if config["deployment_id"] != marker["deployment_id"] or config["role"] != marker["role"]:
        raise RuntimeError("Wrong deployment marker; refusing network changes.")
    if config["role"] not in {"router", "analyst", "victim", "c2", "goal"}:
        raise RuntimeError("Unknown role")
    for address in [config["underlay_ip"], config["router_ip"]]: ipaddress.IPv4Address(address)
    for role, node in config["nodes"].items():
        if role not in {"analyst", "victim", "c2", "goal"}: raise RuntimeError("Unknown peer")
        for field in ["address", "gateway", "underlay_ip"]: ipaddress.IPv4Address(node[field])
        if node["network"] not in {"mission", "adversary"}: raise RuntimeError("Unknown network")
        if node["vni"] not in {1101, 1102, 2101, 2102}: raise RuntimeError("Unknown tunnel identity")


def ip_address(interface, address):
    command("ip", "address", "replace", address, "dev", interface)
    command("ip", "link", "set", "dev", interface, "mtu", "1400", "up")


def guest_tunnel(name, vni, local, remote):
    present = command("ip", "-j", "link", "show", "dev", name, check=False)
    if present.returncode:
        route = json.loads(command("ip", "-j", "route", "get", remote).stdout)[0]
        command("ip", "link", "add", name, "type", "vxlan", "id", str(vni),
                "local", local, "remote", remote, "dev", route["dev"],
                "dstport", "4789", "nolearning")
    else:
        actual = json.loads(command("ip", "-d", "-j", "link", "show", "dev", name).stdout)[0]["linkinfo"]
        data = actual.get("info_data", {})
        if actual["info_kind"] != "vxlan" or data.get("id") != vni or data.get("remote") != remote or data.get("local") != local:
            raise RuntimeError(f"Existing {name} has a different tunnel identity; inspect instead of replacing it.")
    command("ip", "link", "set", "dev", name, "mtu", "1400", "up")


def configure_guest(config):
    node = config["nodes"][config["role"]]
    guest_tunnel("gl-exercise", node["vni"], config["underlay_ip"], config["router_ip"])
    ip_address("gl-exercise", node["address"] + "/24")
    other = "10.80.1.0/24" if node["network"] == "mission" else "10.60.1.0/24"
    command("ip", "route", "replace", other, "via", node["gateway"], "dev", "gl-exercise")
    if config["role"] == "analyst":
        guest_tunnel("mirror0", 3101, config["underlay_ip"], config["router_ip"])
        command("ip", "link", "set", "dev", "mirror0", "promisc", "on")
        # No L3 address, DHCP, or routing belongs on this receive-only feed.
        command("sysctl", "-w", "net.ipv6.conf.mirror0.disable_ipv6=1")
    command("sysctl", "-w", "net.ipv6.conf.gl-exercise.disable_ipv6=1")


def ovs_tunnel(bridge, port, remote, local, vni):
    command("ovs-vsctl", "--may-exist", "add-port", bridge, port, "--", "set", "Interface", port,
            "type=vxlan", f"options:remote_ip={remote}", f"options:local_ip={local}",
            f"options:key={vni}", "options:dst_port=4789", "mtu_request=1400")


def router_rules():
    """Only this table is managed; never flush another firewall or host route."""
    return '''table inet ghostlock_range {
  chain input {
    type filter hook input priority 0; policy accept;
    iifname { "br-mission", "br-adversary" } jump exercise_input
  }
  chain exercise_input {
    ct state established,related accept
    ip daddr { 10.60.1.1, 10.80.1.1 } udp dport { 53, 123 } accept
    ip daddr { 10.60.1.1, 10.80.1.1 } tcp dport 53 accept
    ip daddr 10.60.1.1 tcp dport 8080 accept
    ip daddr { 10.60.1.1, 10.80.1.1 } ip protocol icmp accept
    counter drop
  }
  chain forward {
    type filter hook forward priority 0; policy drop;
    ct state established,related accept
    iifname "br-mission" oifname "br-adversary" ip saddr 10.60.1.20 ip daddr 10.80.1.10 tcp dport 443 accept
    iifname "br-mission" oifname "br-adversary" ip saddr 10.60.1.10 ip daddr 10.80.1.10 tcp dport { 443, 8443 } accept
    iifname "br-mission" oifname "br-adversary" ip saddr 10.60.1.10 ip daddr 10.80.1.20 tcp dport { 22, 443 } accept
    counter drop
  }
}
'''


def configure_router(config):
    command("systemctl", "start", "openvswitch-switch")
    for bridge, address in [("br-mission", "10.60.1.1/24"), ("br-adversary", "10.80.1.1/24")]:
        command("ovs-vsctl", "--may-exist", "add-br", bridge)
        command("ovs-vsctl", "set", "Bridge", bridge, "fail_mode=standalone")
        command("ovs-vsctl", "set", "Interface", bridge, "mtu_request=1400")
        ip_address(bridge, address)
    for role, node in config["nodes"].items():
        ovs_tunnel("br-" + node["network"], "vx-" + role, node["underlay_ip"], config["underlay_ip"], node["vni"])
    ovs_tunnel("br-mission", "mon-analyst", config["nodes"]["analyst"]["underlay_ip"], config["underlay_ip"], 3101)
    command("ovs-vsctl", "--", "--id=@victim", "get", "Port", "vx-victim",
            "--", "--id=@monitor", "get", "Port", "mon-analyst",
            "--", "--id=@mirror", "create", "Mirror", "name=ghostlock-victim",
            "select-src-port=@victim", "select-dst-port=@victim", "output-port=@monitor",
            "--", "set", "Bridge", "br-mission", "mirrors=@mirror")
    command("sysctl", "-w", "net.ipv4.ip_forward=1")
    Path("/etc/sysctl.d/70-ghostlock-dev.conf").write_text("net.ipv4.ip_forward=1\n")
    rules_path = Path("/etc/ghostlock-dev-router.nft")
    rules_path.write_text(router_rules())
    command("nft", "-c", "-f", str(rules_path))
    command("nft", "delete", "table", "inet", "ghostlock_range", check=False)
    command("nft", "-f", str(rules_path))


def main():
    config = json.loads(Path("/etc/ghostlock-dev-network.json").read_text())
    marker = json.loads(Path("/etc/ghostlock-dev-range.json").read_text())
    validate(config, marker)
    # Defense in depth for link-local cloud metadata. Credentials and tokens are
    # never delivered through user_data, regardless of this local safeguard.
    # Participants with lab root can alter their own host firewall; provider
    # firewalls remain the independent protection for external/legacy systems.
    guard = Path('/etc/ghostlock-dev-metadata.nft')
    guard.write_text('''table inet ghostlock_metadata {
  chain output {
    type filter hook output priority -10; policy accept;
    ip daddr 169.254.169.254 counter drop
  }
}
''')
    command('nft', '-c', '-f', str(guard))
    command('nft', 'delete', 'table', 'inet', 'ghostlock_metadata', check=False)
    command('nft', '-f', str(guard))
    if config["role"] == "router": configure_router(config)
    else: configure_guest(config)
    print("configured", config["role"])


if __name__ == "__main__": main()

