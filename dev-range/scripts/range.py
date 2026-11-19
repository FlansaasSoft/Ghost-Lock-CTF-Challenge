#!/usr/bin/env python3
"""Operate ONE isolated development deployment from Windows, Linux, or macOS.

Examples: range.py init; range.py plan; range.py up; range.py configure;
          range.py harden; range.py verify; range.py destroy

The DO token is read from the selected doctl context into memory and passed only
to Terraform. It is never sent to the range or written to a variables/state file.
Every apply consumes a saved, policy-checked plan. There is no import command.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen

from policy import ALLOWED_TYPES, ROLES, SafetyError, validate_plan, validate_live_droplet

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
TFDIR = ROOT / "terraform"
CONFIG = RUNTIME / "config.json"
OWNER = RUNTIME / "ownership.json"
KNOWN = RUNTIME / "known_hosts"
KEY = RUNTIME / "automation_ed25519"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def executable(name):
    # Windows' bundled 9.5 ssh-keyscan advertises a PQ key exchange it cannot
    # execute. Git's maintained OpenSSH tools interoperate with current Kali.
    if os.name == "nt" and name in {"ssh", "ssh-keyscan", "ssh-keygen"}:
        git_ssh = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin" / (name + ".exe")
        if git_ssh.exists(): return str(git_ssh)
    if name == "terraform":
        local = ROOT / ".tools" / ("terraform.exe" if os.name == "nt" else "terraform")
        if local.exists(): return str(local)
    path = shutil.which(name)
    if not path: raise SafetyError(f"Required executable not found: {name}")
    return path


def run(args, *, capture=False, data=None, env=None, timeout=120):
    result = subprocess.run([str(arg) for arg in args], input=data, capture_output=capture,
                            env=env, timeout=timeout, cwd=ROOT)
    if result.returncode:
        # Provider/SSH diagnostics are useful, but raw secret-bearing stdin is
        # deliberately never included in errors or command previews.
        error = (result.stderr or b"").decode("utf-8", errors="replace")[-1600:] if capture else ""
        raise SafetyError(f"Command failed ({result.returncode}): {Path(str(args[0])).name} {error}")
    return result.stdout if capture else b""


def doctl(*args):
    return json.loads(run([executable("doctl"), *args, "--output", "json"], capture=True, timeout=90))


def token_environment():
    environment = os.environ.copy()
    token = environment.get("DIGITALOCEAN_TOKEN")
    if not token:
        token = run([executable("doctl"), "auth", "token", "--show"], capture=True).decode().strip()
    if not token or "*" in token or any(char.isspace() for char in token):
        raise SafetyError("A usable DigitalOcean token is required; token values are never displayed.")
    environment["DIGITALOCEAN_TOKEN"] = token
    environment["TF_IN_AUTOMATION"] = "1"
    environment.pop("TF_LOG", None)
    environment.pop("TF_LOG_PATH", None)
    return environment


def terraform(*args, capture=False):
    return run([executable("terraform"), f"-chdir={TFDIR}", *args], capture=capture,
               env=token_environment(), timeout=1800)


def config():
    if not CONFIG.exists(): raise SafetyError("Run init first; there is no implicit initialization.")
    return read_json(CONFIG)


@contextlib.contextmanager
def operation_lock():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / "operation.lock"
    try: descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SafetyError("Another operation owns operation.lock. Inspect its PID before removing a stale lock.") from None
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def inventory():
    commands = {
        "droplet": ("compute", "droplet", "list"), "vpc": ("vpcs", "list"),
        "firewall": ("compute", "firewall", "list"), "project": ("projects", "list"),
        "ssh_key": ("compute", "ssh-key", "list"), "tag": ("compute", "tag", "list"),
        "image": ("compute", "image", "list-user"),
    }
    return {kind: doctl(*arguments) for kind, arguments in commands.items()}


def initialize(args):
    if CONFIG.exists(): raise SafetyError("This deployment already exists. Preserve its state and use status.")
    baseline = read_json(ROOT / "protected-resources.json")
    account = doctl("account", "get")
    if isinstance(account, list): account = account[0]
    if account["uuid"] != baseline["account_uuid"]:
        raise SafetyError("Wrong DigitalOcean account; inspect the protected inventory before proceeding.")
    current = inventory()
    for kind, rows in current.items():
        field = "name" if kind == "tag" else "id"
        baseline[kind] = sorted(set(baseline.get(kind, [])) | {str(row[field]) for row in rows})
    for network in current["vpc"]:
        if ipaddress.ip_network(args.cidr).overlaps(ipaddress.ip_network(network["ip_range"])):
            raise SafetyError(f"Requested underlay overlaps existing VPC {network['name']}.")
    if len(current["droplet"]) + 5 > account["droplet_limit"]:
        raise SafetyError("Insufficient quota for exactly five new Droplets.")
    public_ip = args.admin_ip or urlopen("https://api.ipify.org", timeout=15).read().decode().strip()
    address = ipaddress.IPv4Address(public_ip)
    if not address.is_global: raise SafetyError("Use a globally routable operator IPv4 address.")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if KEY.exists(): raise SafetyError("Unexpected deployment private key exists; inspect it before initializing.")
    run([executable("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", "ghostlock-dev-automation", "-f", KEY])
    value = {
        "deployment_id": secrets.token_hex(6), "account_uuid": account["uuid"],
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "region": "sfo3",
        "underlay_cidr": args.cidr, "admin_cidrs": [str(address) + "/32"],
        "kali_image_id": 253485598, "bootstrap_egress": True, "max_hourly_usd": 0.12,
        "gateway_approved": False,
    }
    write_json(RUNTIME / "protected-inventory.json", baseline)
    write_json(RUNTIME / "preexisting-details.json", current)
    write_json(CONFIG, value)
    write_json(RUNTIME / "secrets.json", {
        "analyst_password": secrets.token_urlsafe(24), "victim_password": secrets.token_urlsafe(24),
        "goal_password": secrets.token_urlsafe(24), "gateway_password": secrets.token_urlsafe(24),
    })
    write_vars()
    print(f"Initialized deployment {value['deployment_id']}; protected inventory recorded. No cloud changes.")


def write_vars():
    value = config()
    variables = {key: value[key] for key in ("deployment_id", "region", "admin_cidrs", "kali_image_id", "underlay_cidr", "bootstrap_egress")}
    variables["ssh_public_key"] = KEY.with_suffix(".pub").read_text().strip()
    write_json(RUNTIME / "variables.tfvars.json", variables)


def preflight(*, destroying=False):
    value = config()
    account = doctl("account", "get")
    if isinstance(account, list): account = account[0]
    if account["uuid"] != value["account_uuid"]:
        raise SafetyError("Account identity mismatch.")
    # Capacity/pricing must never prevent disposal of an owned deployment.
    if destroying:
        print("Teardown account identity verified; no capacity or price requirement.")
        return
    if account["status"] != "active":
        raise SafetyError("Account identity/status mismatch.")
    sizes = doctl("compute", "size", "list")
    price = 0
    for slug, quantity in {"s-2vcpu-4gb": 2, "s-1vcpu-2gb": 1, "s-1vcpu-1gb": 2}.items():
        size = next(item for item in sizes if item["slug"] == slug)
        if not size["available"] or "sfo3" not in size["regions"]: raise SafetyError("A selected size is unavailable.")
        price += size["price_hourly"] * quantity
    if price > value["max_hourly_usd"]: raise SafetyError("Hourly size estimate exceeds the configured ceiling.")
    print(f"Read preflight passed; estimated new compute ${price:.5f}/hour before tax/transfer.")


def init_backend():
    terraform("init", "-input=false", "-no-color", "-reconfigure", f"-backend-config=path={(RUNTIME / 'terraform.tfstate').as_posix()}", capture=True)


def state_resources():
    shown = json.loads(terraform("show", "-json", capture=True))
    return shown.get("values", {}).get("root_module", {}).get("resources", [])


def record_ownership():
    """Also runs after a failed apply, preserving ownership of partial creations."""
    value = config()
    protected = read_json(RUNTIME / "protected-inventory.json")
    entries = {}
    for item in state_resources():
        if item["mode"] != "managed": continue
        kind = item["type"]
        if kind not in ALLOWED_TYPES: raise SafetyError("Unexpected resource type in state.")
        fields = item["values"]
        identifier = str(fields["id"])
        if identifier in protected.get(ALLOWED_TYPES[kind], []): raise SafetyError("Protected resource detected in state.")
        if not fields["name"].startswith("gl-dev-" + value["deployment_id"]): raise SafetyError("Unexpected name in state.")
        entries[item["address"]] = {"type": kind, "id": identifier, "name": fields["name"]}
    write_json(OWNER, {"deployment_id": value["deployment_id"], "resources": entries})
    if entries:
        outputs = json.loads(terraform("output", "-json", capture=True))
        if outputs.get("deployment", {}).get("value"):
            write_json(RUNTIME / "deployment.json", outputs["deployment"]["value"])


def guarded_plan(*, destroying=False, replace_analyst=False):
    preflight(destroying=destroying)
    write_vars()
    init_backend()
    destination = RUNTIME / ("destroy.tfplan" if destroying else "deploy.tfplan")
    arguments = ["plan", "-input=false", "-no-color", f"-var-file={RUNTIME / 'variables.tfvars.json'}", f"-out={destination}"]
    if destroying: arguments.append("-destroy")
    replacement = 'digitalocean_droplet.node["analyst"]'
    if replace_analyst: arguments.append('-replace='+replacement)
    terraform(*arguments, capture=True)
    plan = json.loads(terraform("show", "-json", destination, capture=True))
    protected = read_json(RUNTIME / "protected-inventory.json")
    owned = read_json(OWNER)["resources"] if OWNER.exists() else {}
    changes = validate_plan(plan, protected, owned, config()["deployment_id"], destroying=destroying,
                            allowed_replacements={replacement} if replace_analyst else ())
    write_json(RUNTIME / ("destroy-plan.json" if destroying else "deploy-plan.json"), plan)
    print(json.dumps({"validated_changes": changes}, indent=2))
    return destination


def validate_live():
    value, deployment = config(), read_json(RUNTIME / "deployment.json")
    protected = read_json(RUNTIME / "protected-inventory.json")
    droplets = doctl("compute", "droplet", "list")
    by_id = {str(item["id"]): item for item in droplets}
    for role, node in deployment["nodes"].items():
        if node["id"] in protected["droplet"]: raise SafetyError("Protected Droplet target.")
        if node["id"] not in by_id: raise SafetyError("A managed Droplet is missing; inspect drift before continuing.")
        validate_live_droplet(role, by_id[node["id"]], node, value["deployment_id"], deployment["vpc_id"])
    expected = {node["id"] for node in deployment["nodes"].values()}
    if {str(item["id"]) for item in droplets if item.get("vpc_uuid") == deployment["vpc_id"]} != expected:
        raise SafetyError("Unowned Droplet attached to the range VPC; teardown/configuration blocked.")
    for firewall in doctl("compute", "firewall", "list"):
        if firewall["id"] in {node["firewall_id"] for node in deployment["nodes"].values()}:
            if firewall.get("tags") or not {str(item) for item in firewall["droplet_ids"]}.issubset(expected):
                raise SafetyError("Range firewall now affects unowned resources.")
    project_resources = doctl("projects", "resources", "list", deployment["project_id"])
    if not {item["urn"] for item in project_resources}.issubset({"do:droplet:" + item for item in expected}):
        raise SafetyError("Range project contains unowned resources; teardown blocked.")
    return deployment


def validate_teardown_scope():
    """Check live attachments against exact state IDs, allowing partial creation.

    Outputs can be stale after a failed apply. State/ownership identify what may
    be removed; absent owned nodes do not prevent cleanup of surviving nodes.
    """
    value = config()
    protected = read_json(RUNTIME / "protected-inventory.json")
    owned = read_json(OWNER)["resources"] if OWNER.exists() else {}
    resources = [item for item in state_resources() if item["mode"] == "managed"]
    synthetic = {"resource_changes": [
        {"address": item["address"], "type": item["type"], "mode": "managed",
         "change": {"actions": ["delete"], "before": item["values"], "after": None}}
        for item in resources]}
    validate_plan(synthetic, protected, owned, value["deployment_id"], destroying=True)
    by_kind = {}
    for item in resources:
        by_kind.setdefault(ALLOWED_TYPES[item["type"]], {})[str(item["values"]["id"])] = item["values"]
    current = inventory()
    node_ids = set(by_kind.get("droplet", {}))
    prefix = "gl-dev-" + value["deployment_id"]
    for node in current["droplet"]:
        identifier = str(node["id"])
        if identifier in node_ids:
            recorded = by_kind["droplet"][identifier]
            role = recorded["name"].removeprefix(prefix + "-")
            validate_live_droplet(role, node, recorded, value["deployment_id"], recorded["vpc_uuid"])
        elif node.get("vpc_uuid") in by_kind.get("vpc", {}) or prefix in node.get("tags", []):
            raise SafetyError("Unowned Droplet uses the range VPC/tag; teardown blocked.")
    for firewall in current["firewall"]:
        if firewall["id"] in by_kind.get("firewall", {}):
            if firewall.get("tags") or not {str(item) for item in firewall["droplet_ids"]}.issubset(node_ids):
                raise SafetyError("Range firewall affects unowned resources; teardown blocked.")
    project_ids = {item["id"] for item in current["project"]}
    for identifier in by_kind.get("project", {}):
        if identifier in project_ids:
            members = doctl("projects", "resources", "list", identifier)
            if not {item["urn"] for item in members}.issubset({"do:droplet:" + item for item in node_ids}):
                raise SafetyError("Range project contains unowned resources; teardown blocked.")
    return [{"kind": ALLOWED_TYPES[item["type"]], "id": str(item["values"]["id"])} for item in resources]


def assert_hardened(deployment):
    """Check the applied cloud perimeter, not just the local requested setting."""
    from policy import validate_runtime_firewalls
    firewalls = doctl("compute", "firewall", "list")
    validate_runtime_firewalls(deployment, firewalls, config()["admin_cidrs"])


def confirm_cloud_removal(expected):
    current = inventory()
    remaining = []
    for resource in expected:
        key = "name" if resource["kind"] == "tag" else "id"
        if resource["id"] in {str(item[key]) for item in current[resource["kind"]]}:
            remaining.append(resource)
    prefix = "gl-dev-" + config()["deployment_id"]
    orphans = [{"kind": kind, "name": item.get("name")} for kind, rows in current.items()
               for item in rows if item.get("name", "").startswith(prefix)]
    report = {"removed": not remaining and not orphans, "owned_resources_checked": len(expected),
              "remaining_ids": remaining, "namespace_resources_remaining": orphans}
    write_json(RUNTIME / "teardown-verification.json", report)
    if not report["removed"]:
        raise SafetyError("Cloud resources remain; inspect teardown-verification.json. No tag-based cleanup is permitted.")


def apply_plan(destroying=False, replace_analyst=False):
    if destroying and (RUNTIME / "gateway-records.json").exists():
        raise SafetyError("Remove the explicitly registered dev gateway records/peering first, then record their removal.")
    removal_scope = validate_teardown_scope() if destroying else None
    if destroying:
        scope_file = RUNTIME / "teardown-scope.json"
        previous = read_json(scope_file) if scope_file.exists() else []
        removal_scope = list({(item["kind"], item["id"]): item for item in [*previous, *removal_scope]}.values())
        write_json(scope_file, removal_scope)
    if not destroying and (RUNTIME / "deployment.json").exists():
        live = validate_live()
        if replace_analyst: remember_pins(live)
    saved = guarded_plan(destroying=destroying, replace_analyst=replace_analyst)
    try: terraform("apply", "-input=false", "-no-color", saved)
    finally: record_ownership()
    if destroying:
        if read_json(OWNER)["resources"]: raise SafetyError("Resources remain in state after teardown.")
        confirm_cloud_removal(removal_scope)
        active = RUNTIME / 'deployment.json'
        if active.exists():
            write_json(RUNTIME / 'last-deployment.json', read_json(active))
            active.unlink()
        if KNOWN.exists():
            (RUNTIME / 'last-known_hosts').write_bytes(KNOWN.read_bytes())
            KNOWN.unlink()
        (RUNTIME / "teardown-scope.json").replace(RUNTIME / "last-teardown-scope.json")
        # A later fresh up needs downloads for cloud-init/package installation.
        # This is only a local next-deployment default; no cloud rule is reopened.
        value = config(); value["bootstrap_egress"] = True
        write_json(CONFIG, value)
        write_vars()
        print("Teardown complete: empty state and cloud absence verified. Runtime evidence/keys retained locally.")


def ssh(node, command, *, data=None, capture=False, timeout=120):
    arguments = [executable("ssh"), "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN}",
                 "-o", "ConnectTimeout=10", "root@" + node["public_ip"], command]
    return run(arguments, data=data, capture=capture, timeout=timeout)


def remember_pins(deployment):
    path = RUNTIME/'host-pins.json'
    if path.exists(): return read_json(path)
    pins = {}
    if KNOWN.exists():
        for role, node in deployment['nodes'].items():
            rows = [line.split() for line in KNOWN.read_text().splitlines() if line and not line.startswith('#')]
            matches = [row for row in rows if node['public_ip'] in row[0].split(',')]
            if len(matches) != 1: raise SafetyError('Cannot bind the recorded host pin to its deployment ID.')
            pins[role] = {'droplet_id': node['id'], 'key_type': matches[0][1], 'key_data': matches[0][2]}
    write_json(path, pins)
    return pins


def pin_new_hosts(deployment):
    """First-contact trust is allowed ONLY for these new, API-verified identities.

    Later SSH connections are strict. We never accept or replace a legacy host
    pin. Public/private addresses are aliases for the same freshly created host.
    """
    pins = remember_pins(deployment)
    lines = []
    exercise_aliases = {"analyst": "10.60.1.10", "victim": "10.60.1.20", "c2": "10.80.1.10", "goal": "10.80.1.20", "router": "10.60.1.1"}
    for role, node in deployment["nodes"].items():
        previous = pins.get(role)
        if previous and previous['droplet_id'] == node['id']:
            lines.append(f"{node['public_ip']},{node['private_ip']},{exercise_aliases[role]} {previous['key_type']} {previous['key_data']}")
            continue
        deadline = time.monotonic() + 600
        while True:
            result = subprocess.run([executable("ssh-keyscan"), "-T", "5", "-t", "ed25519", node["public_ip"]], capture_output=True)
            keys = [line for line in result.stdout.decode().splitlines() if line and not line.startswith("#")]
            if keys: break
            if time.monotonic() >= deadline: raise SafetyError(f"SSH boot timeout: {role}")
            time.sleep(5)
        for line in keys:
            _, key_type, key_data = line.split()
            lines.append(f"{node['public_ip']},{node['private_ip']},{exercise_aliases[role]} {key_type} {key_data}")
            pins[role] = {'droplet_id': node['id'], 'key_type': key_type, 'key_data': key_data}
    KNOWN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(RUNTIME/'host-pins.json', pins)
    for role, node in deployment["nodes"].items():
        marker = json.loads(ssh(node, "cat /etc/ghostlock-dev-range.json", capture=True))
        if marker.get("deployment_id") != config()["deployment_id"] or marker.get("role") != role:
            raise SafetyError("Remote deployment marker mismatch.")


def make_inventory(deployment):
    value = config()
    hosts = {}
    groups = {"routers": "router", "analysts": "analyst", "victims": "victim", "c2_servers": "c2", "goals": "goal"}
    for role, node in deployment["nodes"].items():
        hosts[role] = {"ansible_host": node["private_ip"], "underlay_ip": node["private_ip"],
                       "range_role": role, "droplet_id": node["id"]}
        if role == "router": hosts[role]["ansible_connection"] = "local"
    result = {"all": {"vars": {"deployment_id": value["deployment_id"], "ansible_user": "root",
                               "ansible_ssh_private_key_file": "/root/ghostlock-dev/automation_ed25519",
                               "router_underlay_ip": deployment["nodes"]["router"]["private_ip"]},
                      "hosts": hosts, "children": {group: {"hosts": {role: {}}} for group, role in groups.items()}}}
    return result


def configure_range():
    deployment = validate_live()
    pin_new_hosts(deployment)
    router = deployment["nodes"]["router"]
    print("Preparing Ansible on the NEW router; no existing gateway/relay is targeted.")
    ssh(router, "cloud-init status --wait && apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv && python3 -m venv /opt/ghostlock-ansible && /opt/ghostlock-ansible/bin/pip install --disable-pip-version-check ansible-core==2.19.7 passlib==1.7.4", timeout=900)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for source in (ROOT / "ansible").rglob("*"):
            if source.is_file() and source.name not in {"secrets.json", "inventory.json"}:
                archive.add(source, arcname=str(source.relative_to(ROOT / "ansible")))
        for source, name in [(KEY, "automation_ed25519"), (KNOWN, "known_hosts"), (RUNTIME / "secrets.json", "secrets.json")]:
            archive.add(source, arcname=name)
        raw = json.dumps(make_inventory(deployment)).encode()
        entry = tarfile.TarInfo("inventory.json"); entry.size = len(raw); entry.mode = 0o600
        archive.addfile(entry, io.BytesIO(raw))
    ssh(router, "umask 077; mkdir -p /root/ghostlock-dev; tar -xzf - -C /root/ghostlock-dev; chmod 600 /root/ghostlock-dev/automation_ed25519 /root/ghostlock-dev/secrets.json", data=payload.getvalue())
    # Keep a private controller log so an interrupted local console does not
    # discard the cause of a failed/unfinished provisioning run.
    ssh(router, "set -o pipefail; cd /root/ghostlock-dev && /opt/ghostlock-ansible/bin/ansible-playbook site.yml --syntax-check && /opt/ghostlock-ansible/bin/ansible-playbook site.yml 2>&1 | tee /root/ghostlock-dev/configure.log", timeout=3600)
    print("Ansible configuration completed. Run harden, then verify, before opening gateway access.")


def verify_range():
    deployment = validate_live()
    if config()["bootstrap_egress"]: raise SafetyError("Run harden first; verification requires runtime egress restrictions.")
    assert_hardened(deployment)
    # Verification evolves independently of provisioning; sync only these
    # checked-in, non-secret files to the already verified new controller.
    for source, target in [(ROOT/"ansible/verify.yml", "/root/ghostlock-dev/verify.yml"),
                           (ROOT/"ansible/files/verify_range.py", "/root/ghostlock-dev/files/verify_range.py")]:
        ssh(deployment["nodes"]["router"], "cat > " + target, data=source.read_bytes())
    # Stream checks into a controller log even when a failed check exits nonzero.
    try:
        ssh(deployment["nodes"]["router"], "set -o pipefail; cd /root/ghostlock-dev && /opt/ghostlock-ansible/bin/ansible-playbook verify.yml 2>&1 | tee /root/ghostlock-dev/verification.log", timeout=600)
    finally:
        result = ssh(deployment["nodes"]["router"], "cat /root/ghostlock-dev/verification.log", capture=True)
        (RUNTIME / "verification.log").write_bytes(result)


def gateway_plan():
    """Export a private review worksheet, NOT a request to modify Guacamole."""
    deployment = validate_live()
    if config()['bootstrap_egress']: raise SafetyError('Harden the perimeter before preparing gateway access.')
    assert_hardened(deployment)
    fingerprint = ssh(deployment['nodes']['router'], 'cat /root/ghostlock-dev/analyst-rdp-fingerprint.txt', capture=True).decode().strip()
    passwords = read_json(RUNTIME/'secrets.json')
    profile = {'name': 'Ghost Lock Dev '+config()['deployment_id'], 'protocol': 'rdp',
               'parameters': {'hostname': deployment['nodes']['analyst']['public_ip'], 'port': '3389',
                              'username': 'analyst', 'password': passwords['analyst_password'],
                              'security': 'tls', 'ignore-cert': 'false', 'cert-fingerprints': fingerprint,
                              'enable-drive': 'false', 'disable-audio': 'true', 'resize-method': 'display-update'},
               'attributes': {'max-connections': '1', 'max-connections-per-user': '1'},
               'notice': 'Private review worksheet. Gateway changes require explicit approval. This is not a native Guacamole import format.'}
    write_json(RUNTIME/'gateway-profile.json', profile)
    print(json.dumps({'name': profile['name'], 'hostname': profile['parameters']['hostname'],
                      'rdp_tls_fingerprint': fingerprint, 'passwords': 'kept in private runtime files',
                      'gateway_changed': False}, indent=2))


def collect_evidence():
    """Retain small, private test records before destroying the new machines.

    Never download the executable implant, operator credentials, or DO token.
    The packet capture may contain exercise traffic and stays in ignored runtime.
    """
    deployment = validate_live()
    evidence = RUNTIME / "evidence"
    evidence.mkdir(exist_ok=True)
    files = [
        ("router", "/root/ghostlock-dev/configure.log", "configure.log"),
        ("router", "/root/ghostlock-dev/verification.log", "verification.log"),
        ("router", "/root/ghostlock-dev/verification-results.json", "verification-results.json"),
        ("router", "/root/ghostlock-dev/analyst-rdp-fingerprint.txt", "analyst-rdp-fingerprint.txt"),
        ("analyst", "/root/ghostlock-mirror-proof.pcap", "victim-mtls-mirror.pcap"),
        ("c2", "/var/lib/ghostlock-sliver/prototype-implant.json", "prototype-implant.json"),
    ]
    for role, remote_path, filename in files:
        (evidence / filename).write_bytes(ssh(deployment["nodes"][role], "cat " + remote_path, capture=True))
    print(f"Saved {len(files)} private evidence files in {evidence}; no implant binary downloaded.")


def audit_existing():
    """Compare stable configuration fields, excluding provider timestamps/counters."""
    before = read_json(RUNTIME/'preexisting-details.json')
    after = inventory()
    fields = {
        'droplet': ['name','status','size_slug','vpc_uuid','networks','tags','volume_ids'],
        'vpc': ['name','ip_range','region','default'],
        'firewall': ['name','inbound_rules','outbound_rules','droplet_ids','tags'],
        'project': ['name','description','purpose','environment','is_default'],
        'ssh_key': ['name','fingerprint','public_key'],
        'image': ['name','type','distribution','regions','min_disk_size','resource_id'],
        'tag': ['name'],
    }
    problems, checked = [], 0
    for kind, original in before.items():
        key = 'name' if kind == 'tag' else 'id'
        current = {str(item[key]): item for item in after[kind]}
        for item in original:
            identifier = str(item[key]); checked += 1
            if identifier not in current:
                problems.append({'kind': kind, 'id': identifier, 'issue': 'missing'})
                continue
            changed = [field for field in fields[kind] if item.get(field) != current[identifier].get(field)]
            if changed: problems.append({'kind': kind, 'id': identifier, 'changed_fields': changed})
    result = {'preexisting_resources_checked': checked, 'unchanged': not problems, 'differences': problems}
    write_json(RUNTIME/'protection-audit.json', result)
    print(json.dumps(result, indent=2))
    if problems: raise SafetyError('Pre-existing configuration differs; inspect the recorded audit.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initial = commands.add_parser("init")
    initial.add_argument("--admin-ip"); initial.add_argument("--cidr", default="10.160.0.0/24")
    for name in ["plan", "up", "configure", "bootstrap", "harden", "verify", "status", "destroy", "gateway-plan", "collect-evidence", "audit-existing", "plan-replace-analyst"]: commands.add_parser(name)
    replacement = commands.add_parser('replace-analyst')
    replacement.add_argument('--expected-id', required=True, help='Exact NEW analyst ID reviewed by the operator; protected IDs remain forbidden.')
    args = parser.parse_args()
    with operation_lock():
        if args.command == "init": initialize(args)
        elif args.command == "plan": guarded_plan()
        elif args.command == "up": apply_plan()
        elif args.command == "plan-replace-analyst": guarded_plan(replace_analyst=True)
        elif args.command == "replace-analyst":
            if validate_live()['nodes']['analyst']['id'] != args.expected_id:
                raise SafetyError('Analyst identity changed since repair approval.')
            apply_plan(replace_analyst=True)
        elif args.command == "configure": configure_range()
        elif args.command in {"harden", "bootstrap"}:
            value = config(); value["bootstrap_egress"] = args.command == "bootstrap"; write_json(CONFIG, value); apply_plan()
        elif args.command == "verify": verify_range()
        elif args.command == "destroy": apply_plan(destroying=True)
        elif args.command == "gateway-plan": gateway_plan()
        elif args.command == "collect-evidence": collect_evidence()
        elif args.command == "audit-existing": audit_existing()
        elif args.command == "status": print(json.dumps(validate_live(), indent=2))


if __name__ == "__main__":
    try: main()
    except (SafetyError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        sys.exit(1)

