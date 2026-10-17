#!/usr/bin/env python3
"""Add a bounded host swap safety net; read-only unless --apply is supplied.

Does not resize/swapoff existing swap, tune other services, or change OOM scores.
Keep this alongside the tenant memory/swap budgets. Swap absorbs temporary peaks,
not sustained capacity shortfalls. A partially completed run is safe to rerun.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

GIB = 1024**3
TARGET = Path("/swapfile-auto-browser")
SIZE = 4 * GIB
MIN_FREE_AFTER = 8 * GIB
FSTAB = Path("/etc/fstab")
BACKUP = Path("/etc/fstab.bak-auto-browser-20261017")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    active = {line.split()[0] for line in Path("/proc/swaps").read_text().splitlines()[1:]}
    entries = [line.split() for line in FSTAB.read_text().splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    configured = any(row[0] == str(TARGET) and len(row) > 2 and row[2] == "swap" for row in entries)
    exists = TARGET.exists() or TARGET.is_symlink()
    if exists:
        info = TARGET.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != SIZE or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("Refusing unexpected existing swap target")
        # Never reformat an existing file, including a partially applied run.
        signature = subprocess.check_output(["blkid", "-p", "-s", "TYPE", "-o", "value", str(TARGET)], text=True).strip()
        if signature != "swap":
            raise SystemExit("Existing target is not initialized swap; inspect it manually")
    if configured and not exists:
        raise SystemExit("Configured swap target is missing; inspect fstab before continuing")
    free = shutil.disk_usage("/").free
    needed = 0 if exists else SIZE
    if free - needed < MIN_FREE_AFTER:
        raise SystemExit("Insufficient disk headroom: at least 8 GiB must remain free")
    if not exists:
        filesystem = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "--target", "/"], text=True).strip()
        if filesystem != "ext4":
            raise SystemExit("Only the reviewed ext4 allocation path is supported")
    print(json.dumps({"target": str(TARGET), "size_gib": 4, "active": str(TARGET) in active,
                      "configured": configured, "disk_free_after_gib": round((free - needed) / GIB, 1),
                      "apply": args.apply}), flush=True)
    if not args.apply:
        return
    if os.geteuid() != 0:
        raise SystemExit("Root is required for --apply")
    if not configured and not BACKUP.exists():
        # Preserve the exact original configuration before making a change.
        with BACKUP.open("xb") as out:
            out.write(FSTAB.read_bytes())
            out.flush()
            os.fsync(out.fileno())
        os.chmod(BACKUP, 0o600)
    if not exists:
        fd = os.open(TARGET, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.posix_fallocate(fd, 0, SIZE)
            os.fsync(fd)
        finally:
            os.close(fd)
        subprocess.run(["mkswap", str(TARGET)], check=True, stdout=subprocess.DEVNULL)
    if str(TARGET) not in active:
        subprocess.run(["swapon", str(TARGET)], check=True)
    now_active = {line.split()[0] for line in Path("/proc/swaps").read_text().splitlines()[1:]}
    if str(TARGET) not in now_active:
        raise SystemExit("Swap activation could not be verified; fstab was not changed")
    if not configured:
        with FSTAB.open("a") as out:
            out.write(f"\n{TARGET} none swap sw 0 0\n")
            out.flush()
            os.fsync(out.fileno())
    print("Browser host swap active and persisted; original swap unchanged.")


if __name__ == "__main__":
    main()
