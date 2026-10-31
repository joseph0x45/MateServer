"""Install-time commands, run by the MSI as SYSTEM: `configure` and `uninstall-cleanup`.

They live here rather than in the installer's own language because they touch the same file format
and the same directory layout the server reads — one implementation, tested with the rest.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .config import parse_env_file

# Well-known SIDs, so the ACLs are right on every Windows display language.
_ADMINS, _SYSTEM, _LOCAL_SERVICE = "*S-1-5-32-544", "*S-1-5-18", "*S-1-5-19"

# What uninstalling removes: everything the server can rebuild by itself. The database, its key,
# the Leapmotor certificates and the configuration stay — that is the user's driving history and
# login, and a reinstall picks them up where they were.
REGENERABLE = ("payload", "venvs", "cache", "tmp", "logs", "server.lock", "stop.request")

_HEADER = [
    "# LeapMotor Mate server configuration - one KEY=VALUE per line, values taken literally.",
    "# Restart the service after editing:  Restart-Service LeapMotorMate",
    "# Port and password can also be changed by running the installer again.",
]


def _set_keys(lines: list[str], values: dict[str, str]) -> list[str]:
    """Replace each KEY=… line in place (first occurrence wins, later duplicates dropped), append
    keys that are not there yet. Comments and every other key are left exactly as they were."""
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in values:
            if key in seen:
                continue
            seen.add(key)
            out.append(f"{key}={values[key]}")
        else:
            out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return out


def write_config(path: Path, *, data_dir: Path, port: int, password: str) -> bool:
    """Create or update mate.env. Returns True when the file was created.

    On an existing file only the port changes, and the password when one was given: an upgrade
    shows the settings page again with the password field empty, and empty means "keep"."""
    if password and (password != password.strip() or any(c in password for c in "\r\n")):
        raise ValueError("the password cannot contain a line break or start/end with a space")
    created = not path.is_file()
    if created:
        lines = _HEADER + [
            "MATE_DATA_DIR=" + str(data_dir),
            "# auto = install new Mate releases by themselves; notify = only log them; off",
            "MATE_UPDATE=auto",
            "MATE_UPDATE_INTERVAL_HOURS=6",
            "# MATE_VERSION_PIN=4.17.0",
            "# Behind a reverse proxy that rewrites Host, name the public origin here:",
            "# MATE_TRUSTED_ORIGINS=https://mate.example.com",
        ]
    else:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    values = {"MATE_PORT": str(port)}
    if password:
        values["MATE_AUTH_PASSWORD"] = password
    lines = _set_keys(lines, values)
    if not password and "MATE_AUTH_PASSWORD" not in parse_env_file("\n".join(lines)):
        lines.append("# MATE_AUTH_PASSWORD=   (none - set one in Mate's Settings before exposing this server)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return created


def _icacls(*args: str) -> None:
    r = subprocess.run(["icacls", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"icacls {' '.join(args[:1])} failed: {(r.stderr or r.stdout).strip()}")


def secure(data_dir: Path, config: Path) -> None:
    """Data: Administrators + SYSTEM full control, the service account modify, nobody else.
    Config (it holds the password): the service account may only read it.

    The service account also gets WRITE_DAC: Mate locks its own private folders (certs/,
    api-v2-private/) to its account alone, and Modify does not allow rewriting a folder's
    permissions. Without it, a folder this installer created (owned by SYSTEM, not the service)
    cannot be locked, Mate refuses to install its application certificate, and the login page
    reports provisioning_required.

    No /T: the root's inheritable entries propagate to every child that inherits, and a folder Mate
    has already locked (protected, readable by the service only) is left alone instead of failing
    the install."""
    if sys.platform != "win32":
        return
    _icacls(str(data_dir), "/inheritance:r", "/grant:r",
            f"{_ADMINS}:(OI)(CI)F", f"{_SYSTEM}:(OI)(CI)F", f"{_LOCAL_SERVICE}:(OI)(CI)(M,WDAC)", "/C", "/Q")
    _icacls(str(config), "/inheritance:r", "/grant:r",
            f"{_ADMINS}:F", f"{_SYSTEM}:F", f"{_LOCAL_SERVICE}:R", "/C", "/Q")


def configure(data_dir: Path, port: int, password: str) -> int:
    if not 1 <= port <= 65535:
        print(f"port out of range: {port}", file=sys.stderr)
        return 2
    # Not certs/: Mate creates and locks that one itself, as the service account.
    for sub in ("", "logs", "tmp"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    config = data_dir / "mate.env"
    created = write_config(config, data_dir=data_dir, port=port, password=password)
    secure(data_dir, config)
    print(("created " if created else "updated ") + str(config))
    return 0


def uninstall_cleanup(data_dir: Path) -> int:
    # Narrow on purpose: only ever the folder the installer created, recognised by name and content.
    if data_dir.name != "LeapMotorMate" or not (data_dir / "mate.env").is_file():
        print(f"refusing to clean {data_dir}: not a LeapMotor Mate data folder")
        return 0
    for name in REGENERABLE:
        target = data_dir / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)
    print(f"removed the downloadable parts of {data_dir}; the database and configuration are kept")
    return 0
