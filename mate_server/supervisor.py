"""Run Mate as a long-lived service and keep it current.

This is run.sh's job (and MateDesktop's launcher's), done for a server:

  * start the poller and the web server as children, and treat them as one unit — when either
    exits, both are stopped;
  * honour Mate's relaunch code (42: demo toggle, account switch, restore) by restarting them;
  * check GitHub for new releases on startup and then every few hours, prepare the new version
    (download + its Python environment) WHILE the old one keeps serving, then swap with a few
    seconds of downtime — and roll back by itself if the new version does not come up;
  * exit non-zero on a real crash, and let systemd / the Windows service manager restart us.
"""
from __future__ import annotations

import glob
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import SERVER_VERSION, envs, lock, updater
from .winjob import KillOnCloseJob
from .config import Config

RELAUNCH_CODE = 42        # the payload asking to be restarted (same contract as run.sh)
STARTUP_GRACE_S = 60      # how long a payload gets to answer HTTP before it is called broken
_APPLY_UPDATE = object()  # sentinel returned by watch(): a staged update is ready to go in
_IS_WINDOWS = sys.platform == "win32"
STOP_FILE = "stop.request"   # `mate-server stop` creates it; the service manager runs that on stop


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [mate-server] {msg}"
    try:
        print(line, flush=True)
    except Exception:                                          # noqa: BLE001 — never kill the app
        pass


