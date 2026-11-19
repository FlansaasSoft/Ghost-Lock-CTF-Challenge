# Ghost Lock Linux development range

This prototype creates four challenge VMs and one trusted router/controller on
DigitalOcean. Terraform owns only newly created resources. Ansible installs the
Linux exercise, a Kali desktop, a pinned Sliver server and a readable victim
implant. The public gateway and existing relay are outside this Terraform state.

## Resource and trust boundaries

| New VM | Purpose | Simulated address | Initial size |
| --- | --- | --- | --- |
| analyst | Kali XFCE, Wireshark and analysis tools; staff RDP desktop | 10.60.1.10 | 2 vCPU, 4 GB |
| victim | Linux mission server; provided investigator SSH account; Sliver beacon | 10.60.1.20 | 1 vCPU, 2 GB |
| c2 | Sliver 1.7.9; mTLS callback on exercise TCP 443 | 10.80.1.10 | 2 vCPU, 4 GB |
| goal | Linux SSH target and protected mission account, ready for later challenge content | 10.80.1.20 | 1 vCPU, 1 GB |
| router | Open vSwitch, routing ACLs, victim traffic mirror, simulated DNS/time/status, Ansible controller | .1 on both LANs | 1 vCPU, 1 GB |

The new VPC uses `10.160.0.0/24` as an administration/tunnel underlay. Unicast
VXLAN peers build the simulated LANs. The analyst and victim share the mission
LAN. C2 and goal share the adversary LAN. Each guest has a distinct tunnel key;
the router assigns membership. The analyst's `mirror0` receives copies of victim
traffic, with no IP address and no permitted ingress through the OVS mirror port.

The router allows victim-to-C2 TCP 443, analyst-to-C2 TCP 443/8443 and
analyst-to-goal TCP 22/443. Other inter-LAN forwarding is denied. Same-LAN
analyst/victim access is unrestricted. The Sliver operator API binds only to
127.0.0.1:31337. The implant listener binds only to 10.80.1.10:443. No implant or
C2 operator interface is exposed to the public Internet.
The implant uses mTLS over TCP 443, which provides an observable TLS connection
rather than an HTTP application protocol.

The existing relay reaches the new analyst's public address using RDP over TLS.
The new firewall permits that RDP connection ONLY from relay 165.232.145.224/32.
This prototype uses no VPC peering, gateway host configuration, relay route
change, or modification of existing pilot VMs. Gateway registration is an
explicitly approved separate step, described below.

## Prerequisites and local tools

- Python 3.11+, doctl authenticated to the intended DigitalOcean account, and OpenSSH.
- Terraform 1.17.2 and DigitalOcean provider 2.107.0 are pinned. On Windows run
  `./Install-Tools.ps1 -Python <path-to-python.exe>` to install tools locally.
  On Linux/macOS install that Terraform version on PATH instead.
- WSL, Docker and local Ansible are not required. Ansible 2.19.7 runs in an
  isolated Python environment on the NEW router; it never targets the old hosts.
- The existing clean Kali custom image 253485598 is used read-only. Existing
  pilot snapshots and their per-host identities/credentials are not cloned.
- On Windows, the scripts prefer Git's OpenSSH tools when available. The system
  OpenSSH 9.5 host-key scanner can fail negotiation against current Linux servers.
- The Kali bootstrap masks NetworkManager and marks cloud interfaces unmanaged.
  A lean XFCE package set is used instead of the broad desktop metapackage so
  desktop installation cannot replace the provider's network configuration.
- The initial compute estimate observed on November 12, 2026 was $0.10714/hour,
  approximately $72/month continuously, before tax/transfer. `preflight` checks
  current size rates against a $0.12/hour ceiling. This is not a provider billing
  cap or an automatic expiry system. Destroy idle deployments.

## Deployment

Run commands from this directory, using the same Python executable each time:

```text
python scripts/range.py init
python scripts/range.py plan
python scripts/range.py up
python scripts/range.py configure
python scripts/range.py harden
python scripts/range.py verify
python scripts/range.py gateway-plan
```

