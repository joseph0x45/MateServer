"""Unit tests for the parts of mate-server that need no network and no Mate checkout."""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mate_server import config, envs, updater  # noqa: E402
from mate_server.supervisor import Server  # noqa: E402


# ── config ──────────────────────────────────────────────────────────────────────────────

def test_env_file_parsing_keeps_values_literal():
    parsed = config.parse_env_file(
        "# comment\n"
        "MATE_PORT=4100\n"
        "export TZ=Europe/Rome\n"
        "MATE_AUTH_PASSWORD=pa$$=word#1\n"
        "QUOTED=\"two words\"\n"
        "SINGLE='x'\n"
        "not a line\n"
        "  \n")
    assert parsed == {"MATE_PORT": "4100", "TZ": "Europe/Rome",
                      "MATE_AUTH_PASSWORD": "pa$$=word#1", "QUOTED": "two words", "SINGLE": "x"}


def test_known_keys_are_consumed_and_the_rest_pass_through(tmp_path):
    f = tmp_path / "mate.env"
    f.write_text(f"MATE_DATA_DIR={tmp_path}\nMATE_PORT=4123\nMATE_UPDATE=notify\n"
                 "MATE_AUTH_PASSWORD=s3cret\nMATE_TRUSTED_ORIGINS=https://x.example\n", encoding="utf-8")
    cfg = config.load(f, environ={})
    assert cfg.port == 4123 and cfg.update == "notify"
    assert cfg.passthrough == {"MATE_AUTH_PASSWORD": "s3cret", "MATE_TRUSTED_ORIGINS": "https://x.example"}
    assert cfg.current == tmp_path.resolve() / "payload" / "current"


def test_environment_overrides_the_file_but_does_not_leak_everything(tmp_path):
    f = tmp_path / "mate.env"
    f.write_text("MATE_PORT=4123\nTZ=Europe/Rome\n", encoding="utf-8")
    cfg = config.load(f, environ={"MATE_PORT": "5000", "TZ": "UTC", "PATH": "/bin", "HOME": "/root"})
    assert cfg.port == 5000
    assert cfg.passthrough == {"TZ": "UTC"}


@pytest.mark.parametrize("text", ["MATE_PORT=abc", "MATE_PORT=70000", "MATE_UPDATE=sometimes"])
def test_bad_config_is_refused(tmp_path, text):
    f = tmp_path / "mate.env"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(f, environ={})


def test_update_interval_has_a_floor(tmp_path):
    f = tmp_path / "mate.env"
    f.write_text("MATE_UPDATE_INTERVAL_HOURS=0.001", encoding="utf-8")
    assert config.load(f, environ={}).update_interval_hours == 0.25


# ── versions ────────────────────────────────────────────────────────────────────────────

def test_release_order():
    o = updater.release_order
    assert o("4.17.0") > o("4.16.3") > o("4.16.0")
    assert o("4.10.0") > o("4.9.2")
    assert o("4.0.0") > o("4.0.0-rc.1")
    assert o("v4.17.0") == o("4.17.0")


def _payload(root: Path, version: str = "4.17.0", reqs: str = "fastapi==0.115.0\n") -> Path:
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "poller").mkdir(parents=True, exist_ok=True)
    (root / "web" / "main.py").write_text(f'"""doc"""\nMATE_VERSION = "{version}"  # note\n', encoding="utf-8")
    (root / "poller" / "main.py").write_text("", encoding="utf-8")
    (root / "web" / "requirements.txt").write_bytes(reqs.encode())     # bytes: no newline translation
    (root / "poller" / "requirements.txt").write_text("paho-mqtt>=2.0.0\n", encoding="utf-8")
    return root


def test_payload_version_and_completeness(tmp_path):
    p = _payload(tmp_path / "p", "4.16.3")
    assert updater.payload_version(p) == "4.16.3"
    assert updater.payload_looks_complete(p) is None
    (p / "poller" / "main.py").unlink()
    assert updater.payload_looks_complete(p) == "poller/main.py missing"
    assert updater.payload_version(tmp_path / "nothing") is None


# ── install / rollback ──────────────────────────────────────────────────────────────────

def test_install_then_rollback(tmp_path):
    root = tmp_path / "payload"
    cur, prev, staged = root / "current", root / "previous", root / "staged"
    _payload(cur, "1.0.0")
    _payload(staged, "2.0.0")
    updater.install_payload(staged, cur, prev, log=lambda *_: None)
    assert updater.payload_version(cur) == "2.0.0"
    assert updater.payload_version(prev) == "1.0.0"
    assert not staged.exists()
    assert updater.rollback(cur, prev, log=lambda *_: None)
    assert updater.payload_version(cur) == "1.0.0"
    assert not prev.exists()
    assert not updater.rollback(cur, prev, log=lambda *_: None)     # nothing left to roll back to


def test_first_install_has_no_rollback_target(tmp_path):
    root = tmp_path / "payload"
    _payload(root / "staged", "1.0.0")
    updater.install_payload(root / "staged", root / "current", root / "previous", log=lambda *_: None)
    assert updater.payload_version(root / "current") == "1.0.0"
    assert not (root / "previous").exists()