class Server:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.quit = threading.Event()
        self.update_ready = threading.Event()
        self.pending: dict | None = None
        self.newest_seen = ""          # last version announced in 'notify' mode — announce once
        self.procs: list[subprocess.Popen] = []
        self.env_python: str | None = None   # interpreter of the current payload's environment
        self.job = KillOnCloseJob(log=log)   # Windows: children die with us, however we die

    # ── environment for the children ──────────────────────────────────────────────────────

    def child_env(self, part: str, demo_db: Path | None = None) -> dict:
        cfg = self.cfg
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MATE_DESKTOP") and k not in ("VIRTUAL_ENV", "PYTHONHOME")}
        env.update(cfg.passthrough)
        env.update({
            "PYTHONPATH": str(cfg.current / part),
            "DB_PATH": str(demo_db or cfg.db_path),
            # Mate installs its own application certificate here at startup (since 4.7.7).
            "DATA_CERT_DIR": str(cfg.data_dir / "certs"),
            "CERT_DIR": str(cfg.data_dir / "certs"),
            "WEB_PORT": str(cfg.port),
            # The per-login account certificate is written with tempfile; keep it on persistent
            # storage, or every restart forces a re-login (run.sh does the same with /data/tmp).
            "TMPDIR": str(cfg.data_dir / "tmp"),
            "PYTHONUNBUFFERED": "1",
            # Mate logs arrows and symbols; Windows encodes streams and files in the ANSI code page
            # unless told otherwise (MateDesktop: both variables were needed, not just one).
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "MATE_SERVER": "1",
            "MATE_SERVER_VERSION": SERVER_VERSION,
        })
        if demo_db:
            env["MATE_DEMO"] = "1"
        cafile = self._certifi_bundle()
        if cafile and "SSL_CERT_FILE" not in cfg.passthrough:
            env["SSL_CERT_FILE"] = cafile
        return env

    def _certifi_bundle(self) -> str | None:
        py = self.env_python
        if not py:
            return None
        env_dir = Path(py).parent.parent
        pattern = ("Lib/site-packages/certifi/cacert.pem" if _IS_WINDOWS
                   else "lib/python*/site-packages/certifi/cacert.pem")
        hits = glob.glob(str(env_dir / pattern))
        return hits[0] if hits else None

    # ── payload ───────────────────────────────────────────────────────────────────────────

    def ensure_payload(self, seed_from: Path | None = None) -> bool:
        """Make sure SOME Mate is installed. On a first start that means downloading one, and a
        server with no network yet (booting before the link is up) waits rather than dying."""
        cfg = self.cfg
        if seed_from:
            log(f"installing Mate from local source {seed_from}")
            staged = cfg.staged
            import shutil
            shutil.rmtree(staged, ignore_errors=True)
            updater.copy_payload(seed_from, staged)
            broken = updater.payload_looks_complete(staged)
            if broken:
                shutil.rmtree(staged, ignore_errors=True)
                log(f"{seed_from} is not a leapmotor-mate checkout ({broken})")
                return False
            updater.install_payload(staged, cfg.current, cfg.previous, log=log)
            return True
        if updater.payload_version(cfg.current):
            return True
        delay = 15
        while not self.quit.is_set():
            log("no Mate installed yet — downloading the current release")
            rel = self.stage_update()
            if rel:
                updater.install_payload(cfg.staged, cfg.current, cfg.previous, log=log)
                self.pending = None
                self.update_ready.clear()
                return True
            log(f"could not install Mate ({updater.last_error or 'see above'}); retrying in {delay}s")
            self.quit.wait(delay)
            delay = min(delay * 2, 600)
        return False

    def target_release(self) -> dict | None:
        pin = self.cfg.version_pin
        if pin:
            for tag in (pin, f"v{pin.lstrip('vV')}", pin.lstrip("vV")):
                rel = updater.release_for_tag(tag)
                if rel:
                    return rel
            return None
        return updater.latest_release()

    def stage_update(self, force: bool = False) -> dict | None:
        """If a newer Mate exists, download it into staged/ and build its environment, while the
        running version carries on. Returns the release when it is ready to install."""
        cfg = self.cfg
        have = updater.payload_version(cfg.current)
        rel = self.target_release()
        if not rel:
            log(f"update check failed: {updater.last_error or 'no such release'}")
            return None
        if have:
            if cfg.version_pin:
                if rel["version"] == have:
                    return None
            elif updater.release_order(rel["version"]) <= updater.release_order(have):
                log(f"Mate {have} is up to date")
                return None
        if have and cfg.update == "notify" and not cfg.version_pin and not force:
            if rel["version"] != self.newest_seen:
                self.newest_seen = rel["version"]
                log(f"Mate {rel['version']} is available (running {have}); MATE_UPDATE=notify, "
                    f"so it is not installed automatically — run 'mate-server update' to install it")
            return None

        log(f"preparing Mate {rel['version']}" + (f" (running {have})" if have else ""))
        import shutil
        shutil.rmtree(cfg.staged, ignore_errors=True)
        try:
            updater.fetch_payload(rel["tag"], cfg.staged, log=log)
            broken = updater.payload_looks_complete(cfg.staged)
            if broken:
                raise RuntimeError(f"download is not a usable Mate ({broken})")
            envs.ensure_env(cfg.staged, cfg.venvs, python=cfg.python, uv=cfg.uv,
                            cache_dir=cfg.data_dir / "cache", log=log)
        except Exception as exc:                               # noqa: BLE001 — never fatal
            log(f"update to {rel['version']} abandoned, staying on {have}: {exc}")
            shutil.rmtree(cfg.staged, ignore_errors=True)
            return None
        self.pending = rel
        self.update_ready.set()
        return rel

    def _update_loop(self) -> None:
        interval = self.cfg.update_interval_hours * 3600
        while not self.quit.wait(interval):
            if self.pending:
                continue                          # one waiting to go in already
            try:
                self.stage_update()
                newer = updater.newer_server(SERVER_VERSION)
                if newer:
                    log(f"a newer mate-server is available ({SERVER_VERSION} → {newer}); "
                        f"re-run the installer to upgrade the shell")
            except Exception as exc:                           # noqa: BLE001
                log(f"update check crashed: {type(exc).__name__}: {exc}")

    # ── processes ─────────────────────────────────────────────────────────────────────────

    def demo_requested(self) -> bool:
        if self.cfg.passthrough.get("MATE_DEMO", "") not in ("", "0", "false", "False"):
            return True
        return (self.cfg.data_dir / "demo.flag").exists()

    def _cmd(self, script: Path) -> list[str]:
        return [str(self.env_python), str(script)]

    def seed_demo(self) -> Path:
        demo_db = self.cfg.data_dir / "demo.db"
        env = self.child_env("poller", demo_db=demo_db)
        r = subprocess.run(self._cmd(self.cfg.current / "poller" / "seed_demo.py"), env=env,
                           cwd=str(self.cfg.current / "poller"), capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            log(f"demo seeding failed: {(r.stderr or r.stdout or '').strip()[-400:]}")
        else:
            log("demo mode — sample data ready")
        return demo_db

    def spawn(self) -> None:
        demo = self.demo_requested()
        demo_db = self.seed_demo() if demo else None
        parts = ("web",) if demo else ("poller", "web")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0
        procs = []
        for part in parts:
            p = subprocess.Popen(self._cmd(self.cfg.current / part / "main.py"),
                                 env=self.child_env(part, demo_db=demo_db),
                                 cwd=str(self.cfg.current / part),
                                 stdin=subprocess.DEVNULL, creationflags=flags)
            self.job.assign(p)
            log(f"{part} started (pid {p.pid})" + (" [demo]" if demo else ""))
            procs.append(p)
        self.procs = procs

    def stop(self) -> None:
        procs, self.procs = self.procs, []
        for p in procs:
            if p.poll() is not None:
                continue
            try:
                if _IS_WINDOWS:
                    # Ctrl+Break reaches a child in its own process group; uvicorn shuts down on it.
                    # It needs a shared console, which a service may not have — then terminate.
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    p.terminate()
            except OSError:
                _kill_tree(p)
        deadline = time.monotonic() + 10
        for p in procs:
            while p.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if p.poll() is None:
                log(f"pid {p.pid} did not stop in time — killing it")
                _kill_tree(p)

    def wait_until_serving(self) -> bool:
        url = f"http://127.0.0.1:{self.cfg.port}/healthz"
        deadline = time.monotonic() + STARTUP_GRACE_S
        while time.monotonic() < deadline and not self.quit.is_set():
            if any(p.poll() is not None for p in self.procs):
                return False                     # a child died during startup: no point waiting
            try:
                urllib.request.urlopen(url, timeout=3).close()
                return True
            except urllib.error.HTTPError:
                return True                      # it answered: 503 just means the poller is warming up
            except Exception:                    # noqa: BLE001 — not listening yet
                self.quit.wait(0.5)
        return False

    def watch(self):
        """Until a child exits (→ its code), a staged update is ready (→ _APPLY_UPDATE) or we
        are asked to quit (→ None)."""
        while not self.quit.is_set():
            for p in self.procs:
                code = p.poll()
                if code is not None:
                    return code
            if self.update_ready.is_set():
                return _APPLY_UPDATE
            self.quit.wait(0.5)
        return None

    # ── main loop ─────────────────────────────────────────────────────────────────────────

    def prepare_current_env(self) -> None:
        self.env_python = str(envs.ensure_env(self.cfg.current, self.cfg.venvs, python=self.cfg.python,
                                              uv=self.cfg.uv, cache_dir=self.cfg.data_dir / "cache",
                                              log=log))

    def prune_envs(self) -> None:
        keep = {envs.key_of(Path(self.env_python))} if self.env_python else set()
        if self.cfg.previous.exists():
            base = envs.base_python(self.cfg.python)
            keep.add(envs.env_key(self.cfg.previous, envs.python_version(base)))
        if self.pending and self.cfg.staged.exists():
            base = envs.base_python(self.cfg.python)
            keep.add(envs.env_key(self.cfg.staged, envs.python_version(base)))
        envs.prune(self.cfg.venvs, keep, log=log)

    def apply_staged(self) -> None:
        rel, self.pending = self.pending, None
        self.update_ready.clear()
        log(f"installing Mate {rel['version'] if rel else '?'} — restarting services")
        self.stop()
        updater.install_payload(self.cfg.staged, self.cfg.current, self.cfg.previous, log=log)

    def run(self, seed_from: Path | None = None) -> int:
        cfg = self.cfg
        if not self.ensure_payload(seed_from):
            return 0 if self.quit.is_set() else 1

        fresh = seed_from is not None        # a just-installed payload may be rolled back
        if cfg.update != "off" and not seed_from:
            if self.stage_update():
                self.apply_staged()
                fresh = True
        if cfg.update != "off" and not cfg.version_pin:
            threading.Thread(target=self._update_loop, name="updates", daemon=True).start()

        while not self.quit.is_set():
            try:
                self.prepare_current_env()
            except Exception as exc:                           # noqa: BLE001
                log(f"cannot prepare the environment for Mate {updater.payload_version(cfg.current)}: {exc}")
                if fresh and updater.rollback(cfg.current, cfg.previous, log=log):
                    fresh = False
                    continue
                return 1

            self.spawn()
            if not self.wait_until_serving():
                if self.quit.is_set():
                    break
                died = [p.returncode for p in self.procs if p.poll() is not None]
                log(f"Mate {updater.payload_version(cfg.current)} did not come up: "
                    + (f"a service exited during startup (code {died[0]})" if died
                       else f"no answer on port {cfg.port} within {STARTUP_GRACE_S}s"))
                self.stop()
                if fresh and updater.rollback(cfg.current, cfg.previous, log=log):
                    fresh = False
                    continue
                return 1
            log(f"Mate {updater.payload_version(cfg.current)} serving on port {cfg.port}")
            fresh = False
            self.prune_envs()

            outcome = self.watch()
            if outcome is None:                                # asked to quit
                break
            if outcome is _APPLY_UPDATE:
                self.apply_staged()
                fresh = True
                continue
            self.stop()
            if outcome == RELAUNCH_CODE:
                log("relaunch requested by Mate — restarting services")
                continue
            log(f"a service exited unexpectedly (code {outcome}) — stopping")
            return outcome or 1
        return 0


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill a child AND its descendants. On Windows a venv's python.exe is a launcher that runs the
    real interpreter as its own child, so killing just the pid we started leaves Mate running —
    still on the port, still polling — while we believe it stopped."""
    if _IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                       capture_output=True, timeout=30)
    else:
        p.kill()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        log(f"pid {p.pid} survived being killed")


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        if not _IS_WINDOWS:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def checkpoint_db(db: Path) -> None:
    """Fold the WAL back into the main file on a clean stop: one file to back up, not three."""
    if not db.exists():
        return
    try:
        con = sqlite3.connect(str(db), timeout=5)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except Exception as exc:                                   # noqa: BLE001
        log(f"checkpoint skipped: {exc}")


def serve(cfg: Config, seed_from: Path | None = None) -> int:
    # Our own lines carry arrows and dashes too; a redirected stdout on Windows is ANSI otherwise.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass
    for sub in ("", "certs", "tmp", "cache", "payload"):
        (cfg.data_dir / sub).mkdir(parents=True, exist_ok=True)
    if not lock.acquire(cfg.data_dir):
        log(f"another mate-server is already running on {cfg.data_dir}")
        return 1
    server = Server(cfg)

    def on_signal(signum, _frame):
        if not server.quit.is_set():
            log(f"stop requested (signal {signum})")
        server.quit.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    # The way a Windows service is stopped. Signals cannot be relied on there: the service wrapper's
    # Ctrl+C needs to attach to our console, and under a service account that was refused. A file
    # in the data directory needs nothing but the write access the service already has.
    stop_file = cfg.data_dir / STOP_FILE
    stop_file.unlink(missing_ok=True)

    def watch_stop_file():
        while not server.quit.wait(0.5):
            if stop_file.exists():
                log("stop requested (service stop)")
                server.quit.set()

    threading.Thread(target=watch_stop_file, name="stop-file", daemon=True).start()

    log(f"mate-server {SERVER_VERSION} — data {cfg.data_dir}, port {cfg.port}, updates {cfg.update}"
        + (f" (pinned to {cfg.version_pin})" if cfg.version_pin else ""))
    if not cfg.passthrough.get("MATE_AUTH_PASSWORD"):
        log("WARNING: no MATE_AUTH_PASSWORD set — until a password is set in Mate's Settings, "
            "anyone who can reach this port can control the car")
    if not _port_free(cfg.port):
        log(f"port {cfg.port} is already in use — set MATE_PORT to a free one")
        lock.release()
        return 1
    try:
        code = server.run(seed_from)
    finally:
        server.stop()
        checkpoint_db(cfg.db_path)
        stop_file.unlink(missing_ok=True)
        lock.release()
    log(f"mate-server stopped (code {code})")
    return code
