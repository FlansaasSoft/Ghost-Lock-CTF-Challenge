#!/usr/bin/env python3
"""Bounded live checks; output contains results, never passwords or private keys.

The controller is the NEW range router. VM administration uses pinned host keys.
Negative tests demonstrate blocked paths rather than relying on a diagram.
"""
from __future__ import annotations
import concurrent.futures
import json
from pathlib import Path
import shlex
import subprocess
import time
import paramiko

ROOT = Path('/root/ghostlock-dev')
INVENTORY = json.loads((ROOT/'inventory.json').read_text())
HOSTS = INVENTORY['all']['hosts']
SECRETS = json.loads((ROOT/'secrets.json').read_text())
RESULTS = []


def remote(role, command, timeout=45):
    arguments = ['ssh', '-i', str(ROOT/'automation_ed25519'), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                 '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile='+str(ROOT/'known_hosts'),
                 '-o', 'ConnectTimeout=8', 'root@'+HOSTS[role]['underlay_ip'], command]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if result.returncode: raise RuntimeError(f'{role} verification command failed: {result.stderr[-400:]}')
    return result.stdout


def record(name, condition, detail=None):
    RESULTS.append({'check': name, 'passed': bool(condition), **({'detail': detail} if detail else {})})


def probe(role, address, port):
    code = ('import socket; s=socket.socket(); s.settimeout(2); '
            f'r=s.connect_ex(({address!r},{port})); print(r==0); s.close()')
    return remote(role, 'python3 -c '+shlex.quote(code)).strip() == 'True'


def password_login(address, username, password):
    # SSH -W makes the target connection originate from Kali. Authentication is
    # performed here through that encrypted stream; no goal password or private
    # orchestration key is copied into a participant VM.
    proxy_args = ['ssh', '-i', str(ROOT/'automation_ed25519'), '-o', 'BatchMode=yes',
                  '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                  '-o', 'UserKnownHostsFile='+str(ROOT/'known_hosts'), '-o', 'ConnectTimeout=8',
                  '-W', address+':22', 'root@'+HOSTS['analyst']['underlay_ip']]
    proxy = paramiko.ProxyCommand(shlex.join(proxy_args))
    client = paramiko.SSHClient()
    client.load_host_keys(str(ROOT/'known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(address, username=username, password=password, look_for_keys=False, allow_agent=False,
                       sock=proxy, timeout=8, auth_timeout=8, banner_timeout=8)
        _, stdout, _ = client.exec_command('id -un', timeout=5)
        return stdout.read().decode().strip() == username
    except paramiko.AuthenticationException:
        return False
    finally:
        client.close()
        proxy.close()


def main():
    deployment_id = INVENTORY['all']['vars']['deployment_id']
    for role in ['analyst', 'victim', 'c2', 'goal']:
        marker = json.loads(remote(role, 'cat /etc/ghostlock-dev-range.json'))
        record(role+' identity', marker['deployment_id'] == deployment_id and marker['role'] == role)
    paths = [
        ('analyst', '10.60.1.20', 22, True, 'analyst to victim SSH'),
        ('victim', '10.80.1.10', 443, True, 'victim to C2 mTLS'),
        ('analyst', '10.80.1.20', 22, True, 'analyst directly to goal SSH'),
        ('analyst', '10.80.1.10', 22, False, 'C2 administration blocked from analyst'),
        ('analyst', '10.80.1.10', 31337, False, 'Sliver operator API blocked from analyst'),
        ('victim', '10.80.1.20', 22, False, 'victim cannot skip to goal'),
        ('analyst', '10.160.0.1', 22, False, 'management underlay unreachable through exercise routing'),
        ('analyst', '24.199.98.94', 22, False, 'existing gateway SSH blocked'),
        ('analyst', '10.124.0.4', 3389, False, 'existing Ubuntu pilot blocked'),
        ('victim', '169.254.169.254', 80, False, 'cloud metadata blocked at runtime'),
        ('analyst', '1.1.1.1', 443, False, 'analyst Internet egress blocked'),
        ('c2', '1.1.1.1', 443, False, 'C2 Internet egress blocked'),
    ]
    def check_path(item):
        role, address, port, expected, label = item
        return label, probe(role, address, port) == expected
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for label, passed in pool.map(check_path, paths): record(label, passed)
    record('provided victim credentials work', password_login('10.60.1.20', 'investigator', SECRETS['victim_password']))
    record('goal rejects incorrect credentials', not password_login('10.80.1.20', 'mission', 'incorrect-ghostlock-test-password'))
    record('goal accepts its actual credentials', password_login('10.80.1.20', 'mission', SECRETS['goal_password']))
    record('implant is an inspectable Linux ELF', 'ELF' in remote('victim', 'file /var/lib/mission/.cache/telemetry/mission-agent'))
    record('implant service running', remote('victim', 'systemctl is-active mission-agent').strip() == 'active')
    record('xrdp service running', remote('analyst', 'systemctl is-active xrdp').strip() == 'active')
    record('xrdp redirected-drive mount disabled', 'EnableFuseMount=false' in remote('analyst', 'grep "^EnableFuseMount=" /etc/xrdp/sesman.ini'))
    capture_command = "timeout 40 tcpdump -U -n -i mirror0 'host 10.60.1.20 and host 10.80.1.10 and tcp port 443' -c 6 -w /root/ghostlock-mirror-proof.pcap 2>/dev/null"
    try:
        remote('analyst', capture_command, timeout=50)
        packets = remote('analyst', 'tcpdump -nn -r /root/ghostlock-mirror-proof.pcap 2>/dev/null')
        record('live victim C2 packets visible on Kali mirror', '10.60.1.20' in packets and '10.80.1.10.443' in packets)
    except RuntimeError:
        record('live victim C2 packets visible on Kali mirror', False)
    # Query the actual Sliver registry, not just an open TCP socket.
    # SourceFileLoader is used because the installed helper has no .py suffix.
    list_code = "from importlib.machinery import SourceFileLoader; from importlib.util import spec_from_loader,module_from_spec; loader=SourceFileLoader('s','/usr/local/sbin/ghostlock-sliver-setup'); spec=spec_from_loader('s',loader); m=module_from_spec(spec); loader.exec_module(m); print(m.console('beacons'))"
    beacons = remote('c2', 'python3 -c '+shlex.quote(list_code))
    # Sliver canonicalizes the requested implant name to lowercase.
    record('real Sliver beacon registered from victim',
           'mission_telemetry' in beacons.lower() and 'gl-dev-'+deployment_id+'-victim' in beacons
           and '10.60.1.20:' in beacons and 'svc-mission' in beacons)
    listeners = remote('c2', "ss -lntp '( sport = :31337 or sport = :443 )'")
    record('operator API loopback-only', '127.0.0.1:31337' in listeners and '0.0.0.0:31337' not in listeners)
    record('implant listener exercise-only', '10.80.1.10:443' in listeners and '0.0.0.0:443' not in listeners)
    mirror = subprocess.run(['ovs-vsctl', 'list', 'Mirror'], capture_output=True, text=True, check=True).stdout
    record('OVS victim mirror configured', 'ghostlock-victim' in mirror and 'select_src_port' in mirror)
    summary = {'passed': all(item['passed'] for item in RESULTS), 'checks': RESULTS,
               'scope': 'Five new VMs; public gateway browser verification is a separate explicit step.'}
    (ROOT/'verification-results.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary))
    if not summary['passed']: raise SystemExit(1)


if __name__ == '__main__': main()

