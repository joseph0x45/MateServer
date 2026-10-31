"""One Python environment per distinct set of payload requirements.

An environment is identified by a hash of the payload's requirements files plus the base
interpreter's version. Most releases change no requirements, so most updates reuse the environment
already in use and cost nothing. A release that does change them gets a NEW environment, built
beside the running one while Mate keeps serving, and only adopted once it installed completely.
The previous payload keeps its own environment, so a rollback is exactly as cheap as an update.

uv is used when available (fast, and it is how the installers provision Python 3.12); otherwise
the standard venv + pip route, which needs nothing but the interpreter.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Installed on top of the payload's own requirements. certifi gives Mate's urllib calls a complete
# trust store on Windows (see updater.https_context) — MateDesktop learned that one the hard way.
EXTRA_REQUIREMENTS = ("certifi",)
READY_MARK = ".mate-env-ready"
_INSTALL_TIMEOUT_S = 1800


class EnvError(RuntimeError):
    pass


def requirement_files(payload: Path) -> list[Path]:
    return [p for p in (payload / "poller" / "requirements.txt", payload / "web" / "requirements.txt")
            if p.is_file()]


def base_python(configured: str = "") -> str:
    """The interpreter environments are created from: the configured one, else the base of the
    interpreter running the shell (not the shell's own venv — a venv of a venv is fragile)."""
    if configured:
        return configured
    return getattr(sys, "_base_executable", None) or sys.executable


def python_version(python: str) -> str:
    try:
        out = subprocess.run([python, "-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or "unknown"
    except Exception:                                          # noqa: BLE001
        return "unknown"


def env_key(payload: Path, py_version: str) -> str:
    h = hashlib.sha256()
    h.update(f"python={py_version}\n".encode())
    h.update(("extra=" + ",".join(EXTRA_REQUIREMENTS) + "\n").encode())
    for f in requirement_files(payload):
        h.update(f"--- {f.parent.name}\n".encode())
        # Normalise line endings: a checkout with CRLF must not produce a different environment.
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def env_python(env_dir: Path) -> Path:
    if sys.platform == "win32":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def find_uv(configured: str = "") -> str | None:
    if configured:
        return configured if Path(configured).is_file() else None
    return shutil.which("uv")


def _run(cmd: list[str], log, env: dict | None = None) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env,
                       timeout=_INSTALL_TIMEOUT_S, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-15:]
        raise EnvError(f"command failed ({r.returncode}): {' '.join(map(str, cmd[:3]))} …\n"
                       + "\n".join(tail))


def ensure_env(payload: Path, venvs: Path, *, python: str = "", uv: str = "",
               cache_dir: Path | None = None, log=print) -> Path:
    """The environment for `payload`, built if needed. Returns its python executable.

    Built under a temporary name and renamed into place only when complete, so an interrupted
    build (power cut, service stop, failed download) can never be mistaken for a working one.
    """
    base = base_python(python)
    key = env_key(payload, python_version(base))
    target = venvs / key
    if (target / READY_MARK).is_file() and env_python(target).exists():
        return env_python(target)

    reqs = requirement_files(payload)
    if not reqs:
        raise EnvError(f"no requirements files in {payload}")
    venvs.mkdir(parents=True, exist_ok=True)
    building = venvs / f"{key}.building"
    shutil.rmtree(building, ignore_errors=True)
    shutil.rmtree(target, ignore_errors=True)          # a leftover without READY_MARK is garbage

    uv_bin = find_uv(uv)
    env = dict(os.environ)
    if cache_dir:
        env["UV_CACHE_DIR"] = str(cache_dir)
        env["PIP_CACHE_DIR"] = str(cache_dir / "pip")
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    log(f"preparing Python environment {key} ({'uv' if uv_bin else 'venv + pip'}, base {base})")
    try:
        req_args: list[str] = []
        for r in reqs:
            req_args += ["-r", str(r)]
        if uv_bin:
            _run([uv_bin, "venv", "--quiet", "--python", base, str(building)], log, env)
            _run([uv_bin, "pip", "install", "--quiet", "--python", str(env_python(building)),
                  *req_args, *EXTRA_REQUIREMENTS], log, env)
        else:
            _run([base, "-m", "venv", str(building)], log, env)
            _run([str(env_python(building)), "-m", "pip", "install", "--quiet",
                  "--disable-pip-version-check", *req_args, *EXTRA_REQUIREMENTS], log, env)
        # Prove it: the imports Mate cannot start without, in the new interpreter.
        _run([str(env_python(building)), "-c",
              "import fastapi, uvicorn, jinja2, cryptography, PIL, paho.mqtt.client, certifi"], log, env)
    except Exception:
        shutil.rmtree(building, ignore_errors=True)
        raise
    (building / READY_MARK).write_text(key, encoding="utf-8")
    building.rename(target)
    log(f"environment {key} ready")
    return env_python(target)


def key_of(env_python_path: Path) -> str:
    """'…/venvs/<key>/bin/python' → '<key>'."""
    return Path(env_python_path).parent.parent.name


def prune(venvs: Path, keep: set[str], log=print) -> None:
    """Remove environments no installed payload uses any more."""
    if not venvs.is_dir():
        return
    for d in venvs.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                log(f"removed unused environment {d.name}")