# ── tarball extraction ──────────────────────────────────────────────────────────────────

def _tarball(members: dict[str, bytes], links: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
    return buf.getvalue()


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _serve_tarball(monkeypatch, data: bytes):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(data))


def test_fetch_extracts_only_web_and_poller(tmp_path, monkeypatch):
    _serve_tarball(monkeypatch, _tarball({
        "leapmotor-mate-4.17.0/web/main.py": b'MATE_VERSION = "4.17.0"\n',
        "leapmotor-mate-4.17.0/poller/main.py": b"",
        "leapmotor-mate-4.17.0/docs/README.md": b"docs",
        "leapmotor-mate-4.17.0/Dockerfile": b"FROM x",
    }))
    dest = tmp_path / "payload" / "staged"
    updater.fetch_payload("v4.17.0", dest, log=lambda *_: None)
    assert sorted(p.name for p in dest.iterdir()) == ["poller", "web"]
    assert updater.payload_version(dest) == "4.17.0"


def test_fetch_refuses_path_traversal(tmp_path, monkeypatch):
    _serve_tarball(monkeypatch, _tarball({
        "leapmotor-mate-x/web/main.py": b"",
        "leapmotor-mate-x/web/../../../evil.py": b"boom",
    }))
    with pytest.raises(ValueError, match="unsafe path"):
        updater.fetch_payload("vX", tmp_path / "payload" / "staged", log=lambda *_: None)


def test_fetch_refuses_links(tmp_path, monkeypatch):
    _serve_tarball(monkeypatch, _tarball({"leapmotor-mate-x/web/main.py": b""},
                                         links=("leapmotor-mate-x/web/passwd",)))
    with pytest.raises(ValueError, match="link"):
        updater.fetch_payload("vX", tmp_path / "payload" / "staged", log=lambda *_: None)


# ── environments ────────────────────────────────────────────────────────────────────────

def test_env_key_follows_requirements_not_code(tmp_path):
    a = _payload(tmp_path / "a", "1.0.0")
    b = _payload(tmp_path / "b", "2.0.0")                    # new code, same requirements
    c = _payload(tmp_path / "c", "3.0.0", reqs="fastapi==0.116.0\n")
    assert envs.env_key(a, "3.12.3") == envs.env_key(b, "3.12.3")
    assert envs.env_key(a, "3.12.3") != envs.env_key(c, "3.12.3")
    assert envs.env_key(a, "3.12.3") != envs.env_key(a, "3.12.4")


def test_env_key_ignores_line_endings(tmp_path):
    a = _payload(tmp_path / "a", reqs="fastapi==0.115.0\nuvicorn==0.30.0\n")
    b = _payload(tmp_path / "b", reqs="fastapi==0.115.0\r\nuvicorn==0.30.0\r\n")
    assert envs.env_key(a, "3.12.3") == envs.env_key(b, "3.12.3")


def test_ready_environment_is_reused_without_building(tmp_path, monkeypatch):
    p = _payload(tmp_path / "p")
    monkeypatch.setattr(envs, "python_version", lambda _py: "3.12.3")
    key = envs.env_key(p, "3.12.3")
    target = tmp_path / "venvs" / key
    py = envs.env_python(target)
    py.parent.mkdir(parents=True)
    py.write_text("")
    (target / envs.READY_MARK).write_text(key)
    monkeypatch.setattr(envs, "_run", lambda *a, **k: pytest.fail("must not build"))
    assert envs.ensure_env(p, tmp_path / "venvs", python="python") == py


def test_failed_build_leaves_nothing_behind(tmp_path, monkeypatch):
    p = _payload(tmp_path / "p")
    monkeypatch.setattr(envs, "python_version", lambda _py: "3.12.3")
    monkeypatch.setattr(envs, "find_uv", lambda _c="": None)

    def failing_run(cmd, log, env=None):
        Path(cmd[-1]).mkdir(parents=True, exist_ok=True)       # venv "created"…
        if "pip" in cmd:
            raise envs.EnvError("no network")                   # …but the install fails

    monkeypatch.setattr(envs, "_run", failing_run)
    with pytest.raises(envs.EnvError):
        envs.ensure_env(p, tmp_path / "venvs", python="python", log=lambda *_: None)
    assert list((tmp_path / "venvs").iterdir()) == []


def test_prune_keeps_only_what_is_in_use(tmp_path):
    v = tmp_path / "venvs"
    for name in ("aaa", "bbb", "ccc"):
        (v / name).mkdir(parents=True)
    envs.prune(v, {"aaa", "ccc"}, log=lambda *_: None)
    assert sorted(d.name for d in v.iterdir()) == ["aaa", "ccc"]


# ── what the children are told ──────────────────────────────────────────────────────────

