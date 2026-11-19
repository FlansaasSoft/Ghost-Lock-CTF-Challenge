"""Security regressions for the cloud rules actually applied before playtests."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from policy import SafetyError, validate_runtime_firewalls


def fixture():
    nodes = {role: {"id": str(i), "firewall_id": "fw-"+role, "private_ip": "10.160.0."+str(i)}
             for i, role in enumerate(["analyst", "victim", "c2", "goal", "router"], 2)}
    deployment = {"deployment_id": "012345abcdef", "nodes": nodes}
    router = nodes["router"]["private_ip"]+"/32"
    guests = [node["private_ip"]+"/32" for role, node in nodes.items() if role != "router"]
    def rule(protocol, port, direction, addresses):
        return {"protocol": protocol, "ports": port, direction: {"addresses": addresses}}
    firewalls = []
    for role, node in nodes.items():
        peers = guests if role == "router" else [router]
        inbound = [rule("tcp", "22", "sources", ["203.0.113.1/32"]), rule("udp", "4789", "sources", peers)]
        outbound = [rule("udp", "4789", "destinations", peers)]
        if role != "router": inbound.append(rule("tcp", "22", "sources", [router]))
        else: outbound.append(rule("tcp", "22", "destinations", guests))
        if role == "analyst": inbound.append(rule("tcp", "3389", "sources", ["165.232.145.224/32"]))
        firewalls.append({"id": node["firewall_id"], "status": "succeeded", "pending_changes": [],
                          "droplet_ids": [int(node["id"])], "tags": [],
                          "inbound_rules": inbound, "outbound_rules": outbound})
    return deployment, firewalls


class RuntimePerimeterTests(unittest.TestCase):
    def check(self, deployment, firewalls):
        validate_runtime_firewalls(deployment, firewalls, ["203.0.113.1/32"])

    def test_applied_relay_only_runtime_perimeter_passes(self):
        self.check(*fixture())

    def test_partial_hardening_with_internet_egress_is_rejected(self):
        deployment, firewalls = fixture()
        firewalls[2]["outbound_rules"].append({"protocol": "tcp", "ports": "443", "destinations": {"addresses": ["0.0.0.0/0"]}})
        with self.assertRaises(SafetyError): self.check(deployment, firewalls)

    def test_pending_firewall_change_is_not_treated_as_applied(self):
        deployment, firewalls = fixture()
        firewalls[0]["pending_changes"] = [{"droplet_id": 2, "status": "waiting"}]
        with self.assertRaises(SafetyError): self.check(deployment, firewalls)

    def test_public_rdp_is_rejected(self):
        deployment, firewalls = fixture()
        firewalls[0]["inbound_rules"][-1]["sources"]["addresses"] = ["0.0.0.0/0"]
        with self.assertRaises(SafetyError): self.check(deployment, firewalls)

    def test_second_firewall_cannot_expand_access(self):
        deployment, firewalls = fixture()
        other = copy.deepcopy(firewalls[0]); other["id"] = "unexpected"
        firewalls.append(other)
        with self.assertRaises(SafetyError): self.check(deployment, firewalls)

    def test_firewall_cannot_include_an_unowned_droplet(self):
        deployment, firewalls = fixture()
        firewalls[0]["droplet_ids"].append(616707276)
        with self.assertRaises(SafetyError): self.check(deployment, firewalls)


if __name__ == "__main__": unittest.main()