`init` generates a dedicated local SSH key and random deployment identifier,
records a protected cloud inventory, detects the operator's public IPv4 /32,
and creates ignored runtime inputs. It performs no cloud writes. Use
`init --admin-ip <IPv4> --cidr <unused-/24>` to choose those inputs explicitly.
Protect `.runtime/`; it contains private keys, passwords, Terraform state,
gateway details and test evidence. Never publish it or copy it into a VM image.

`plan` and `up` validate saved Terraform plans before applying them. Existing IDs
are rejected even if imported or given a matching name. Updates/deletions require
exact ownership records. Resource replacement is refused; use an explicit
teardown/recreate cycle. Failed applies preserve ownership of partial creations
that reached Terraform state. Ambiguous unrecorded provider operations require
manual reconciliation; never delete resources based only on a matching tag.
There is one explicit repair path: review `plan-replace-analyst`, then run
`replace-analyst --expected-id <reviewed-new-analyst-id>` only after approval.
It can replace that exact owned analyst, never a protected pilot. Host pins are
bound to Droplet IDs: unchanged IDs keep their pins, and an explicitly replaced
new instance receives a fresh first-contact pin.

`configure` verifies API identities, pins the NEW hosts on first contact, checks
deployment markers, and copies Ansible plus deployment-only keys to the new
router. All later SSH uses strict pins. This first-contact trust applies only to
fresh deployment-owned hosts, never the existing gateway/relay. Cloud API tokens
stay on the operator machine and are never sent to the router or challenge VMs.

`harden` removes the bootstrap DNS/HTTP(S) download rules from the new cloud
firewalls. Runtime permits only the owned overlay, controller SSH and the
relay-originated analyst RDP path. Run `bootstrap` before a later package upgrade,
then `configure`, `harden` and `verify` again. Do not open a playtest with bootstrap
Internet egress enabled. Both `verify` and `gateway-plan` inspect the applied
provider rules and attachments; a local setting alone cannot pass this check.

## Public gateway integration requires explicit approval

The infrastructure scripts do not log into or modify the existing Guacamole
deployment. After configuration, prepare/review a NEW connection and optional
temporary playtest user in Guacamole. Do not edit the current Kali connection.
`gateway-plan` writes `.runtime/gateway-profile.json`, a private review worksheet
containing the exact addresses, certificate pin and connection settings. It is
not a native Guacamole import file and does not modify the gateway.

- Name: `Ghost Lock Dev <deployment_id>`.
- Protocol: RDP. Hostname: NEW analyst public IP from `.runtime/deployment.json`.
- Port: 3389; username: `analyst`; password: private `analyst_password` from
  `.runtime/secrets.json`.
- Security: TLS; certificate validation enabled; pin the SHA256 fingerprint
  recorded on the new router at `/root/ghostlock-dev/analyst-rdp-fingerprint.txt`.
- Disable drive redirection and audio. The desktop is shared deliberately only
  if staff explicitly configure connection sharing; the initial profile is a
  single analyst session.
- For a staff-only check, an existing administrator can open the new connection.
  An optional temporary `gl-dev-<deployment_id>` Guacamole user receives READ only
  on this new connection. Its password is `gateway_password` in the private
  secrets file. Creating that user requires separate approval.
- Record NEW connection/user IDs in `.runtime/gateway-records.json` when added.
  While this file exists, infrastructure teardown refuses to proceed until the
  operator removes those exact new records and confirms their cleanup.

In this development session the operator requested browser-based Guacamole
administration. A maintainer with a trusted internal API connection can automate
the same creation later. No production Guacamole database credentials or host
keys are included in this project.

## Minimal staff playtest

1. Log into the public gateway with the approved staff or temporary dev user and open its analyst
   desktop. Confirm a real desktop renders and accepts input.
2. On Kali, capture `mirror0` with Wireshark or `sudo tcpdump -ni mirror0`.
   Compare periodic mission DNS/HTTP traffic with the victim's encrypted C2 link.
3. SSH from Kali to `investigator@10.60.1.20`, using the provided victim password.
   `sudo` is available for investigation. Correlate network sockets with processes
   and inspect `/var/lib/mission/.cache/telemetry/mission-agent` and
   `mission-agent.service`.
4. Confirm the Sliver C2 has a real registered victim beacon. The private C2
   operator profile is `/var/lib/ghostlock-sliver/operator.cfg`; it is not issued
   to players. A staff administrator can use the pinned Sliver client locally
   on C2. This is separate from recovered implant authentication.
