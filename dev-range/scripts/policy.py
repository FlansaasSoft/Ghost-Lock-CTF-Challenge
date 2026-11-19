"""Fail-closed checks shared by deployment, teardown, and their regression tests.

Terraform state is necessary but not sufficient ownership evidence: an imported
legacy resource must still be rejected. Names/tags alone never grant ownership.
"""
from __future__ import annotations

ALLOWED_TYPES = {
    "digitalocean_droplet": "droplet",
    "digitalocean_vpc": "vpc",
    "digitalocean_firewall": "firewall",
    "digitalocean_project": "project",
    "digitalocean_ssh_key": "ssh_key",
    "digitalocean_tag": "tag",
}
ROLES = {"analyst", "victim", "c2", "goal", "router"}


class SafetyError(RuntimeError):
    """An operation needs inspection, not an automatic retry or override."""


def validate_plan(plan, protected, owned, deployment_id, *, destroying=False, allowed_replacements=()):
    """Accept only this configuration's resources and proven new identities.

    Gateway integration and VPC peering deliberately cannot pass this policy.
    They are reviewed separately because they affect existing infrastructure.
    """
    prefix = f"gl-dev-{deployment_id}"
    changes = []
    for item in plan.get("resource_changes", []):
        if item.get("mode") == "data":
            continue
        kind = item.get("type")
        if kind not in ALLOWED_TYPES:
            raise SafetyError(f"Unapproved resource type: {kind}")
        change = item["change"]
        actions = change["actions"]
        before, after = change.get("before") or {}, change.get("after") or {}
        resource_id = str(before.get("id", ""))
        protected_ids = {str(value) for value in protected.get(ALLOWED_TYPES[kind], [])}
        if resource_id and resource_id in protected_ids:
            raise SafetyError(f"Protected existing resource in plan: {item['address']}")
        if actions in (["no-op"], ["read"]):
            if resource_id and owned.get(item["address"], {}).get("id") != resource_id:
                raise SafetyError(f"State contains an unowned resource: {item['address']}")
            continue
        if destroying:
            if actions != ["delete"]:
                raise SafetyError("A teardown plan may only delete owned resources.")
        elif actions == ["delete", "create"] and item["address"] in allowed_replacements:
            pass  # Explicit repair of an exact, owned, non-protected address.
        elif actions not in (["create"], ["update"]):
            raise SafetyError("Replacements and deletions require a separate teardown; no implicit rebuilds.")
        if actions != ["create"]:
            entry = owned.get(item["address"], {})
            if entry.get("id") != resource_id or entry.get("type") != kind:
                raise SafetyError(f"Missing exact ownership record: {item['address']}")
        name = after.get("name", before.get("name", ""))
        if not name.startswith(prefix):
            raise SafetyError(f"Resource is outside this deployment namespace: {item['address']}")
        if kind == "digitalocean_tag" and name in protected_ids:
            raise SafetyError("Cannot create/adopt a pre-existing tag.")
        if kind == "digitalocean_firewall":
            if after.get("tags") or before.get("tags"):
                raise SafetyError("Tag-based firewall attachment is forbidden.")
            ids = {str(value) for value in (after.get("droplet_ids") or [])}
            if ids.intersection(str(value) for value in protected.get("droplet", [])):
                raise SafetyError("Firewall would attach to a protected Droplet.")
        changes.append({"address": item["address"], "actions": actions, "id": resource_id or None})
    if not changes and destroying and owned:
        raise SafetyError("No deletion actions found although ownership records remain.")
    return changes


def validate_live_droplet(role, actual, recorded, deployment_id, vpc_id):
    """Bind a target to an ID, original creation time, name, VPC, and tag."""
    expected_name = f"gl-dev-{deployment_id}-{role}"
    if role not in ROLES or str(actual["id"]) != str(recorded["id"]):
        raise SafetyError("Droplet ID/role mismatch.")
    if actual["name"] != expected_name or actual.get("vpc_uuid") != vpc_id:
        raise SafetyError("Droplet name/VPC mismatch.")
    if actual["created_at"] != recorded["created_at"]:
        raise SafetyError("Droplet creation identity changed.")
    if f"gl-dev-{deployment_id}" not in actual.get("tags", []):
        raise SafetyError("Deployment tag is absent.")


def validate_runtime_firewalls(deployment, firewalls, admin_cidrs, relay_ip="165.232.145.224"):
    """Require the exact applied runtime allowlist before checks or gateway use.

    This catches a partially failed harden operation and union rules from an
    unexpected second firewall. Names and a local bootstrap flag are insufficient.
    """
    nodes = deployment["nodes"]
    expected_ids = {node["firewall_id"] for node in nodes.values()}
    node_ids = {str(node["id"]) for node in nodes.values()}
    prefix = "gl-dev-" + deployment["deployment_id"]
    by_id = {item["id"]: item for item in firewalls}

    def normalize(rules, direction):
        result = set()
        for rule in rules:
            selectors = rule.get(direction, {})
            if any(value for key, value in selectors.items() if key != "addresses"):
                raise SafetyError("Runtime firewall uses an unexpected non-address selector.")
            if rule.get("action", "allow") != "allow" or not selectors.get("addresses"):
                raise SafetyError("Unexpected runtime firewall rule.")
            for address in selectors["addresses"]:
                result.add((rule["protocol"], rule["ports"], address))
        return result

    for firewall in firewalls:
        if firewall["id"] not in expected_ids and (
                node_ids.intersection(str(item) for item in firewall.get("droplet_ids", []))
                or prefix in firewall.get("tags", [])):
            raise SafetyError("An additional firewall affects this deployment.")
    router = nodes["router"]["private_ip"] + "/32"
    guests = {node["private_ip"] + "/32" for role, node in nodes.items() if role != "router"}
    for role, node in nodes.items():
        firewall = by_id.get(node["firewall_id"])
        if not firewall or firewall.get("status") != "succeeded" or firewall.get("pending_changes"):
            raise SafetyError("A runtime firewall is missing or not yet applied.")
        if firewall.get("tags") or {str(item) for item in firewall["droplet_ids"]} != {str(node["id"])}:
            raise SafetyError("Runtime firewall attachments differ from the deployment.")
        peers = guests if role == "router" else {router}
        inbound = {("tcp", "22", address) for address in admin_cidrs}
        inbound.update(("udp", "4789", address) for address in peers)
        if role != "router": inbound.add(("tcp", "22", router))
        if role == "analyst": inbound.add(("tcp", "3389", relay_ip + "/32"))
        outbound = {("udp", "4789", address) for address in peers}
        if role == "router": outbound.update(("tcp", "22", address) for address in guests)
        if normalize(firewall["inbound_rules"], "sources") != inbound:
            raise SafetyError(f"{role} inbound firewall differs from the runtime allowlist.")
        if normalize(firewall["outbound_rules"], "destinations") != outbound:
            raise SafetyError(f"{role} egress is not hardened to the runtime allowlist.")

