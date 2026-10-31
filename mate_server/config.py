"""Configuration: one KEY=VALUE file, identical in shape on Linux and Windows.

The file is read by the shell itself rather than by systemd's EnvironmentFile, so the same file and
the same rules apply on both systems. Real environment variables win over the file — the usual
convention, and the way to override one value for a single run without editing anything.

Keys the shell understands are listed in KNOWN. Every OTHER key is passed through to Mate untouched,
which is how Mate's own settings (TZ, MATE_TRUSTED_ORIGINS, MATE_FRAME_ANCESTORS, …) are set without
the shell having to know about each one.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

UPDATE_MODES = ("auto", "notify", "off")

# Keys consumed by the shell. MATE_AUTH_PASSWORD is consumed AND passed on: Mate reads it.
KNOWN = {
    "MATE_DATA_DIR", "MATE_PORT", "MATE_UPDATE", "MATE_UPDATE_INTERVAL_HOURS", "MATE_VERSION_PIN",
    "MATE_PYTHON", "MATE_UV",
}


def default_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "LeapMotorMate"
    return Path("/var/lib/leapmotor-mate")


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE per line; '#' comments; optional 'export '; matching quotes around a value are
    stripped. Deliberately no variable expansion — a password containing '$' must arrive intact."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    data_dir: Path
    port: int = 4000
    update: str = "auto"
    update_interval_hours: float = 6.0
    version_pin: str = ""
    python: str = ""            # base interpreter for payload environments ('' = the one running us)
    uv: str = ""                # uv binary ('' = look on PATH, else fall back to venv + pip)
    passthrough: dict[str, str] = field(default_factory=dict)
    source: Path | None = None

    # ── layout of the data directory (mirrors MateDesktop, so a data dir can move between them) ──
    @property
    def db_path(self) -> Path:
        return self.data_dir / "leapmotor_mate.db"

    @property
    def payload_root(self) -> Path:
        return self.data_dir / "payload"

    @property
    def current(self) -> Path:
        return self.payload_root / "current"

    @property
    def previous(self) -> Path:
        return self.payload_root / "previous"

    @property
    def staged(self) -> Path:
        return self.payload_root / "staged"

    @property
    def venvs(self) -> Path:
        return self.data_dir / "venvs"


def load(path: str | os.PathLike | None, environ: dict[str, str] | None = None) -> Config:
    environ = dict(os.environ if environ is None else environ)
    values: dict[str, str] = {}
    source = None
    if path:
        source = Path(path)
        if not source.is_file():
            raise ConfigError(f"config file not found: {source}")
        try:
            text = source.read_text(encoding="utf-8-sig")
        except PermissionError:
            # The installer makes the file readable by administrators and the service only (it holds
            # the password), so a plain prompt lands here.
            raise ConfigError(f"cannot read {source} - run this from an elevated prompt "
                              f"(Run as administrator)") from None
        values.update(parse_env_file(text))
    # The environment wins, but only for keys that mean something here — the whole process
    # environment is not "configuration" and must not all be treated as Mate settings.
    for key, value in environ.items():
        if key in KNOWN or key in values or key == "MATE_AUTH_PASSWORD":
            values[key] = value

    try:
        port = int(values.get("MATE_PORT", "4000") or 4000)
    except ValueError:
        raise ConfigError(f"MATE_PORT must be a number, got {values.get('MATE_PORT')!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"MATE_PORT out of range: {port}")

    update = (values.get("MATE_UPDATE", "auto") or "auto").strip().lower()
    if update not in UPDATE_MODES:
        raise ConfigError(f"MATE_UPDATE must be one of {', '.join(UPDATE_MODES)}, got {update!r}")

    try:
        interval = float(values.get("MATE_UPDATE_INTERVAL_HOURS", "6") or 6)
    except ValueError:
        raise ConfigError("MATE_UPDATE_INTERVAL_HOURS must be a number") from None
    interval = max(interval, 0.25)       # GitHub's anonymous API allows 60 requests an hour

    data_dir = Path(values.get("MATE_DATA_DIR") or default_data_dir()).expanduser()
    passthrough = {k: v for k, v in values.items() if k not in KNOWN}
    return Config(
        data_dir=data_dir.resolve() if data_dir.exists() else data_dir.absolute(),
        port=port,
        update=update,
        update_interval_hours=interval,
        version_pin=values.get("MATE_VERSION_PIN", "").strip(),
        python=values.get("MATE_PYTHON", "").strip(),
        uv=values.get("MATE_UV", "").strip(),
        passthrough=passthrough,
        source=source,
    )