5. Goal SSH is directly reachable from Kali, rejects incorrect credentials, and
   accepts its separate mission password. Staff credentials are supplied for
   prototype validation; a later challenge stage must provide the intended
   credential-recovery or vulnerability path.

The implant is generated with `--skip-symbols --debug`, without packing, evasion,
or a shellcode encoder. It is a Linux ELF running as `svc-mission`, in a plausible
hidden cache directory with readable permissions and an ordinary inspectable
systemd service. It is deliberately discoverable. This is training software;
keep it in this isolated range.

This prototype does not yet implement scored C2 collection, goal exploitation,
credential-rotation stages, per-team flags, roster-to-range scheduling, or live
round admission/cutoff. Those are extension points, not claimed working features.

## Verification and teardown

```text
python -m unittest discover -s tests -v
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
python scripts/range.py verify
python scripts/range.py collect-evidence
python scripts/range.py destroy
python scripts/range.py audit-existing
```

The template tests require `requirements-dev.txt`; on Windows the installer puts
them in `.tools/checks`. Ansible syntax checking also runs on the Linux controller
before configuration. Live checks cover marker identity, allowed/blocked network
paths, real Sliver registration, mirrored packets, xrdp health, and victim/goal
authentication. A public browser session is recorded separately; an open RDP port
does not prove that the gateway works.

`collect-evidence` saves private provisioning/verification logs, results, the RDP
certificate fingerprint, a mirrored packet capture and the implant manifest in
`.runtime/evidence/`. It does not download the implant executable or C2 operator
credentials. Run it after verification and before teardown.

Before destroy, close test sessions and remove only the NEW Guacamole connection,
its permissions and temporary dev user. Remove `gateway-records.json` only after
their absence has been verified. `destroy` validates cloud identity, live
attachments and a saved delete-only plan, then deletes only this deployment's
owned resources. Cleanup permits missing owned VMs after a partial deployment;
unowned occupants of the new VPC/project block it. Size availability and pricing
never block teardown. Empty state and provider-side absence are both checked. It leaves
local evidence and keys for review. It does not delete existing images, snapshots,
VPCs, projects, keys, firewalls, or pilot Droplets.
After teardown, `audit-existing` compares stable fields of the original resource
inventory against current provider results. Retained `last-deployment.json` and
`last-known_hosts` are evidence; a new `up` can create a fresh deployment with
fresh resource IDs after the previous deployment has been completely removed.
Successful teardown resets the local bootstrap setting for that next deployment;
it does not reopen any cloud firewall. Run `harden` again before the next playtest.

If a provider deletion times out, inspect current resources and re-run the guarded
`destroy`. It can refresh already-missing owned nodes and finish the remaining
owned resources. This recovery was exercised during the live test; do not remove
state entries manually to make a failed teardown appear complete.

If a command fails, preserve `.runtime/` and inspect state and provider results.
Do not remove an operation lock without checking its PID. Do not use raw
`terraform destroy`, `terraform import`, `doctl ... delete`, or tag-based cleanup.
Losing local state is not authorization to guess which resources to delete.

## Source map and extension notes

- `terraform/`: new cloud resources, bootstrap marker and runtime perimeter.
- `scripts/range.py`: preflight, ownership, saved-plan policy, transport, lifecycle.
- `scripts/policy.py`: protection checks, independent of Terraform execution.
- `ansible/site.yml`: explicit role setup; no hidden import-time cloud operations.
- `ansible/group_vars/all.yml`: address/tunnel inventory and pinned Sliver digests.
- `ansible/files/range_network.py`: switching, mirror and explicit routing ACLs.
- `ansible/files/sliver_setup.py`: local-only C2 setup and readable beacon build.
- `ansible/files/verify_range.py`: bounded live verification and private results.
- `tests/`: protection and configuration regression tests.

The initial logical address plan and allowed service ports are intentionally
fixed. When changing them, update group variables, the router ACLs and live probe
expectations together. Add new challenge services on exercise interfaces only.
Keep orchestration, scoring secrets and cloud credentials outside participant
VMs. Never reuse a populated participant snapshot as a fleet golden image.

