"""mate-server command line.

    python -m mate_server serve        [--config FILE] [--seed-from CHECKOUT]
    python -m mate_server status       [--config FILE]
    python -m mate_server check-update [--config FILE]
    python -m mate_server update       [--config FILE]     (service must be stopped)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import SERVER_VERSION, envs, lock, updater
from .config import ConfigError, load
from .supervisor import STOP_FILE, Server, log, serve


def _default_config() -> str | None:
    """The config file the installers write, when it exists — so `mate-server status` works
    without remembering where it lives."""
    env = os.environ.get("MATE_CONFIG")
    if env:
        return env
    candidates = ([Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "LeapMotorMate" / "mate.env"]
                  if sys.platform == "win32" else [Path("/etc/leapmotor-mate/mate.env")])
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def cmd_status(cfg) -> int:
    print(f"mate-server    {SERVER_VERSION}")
    print(f"config         {cfg.source or '(none — defaults and environment)'}")
    print(f"data dir       {cfg.data_dir}")
    print(f"port           {cfg.port}")
    print(f"updates        {cfg.update}" + (f", pinned to {cfg.version_pin}" if cfg.version_pin else
                                           f", every {cfg.update_interval_hours:g} h"))
    print(f"Mate           {updater.payload_version(cfg.current) or 'not installed'}")
    if cfg.previous.exists():
        print(f"rollback       {updater.payload_version(cfg.previous) or '?'}")
    print(f"password       {'set in config' if cfg.passthrough.get('MATE_AUTH_PASSWORD') else 'not set in config'}")
    running = not lock.acquire(cfg.data_dir)
    if not running:
        lock.release()
    print(f"service        {'running' if running else 'not running'}")
    return 0


def cmd_check_update(cfg) -> int:
    have = updater.payload_version(cfg.current)
    rel = updater.latest_release()
    if not rel:
        print(f"could not reach GitHub: {updater.last_error}")
        return 1
    newer = not have or updater.release_order(rel["version"]) > updater.release_order(have)
    print(f"installed {have or 'nothing'}, latest {rel['version']}"
          + (" — update available" if newer else " — up to date"))
    return 0


def cmd_update(cfg) -> int:
    if not lock.acquire(cfg.data_dir):
        print("the service is running — it installs updates by itself; stop it first to update by hand")
        return 1
    try:
        server = Server(cfg)
        if not server.stage_update(force=True):
            return 0 if updater.payload_version(cfg.current) else 1
        server.apply_staged()
        envs.ensure_env(cfg.current, cfg.venvs, python=cfg.python, uv=cfg.uv,
                        cache_dir=cfg.data_dir / "cache", log=log)
        print(f"installed Mate {updater.payload_version(cfg.current)}")
        return 0
    finally:
        lock.release()


def cmd_stop(cfg, timeout: float = 40.0) -> int:
    """Ask a running server to stop and wait until it has. This is what the Windows service runs on
    'Stop'; the wrapper then waits for the server process itself to exit."""
    import time
    if lock.acquire(cfg.data_dir):
        lock.release()
        print("not running")
        return 0
    (cfg.data_dir / STOP_FILE).write_text("stop", encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if lock.acquire(cfg.data_dir):
            lock.release()
            print("stopped")
            return 0
    print(f"still running after {timeout:.0f}s")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mate-server", description="LeapMotor Mate server")
    parser.add_argument("--version", action="version", version=f"mate-server {SERVER_VERSION}")
    sub = parser.add_subparsers(dest="command")
    for name, help_ in (("serve", "run Mate (what the service runs)"),
                        ("status", "show what is installed and how it is configured"),
                        ("check-update", "ask GitHub whether a newer Mate exists"),
                        ("update", "install the newest Mate now (service stopped)"),
                        ("stop", "ask a running server to stop, and wait until it has")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--config", default=None, help="KEY=VALUE config file")
        if name == "serve":
            p.add_argument("--seed-from", type=Path, default=None,
                           help="install Mate from a local leapmotor-mate checkout instead of GitHub")
    # Installer-only commands (the MSI runs them as SYSTEM).
    p = sub.add_parser("configure", help="write mate.env and secure the data folder (installer)")
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--password", default="")
    p = sub.add_parser("uninstall-cleanup", help="remove downloaded Mate and environments (installer)")
    p.add_argument("--data-dir", type=Path, required=True)

    # WinSW 2.x appends the service's start arguments to <stoparguments> (WrapperService.DoStop:
    # `stopArguments += " " + config.Arguments`), so `stop` arrives with a whole `serve …` command
    # line behind it. Tolerate that for `stop` alone; everything else stays strict.
    args, extra = parser.parse_known_args(argv)
    if extra and args.command != "stop":
        parser.error("unrecognized arguments: " + " ".join(extra))
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "configure":
        from .setup_cmds import configure
        return configure(args.data_dir, args.port, args.password)
    if args.command == "uninstall-cleanup":
        from .setup_cmds import uninstall_cleanup
        return uninstall_cleanup(args.data_dir)
    try:
        cfg = load(args.config or _default_config())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.command == "serve":
        return serve(cfg, seed_from=args.seed_from)
    return {"status": cmd_status, "check-update": cmd_check_update, "update": cmd_update,
            "stop": cmd_stop}[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
