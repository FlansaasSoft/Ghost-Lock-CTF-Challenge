"""Destructive-plan tests exercise the boundaries that protect the live pilots."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from policy import SafetyError, validate_plan, validate_live_droplet

DEPLOYMENT = "012345abcdef"
PREFIX = f"gl-dev-{DEPLOYMENT}"
PROTECTED = {"droplet": ["616707276", "616708783"], "vpc": ["old-vpc"], "tag": ["legacy-tag"]}


def change(kind="digitalocean_droplet", actions=None, before=None, after=None):
    return {"address": kind + '.node["analyst"]', "type": kind, "mode": "managed",
            "change": {"actions": actions or ["create"], "before": before,
                       "after": after or {"name": PREFIX + "-analyst"}}}


class ProtectionTests(unittest.TestCase):
    def check(self, item, owned=None, destroying=False):
        return validate_plan({"resource_changes": [item]}, PROTECTED, owned or {}, DEPLOYMENT, destroying=destroying)

    def test_new_range_resource_is_allowed(self):
        self.assertEqual(self.check(change())[0]["actions"], ["create"])

    def test_computed_firewall_ids_are_unknown_until_creation(self):
        item = change(kind="digitalocean_firewall", after={"name": PREFIX + "-analyst", "droplet_ids": None})
        self.assertEqual(len(self.check(item)), 1)

    def test_imported_gateway_cannot_be_destroyed_even_with_forged_ownership(self):
        item = change(actions=["delete"], before={"id": "616707276", "name": PREFIX + "-analyst"})
        owned = {item["address"]: {"id": "616707276", "type": item["type"]}}
        with self.assertRaises(SafetyError): self.check(item, owned, destroying=True)

    def test_existing_relay_update_is_rejected(self):
        item = change(actions=["update"], before={"id": "616708783", "name": PREFIX + "-analyst"})
        with self.assertRaises(SafetyError): self.check(item)

    def test_unowned_state_cannot_be_destroyed(self):
        item = change(actions=["delete"], before={"id": "123", "name": PREFIX + "-analyst"})
        with self.assertRaises(SafetyError): self.check(item, destroying=True)

    def test_owned_new_resource_can_be_destroyed(self):
        item = change(actions=["delete"], before={"id": "123", "name": PREFIX + "-analyst"})
        owned = {item["address"]: {"id": "123", "type": item["type"]}}
        self.assertEqual(len(self.check(item, owned, destroying=True)), 1)

    def test_implicit_replacement_is_rejected(self):
        with self.assertRaises(SafetyError): self.check(change(actions=["delete", "create"]))

    def test_explicit_repair_still_cannot_replace_a_protected_droplet(self):
        item = change(actions=['delete','create'], before={'id':'616707276','name':PREFIX+'-analyst'})
        owned = {item['address']: {'id':'616707276','type':item['type']}}
        with self.assertRaises(SafetyError):
            validate_plan({'resource_changes':[item]}, PROTECTED, owned, DEPLOYMENT, allowed_replacements={item['address']})

    def test_explicit_repair_can_replace_exact_owned_new_analyst(self):
        item = change(actions=['delete','create'], before={'id':'123','name':PREFIX+'-analyst'})
        owned = {item['address']: {'id':'123','type':item['type']}}
        self.assertEqual(len(validate_plan({'resource_changes':[item]}, PROTECTED, owned, DEPLOYMENT, allowed_replacements={item['address']})),1)

    def test_peering_cannot_be_hidden_in_default_plan(self):
        with self.assertRaises(SafetyError): self.check(change(kind="digitalocean_vpc_peering"))

    def test_firewall_cannot_attach_to_gateway(self):
        item = change(kind="digitalocean_firewall", after={"name": PREFIX + "-analyst", "droplet_ids": [616707276]})
        with self.assertRaises(SafetyError): self.check(item)

    def test_tag_based_firewall_is_rejected(self):
        item = change(kind="digitalocean_firewall", after={"name": PREFIX + "-analyst", "tags": ["legacy"]})
        with self.assertRaises(SafetyError): self.check(item)

    def test_wrong_namespace_is_rejected(self):
        with self.assertRaises(SafetyError): self.check(change(after={"name": "ghostlock-gateway-pilot"}))

    def test_noop_on_unowned_resource_still_fails(self):
        item = change(actions=["no-op"], before={"id": "123", "name": PREFIX + "-analyst"})
        with self.assertRaises(SafetyError): self.check(item)

    def test_provider_identity_checks_creation_time(self):
        actual = {"id": 123, "name": PREFIX + "-analyst", "created_at": "new-time", "vpc_uuid": "new-vpc", "tags": [PREFIX]}
        with self.assertRaises(SafetyError):
            validate_live_droplet("analyst", actual, {"id": "123", "created_at": "old-time"}, DEPLOYMENT, "new-vpc")

    def test_read_only_data_reference_is_allowed(self):
        item = {"mode": "data", "type": "digitalocean_droplet", "change": {"actions": ["read"]}}
        self.assertEqual(self.check(item), [])


if __name__ == "__main__": unittest.main()

