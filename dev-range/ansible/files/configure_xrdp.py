#!/usr/bin/env python3
"""Keep xrdp TLS pinned and prevent the old disconnected FUSE-drive problem."""
from pathlib import Path
import re

def replace(path, values):
    original = text = Path(path).read_text()
    for name, value in values.items():
        text, count = re.subn(r"^#?" + re.escape(name) + r"\s*=.*$", name + "=" + value, text, flags=re.M)
        if count != 1: raise RuntimeError(f"Unexpected xrdp setting count for {name}: {count}")
    if text != original:
        Path(path).write_text(text)
        print("changed", path)

if __name__ == "__main__":
    replace("/etc/xrdp/xrdp.ini", {"security_layer": "tls", "crypt_level": "high",
                                  "certificate": "/etc/xrdp/ghostlock.crt", "key_file": "/etc/xrdp/ghostlock.key"})
    replace("/etc/xrdp/sesman.ini", {"EnableFuseMount": "false"})

