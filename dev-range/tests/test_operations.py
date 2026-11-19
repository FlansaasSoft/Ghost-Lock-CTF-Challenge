"""Mock cloud reads to check that cleanup survives drift without adopting it."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
spec = importlib.util.spec_from_file_location("range_operations", Path(__file__).resolve().parents[1] / "scripts/range.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

DEPLOYMENT = "012345abcdef"
PREFIX = "gl-dev-" + DEPLOYMENT


class TeardownTests(unittest.TestCase):
    def scope(self, actual_nodes):
        values = {"id": "123", "name": PREFIX+"-analyst", "created_at": "today", "vpc_uuid": "new-vpc"}
        resources = [
            {"address": 'digitalocean_droplet.node["analyst"]', "mode": "managed", "type": "digitalocean_droplet", "values": values},
            {"address": "digitalocean_vpc.range", "mode": "managed", "type": "digitalocean_vpc", "values": {"id": "new-vpc", "name": PREFIX+"-vpc"}},
        ]
        owned = {item["address"]: {"id": item["values"]["id"], "type": item["type"]} for item in resources}
        current = {"droplet": actual_nodes, "firewall": [], "project": []}
        def read(path):
            return {"resources": owned} if Path(path).name == "ownership.json" else {"droplet": ["616707276"]}
        with patch.object(ops, "config", return_value={"deployment_id": DEPLOYMENT}), \
             patch.object(ops, "read_json", side_effect=read), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(ops, "state_resources", return_value=resources), \
             patch.object(ops, "inventory", return_value=current):
            return ops.validate_teardown_scope()

    def test_missing_owned_vm_does_not_block_cleanup(self):
        self.assertEqual(len(self.scope([])), 2)

    def test_unowned_vpc_occupant_blocks_cleanup(self):
        with self.assertRaises(ops.SafetyError):
            self.scope([{"id": 999, "vpc_uuid": "new-vpc", "tags": []}])

    def test_matching_tag_does_not_grant_ownership(self):
        with self.assertRaises(ops.SafetyError):
            self.scope([{"id": 999, "vpc_uuid": "other-vpc", "tags": [PREFIX]}])

    def test_teardown_does_not_require_active_account_or_available_sizes(self):
        with patch.object(ops, "config", return_value={"account_uuid": "right"}), \
             patch.object(ops, "doctl", return_value={"uuid": "right", "status": "locked"}) as cloud:
            ops.preflight(destroying=True)
            cloud.assert_called_once_with("account", "get")

    def test_teardown_still_requires_account_identity(self):
        with patch.object(ops, "config", return_value={"account_uuid": "right"}), \
             patch.object(ops, "doctl", return_value={"uuid": "wrong", "status": "active"}):
            with self.assertRaises(ops.SafetyError): ops.preflight(destroying=True)


if __name__ == "__main__": unittest.main()