def test_child_env(tmp_path, monkeypatch):
    f = tmp_path / "mate.env"
    f.write_text(f"MATE_DATA_DIR={tmp_path}\nMATE_PORT=4123\nMATE_AUTH_PASSWORD=pw\nTZ=Europe/Rome\n",
                 encoding="utf-8")
    monkeypatch.setenv("MATE_DESKTOP", "1")                    # must never reach a server child
    cfg = config.load(f, environ={})
    env = Server(cfg).child_env("web")
    assert env["DB_PATH"] == str(cfg.data_dir / "leapmotor_mate.db")
    assert env["WEB_PORT"] == "4123"
    assert env["PYTHONPATH"] == str(cfg.current / "web")
    assert env["MATE_AUTH_PASSWORD"] == "pw" and env["TZ"] == "Europe/Rome"
    assert env["TMPDIR"] == str(cfg.data_dir / "tmp")
    assert "MATE_DESKTOP" not in env and "MATE_DEMO" not in env
    demo = Server(cfg).child_env("web", demo_db=tmp_path / "demo.db")
    assert demo["MATE_DEMO"] == "1" and demo["DB_PATH"] == str(tmp_path / "demo.db")


# ── installer commands ──────────────────────────────────────────────────────────────────

from mate_server import setup_cmds  # noqa: E402
from mate_server.__main__ import main as cli  # noqa: E402


def test_configure_creates_then_updates_without_losing_edits(tmp_path):
    cfg_file = tmp_path / "LeapMotorMate" / "mate.env"
    data = cfg_file.parent
    assert setup_cmds.write_config(cfg_file, data_dir=data, port=4100, password='p@ss w0rd$%!"x')
    parsed = config.parse_env_file(cfg_file.read_text(encoding="utf-8"))
    assert parsed["MATE_PORT"] == "4100" and parsed["MATE_AUTH_PASSWORD"] == 'p@ss w0rd$%!"x'
    assert parsed["MATE_DATA_DIR"] == str(data) and parsed["MATE_UPDATE"] == "auto"

    # The user edits the file by hand; an upgrade with an empty password must keep it all.
    with open(cfg_file, "a", encoding="utf-8") as f:
        f.write("TZ=Europe/Rome\n# my note\n")
    assert not setup_cmds.write_config(cfg_file, data_dir=data, port=4200, password="")
    text = cfg_file.read_text(encoding="utf-8")
    parsed = config.parse_env_file(text)
    assert parsed["MATE_PORT"] == "4200"
    assert parsed["MATE_AUTH_PASSWORD"] == 'p@ss w0rd$%!"x'
    assert parsed["TZ"] == "Europe/Rome" and "# my note" in text
    assert text.count("MATE_PORT=") == 1

    # …and a new password replaces the old one in place.
    setup_cmds.write_config(cfg_file, data_dir=data, port=4200, password="newer")
    assert config.parse_env_file(cfg_file.read_text(encoding="utf-8"))["MATE_AUTH_PASSWORD"] == "newer"


def test_configure_without_password_leaves_a_hint(tmp_path):
    f = tmp_path / "mate.env"
    setup_cmds.write_config(f, data_dir=tmp_path, port=4000, password="")
    assert "MATE_AUTH_PASSWORD" not in config.parse_env_file(f.read_text(encoding="utf-8"))
    assert "# MATE_AUTH_PASSWORD=" in f.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [" lead", "trail ", "two\nlines"])
def test_configure_refuses_passwords_the_file_cannot_hold(tmp_path, bad):
    with pytest.raises(ValueError):
        setup_cmds.write_config(tmp_path / "mate.env", data_dir=tmp_path, port=4000, password=bad)


def test_uninstall_cleanup_keeps_the_driving_history(tmp_path):
    d = tmp_path / "LeapMotorMate"
    for sub in ("payload/current/web", "venvs/abc", "cache/uv", "tmp", "logs", "certs"):
        (d / sub).mkdir(parents=True)
    for name in ("mate.env", "leapmotor_mate.db", "secret.key", "server.lock", "certs/app.crt"):
        (d / name).write_text("x")
    setup_cmds.uninstall_cleanup(d)
    assert sorted(p.name for p in d.iterdir()) == ["certs", "leapmotor_mate.db", "mate.env", "secret.key"]


def test_uninstall_cleanup_refuses_a_foreign_folder(tmp_path):
    (tmp_path / "payload").mkdir()
    setup_cmds.uninstall_cleanup(tmp_path)               # wrong name, no mate.env
    assert (tmp_path / "payload").exists()


def test_stop_tolerates_the_arguments_winsw_appends(tmp_path, capsys):
    f = tmp_path / "mate.env"
    f.write_text(f"MATE_DATA_DIR={tmp_path}\n", encoding="utf-8")
    # What WinSW 2.12 actually runs: <stoparguments> + " " + <arguments>
    assert cli(["stop", "--config", str(f), "-I", "mate-server.py", "serve", "--config", str(f)]) == 0
    assert "not running" in capsys.readouterr().out


def test_other_commands_stay_strict(tmp_path):
    with pytest.raises(SystemExit):
        cli(["status", "--bogus"])


def test_unreadable_config_is_a_clear_error(tmp_path, monkeypatch):
    f = tmp_path / "mate.env"
    f.write_text("MATE_PORT=4000", encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(config.ConfigError, match="elevated"):
        config.load(f, environ={})
