"""Find, download and swap Mate releases.

Adapted from MateDesktop's updater, with one deliberate difference: there is no dependency guard.
The desktop shell carries a frozen set of libraries and has to REFUSE a release that needs more; a
server can install them, so a release that adds a library is prepared an environment of its own
(see envs.py) instead of being turned away.

The payload is the source tarball GitHub generates for every tag, so feeding this needs nothing
from the release process beyond the tag itself.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "ProtossBlaster/leapmotor-mate"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
TAG_API = f"https://api.github.com/repos/{REPO}/releases/tags/{{tag}}"
TARBALL = f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.tar.gz"
# The server shell's own releases. Only ever ASKED, never installed from: the shell lives in a
# read-only install directory and is upgraded by re-running the installer.
SERVER_REPO = "joseph0x45/MateServer"
SERVER_RELEASES_API = f"https://api.github.com/repos/{SERVER_REPO}/releases/latest"
PAYLOAD_PARTS = ("web", "poller")
USER_AGENT = "leapmotor-mate-server"
_TIMEOUT = 30

last_error = ""          # why the most recent GitHub call failed; '' after one that worked
_CONTEXT: ssl.SSLContext | None = None


def https_context() -> ssl.SSLContext:
    """certifi when it is there (the installer puts it in the shell's environment), else the system
    store. On Windows the system store as Python sees it can be missing roots that Windows itself
    would fetch on demand — MateDesktop measured 17 of ~150 on a fresh machine — hence certifi."""
    global _CONTEXT
    if _CONTEXT is None:
        try:
            import certifi
            _CONTEXT = ssl.create_default_context(cafile=certifi.where())
        except Exception:                                      # noqa: BLE001
            _CONTEXT = ssl.create_default_context()
    return _CONTEXT


def version_tuple(v: str) -> tuple:
    out = []
    for part in str(v or "").lstrip("vV").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out) or (0,)


def release_order(version: str) -> tuple:
    """Order a stable release after its own release candidates."""
    core, _, prerelease = str(version).lstrip("vV").partition("-")
    return (version_tuple(core), not bool(prerelease), version_tuple(prerelease))


def payload_version(payload_dir: Path) -> str | None:
    """The MATE_VERSION the payload declares in web/main.py — the single source of truth."""
    main = Path(payload_dir) / "web" / "main.py"
    if not main.is_file():
        return None
    for line in main.read_text(encoding="utf-8", errors="replace").splitlines()[:80]:
        m = re.match(r'\s*MATE_VERSION\s*=\s*["\']([^"\']+)["\']', line)
        if m:
            return m.group(1)
    return None


def _get_release(api: str) -> dict | None:
    global last_error
    try:
        req = urllib.request.Request(api, headers={
            "Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=https_context()) as r:
            data = json.load(r)
        tag = data.get("tag_name", "")
        if not tag:
            last_error = "the release has no tag"
            return None
        last_error = ""
        return {"tag": tag, "version": str(tag).lstrip("vV")}
    except Exception as exc:                                   # noqa: BLE001 — never fatal
        last_error = f"{type(exc).__name__}: {exc}"
        return None


def latest_release() -> dict | None:
    """The newest stable Mate (GitHub's /latest skips prereleases and drafts)."""
    return _get_release(RELEASES_API)


def release_for_tag(tag: str) -> dict | None:
    rel = _get_release(TAG_API.format(tag=urllib.parse.quote(tag, safe="")))
    return rel if rel and rel["tag"] == tag else None


def newer_server(current: str) -> str | None:
    rel = _get_release(SERVER_RELEASES_API)
    if rel and version_tuple(rel["version"]) > version_tuple(current):
        return rel["version"]
    return None


def fetch_payload(tag: str, dest: Path, log=print) -> Path:
    """Download the tag's source tarball and extract ONLY web/ and poller/ into `dest`."""
    url = TARBALL.format(tag=urllib.parse.quote(tag, safe=""))
    log(f"downloading {url}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        archive = Path(tmp) / "src.tar.gz"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=https_context()) as r, \
                open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        log(f"got {archive.stat().st_size / 1e6:.1f} MB")
        with tarfile.open(archive) as tf:
            names = tf.getnames()
            if not names:
                raise ValueError("empty archive")
            root = names[0].split("/")[0]
            wanted = [m for m in tf.getmembers()
                      if any(m.name.startswith(f"{root}/{p}/") for p in PAYLOAD_PARTS)]
            for m in wanted:
                if os.path.isabs(m.name) or ".." in Path(m.name).parts:
                    raise ValueError(f"unsafe path in archive: {m.name}")
                if m.issym() or m.islnk():
                    raise ValueError(f"link in archive refused: {m.name}")
            if hasattr(tarfile, "data_filter"):     # 3.12+: the stdlib's own safety net, on top
                tf.extractall(tmp, members=wanted, filter="data")
            else:
                tf.extractall(tmp, members=wanted)
        for part in PAYLOAD_PARTS:
            src = Path(tmp) / root / part
            if src.is_dir():
                shutil.copytree(src, dest / part, dirs_exist_ok=True)
    return dest


def copy_payload(source: Path, dest: Path) -> Path:
    """Install web/ + poller/ from a local leapmotor-mate checkout (offline installs, development)."""
    dest.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for part in PAYLOAD_PARTS:
        src = Path(source) / part
        if src.is_dir():
            shutil.copytree(src, dest / part, dirs_exist_ok=True, ignore=ignore)
    return dest


def payload_looks_complete(payload_dir: Path) -> str | None:
    """Why this is not a usable Mate, or None when it is."""
    for part in PAYLOAD_PARTS:
        if not (payload_dir / part / "main.py").is_file():
            return f"{part}/main.py missing"
    if not payload_version(payload_dir):
        return "no MATE_VERSION found"
    return None


def install_payload(staged: Path, current: Path, previous: Path, log=print) -> None:
    """staged → current, keeping the outgoing current as the rollback target. The caller must have
    stopped the services first: Windows will not rename a directory a running process sits in."""
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)
    if current.exists():
        current.rename(previous)
    staged.rename(current)
    log(f"payload {payload_version(current)} installed (rollback kept as '{previous.name}')")


def rollback(current: Path, previous: Path, log=print) -> bool:
    if not previous.exists():
        return False
    broken = current.with_name("broken")
    shutil.rmtree(broken, ignore_errors=True)
    if current.exists():
        current.rename(broken)
    previous.rename(current)
    shutil.rmtree(broken, ignore_errors=True)
    log(f"rolled back to {payload_version(current)}")
    return True
