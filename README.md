# LeapMotor Mate — server

[Mate](https://github.com/ProtossBlaster/leapmotor-mate) as a self-updating service on a Windows or
Linux machine, with no Docker and no Home Assistant. You install it once, open `http://<server>:4000`
and sign in with your Leapmotor account. New Mate releases install themselves.

It's the same Mate as the Docker image, the Home Assistant add-on and
[MateDesktop](https://github.com/ProtossBlaster/MateDesktop). The code is identical, and so are the database and the updates.

## Install on Windows (10/11 or Server 2016+, x64)

Run **`LeapMotor-Mate-Server-<version>-x64.msi`**. It asks for:

- a **port** (default 4000)
- a **password** for the web page (the page can unlock the car)
- whether to **open Windows Firewall** for the local network (local subnet only)

That's all. It installs a Windows service called **LeapMotor Mate**, which starts at boot and restarts itself if it
crashes. It also adds a Start-menu shortcut to the web page. Python 3.12, uv and the service wrapper are inside
the package, so nothing else needs to be installed. The first start takes a minute or two, because it downloads
Mate and its libraries. The MSI isn't signed, so Windows shows "Unknown publisher" when it asks for administrator rights.

**Upgrade:** run a newer `.msi` over the old one. The configuration and data are kept, and the settings page offers the
current port; leave the password fields empty to keep the current password. *Mate itself* updates on its
own without a new `.msi`.

**Uninstall:** *Settings ▸ Apps ▸ LeapMotor Mate Server*. That removes the program and everything the server
downloaded, and **keeps** the database, its key, the certificates and `mate.env`, so a reinstall picks up where
it left off. Delete `C:\ProgramData\LeapMotorMate` afterwards to remove those too.

**Silent install** (for fleets or scripts). The dialog's checks don't run in this mode, so the password must
not contain `"` or end with `\`:

```powershell
msiexec /i LeapMotor-Mate-Server-0.1.1-x64.msi /qn MATEPASSWORD="something-long" MATEPORT=4000 OPENFIREWALL=1
```

## Install on Linux (systemd; x86-64, arm64, armv7)

```sh
sudo ./packaging/linux/install.sh                  # asks for a UI password
sudo ./packaging/linux/install.sh --port 8080 --password 'something-long'
sudo ./packaging/linux/uninstall.sh                # keeps data;  --purge deletes it
```

The installer downloads a pinned, checksum-verified [uv](https://github.com/astral-sh/uv), which installs
**Python 3.12**. Mate needs 3.12 exactly, because Python 3.13+ verifies certificates strictly and Leapmotor's
certificates fail that check. Re-running it upgrades in place.

## Where things are

| | Windows | Linux |
|---|---|---|
| Program | `C:\Program Files\LeapMotorMate` | `/opt/leapmotor-mate` |
| Configuration | `C:\ProgramData\LeapMotorMate\mate.env` | `/etc/leapmotor-mate/mate.env` |
| Data | `C:\ProgramData\LeapMotorMate` | `/var/lib/leapmotor-mate` |
| Logs | `C:\ProgramData\LeapMotorMate\logs` | `journalctl -u leapmotor-mate -f` |
| Restart | `Restart-Service LeapMotorMate` | `sudo systemctl restart leapmotor-mate` |
| Runs as | `NT AUTHORITY\LocalService` | system user `leapmotor-mate` |
| CLI | `& "$env:ProgramFiles\LeapMotorMate\mate-server.cmd" status` | `sudo mate-server status` |

`mate.env` holds the password, so only administrators and the service can read it. Run the CLI from an
elevated prompt. Its commands are `status`, `check-update`, `update` (with the service stopped) and `stop`.

## Configuration

`mate.env` is one `KEY=VALUE` per line. Values are taken literally: no quotes are needed and no `$` expansion happens.
Restart the service after editing it. On Windows the port and password can also be changed by running the installer again.
See [`mate.env.example`](mate.env.example).

| Key | Default | |
|---|---|---|
| `MATE_PORT` | `4000` | port the web page listens on (all interfaces) |
| `MATE_AUTH_PASSWORD` | — | **set this.** The page can unlock the car. |
| `MATE_UPDATE` | `auto` | `auto` installs new Mate releases; `notify` only logs them; `off` |
| `MATE_UPDATE_INTERVAL_HOURS` | `6` | how often to ask GitHub |
| `MATE_VERSION_PIN` | — | run exactly this Mate version (e.g. `4.17.0`) and never move off it |
| `MATE_DATA_DIR` | see above | where everything lives |

Any other key is passed straight to Mate. That covers `TZ`, `MATE_TRUSTED_ORIGINS` and `MATE_FRAME_ANCESTORS`.

## Reaching it from outside your network

Don't forward the port on your router. Use a VPN (WireGuard, Tailscale) or a reverse proxy that terminates
TLS (Caddy, nginx). Behind a proxy that rewrites `Host`, set `MATE_TRUSTED_ORIGINS=https://your.public.name`.
The Windows firewall rule only admits the local subnet. Whoever reaches this page can open the car.

## How updates work

```
data folder/
  payload/current/      the Mate that is running (web/ + poller/ from the release tarball)
  payload/previous/     the one before it: the rollback target
  payload/staged/       a new release being prepared
  venvs/<hash>/         one Python environment per distinct set of requirements
  leapmotor_mate.db, secret.key, certs/, mate.env, logs/ …
```

1. Every few hours the server asks GitHub for the newest **stable** Mate release.
2. When there is one, it downloads the tag's source tarball and extracts `web/` and `poller/`. Then it
   prepares a Python environment for it. Mate keeps serving during all of this.
   - Environments are keyed by a hash of the requirements files, so most releases reuse the current one.
   - A release that changes its libraries gets a new environment, built alongside the running one.
     The Desktop app has to refuse such updates; a server can just install them.
3. It stops Mate, swaps `staged` → `current` (keeping the old one as `previous`) and starts it again.
   Mate is down for a few seconds.
4. If the new version doesn't come up, it rolls back to `previous` automatically.

The server shell itself (this repository) updates through a new `.msi` (Windows) or by re-running the
installer (Linux). When a newer one is published, it says so in the log.

## How stopping works (Windows)

The service wrapper's own stop signal (Ctrl+C) can't reach a service account's console, so the service is
stopped by asking instead. On *Stop*, the wrapper runs `mate-server stop`, which drops a request file. The
supervisor then stops Mate's poller and web server, checkpoints the database and exits. As a backstop, the
children run inside a Windows job object, so if the supervisor is ever killed, Windows kills them too and
nothing is left holding the port.

## Moving from Docker, Home Assistant or the Desktop app

Stop the old one and the service. Then copy its `leapmotor_mate.db` **and** `secret.key` into the data folder
(Docker: the `/data` volume; Desktop: `%LOCALAPPDATA%\LeapMotorMate` or
`~/Library/Application Support/LeapMotorMate`). Without `secret.key`, the stored login can't be decrypted and you'll
have to sign in again.

## Building the Windows installer

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_msi.ps1
# → dist\LeapMotor-Mate-Server-<version>-x64.msi  (+ .sha256)
```

It only needs the .NET runtime (6+) and internet access: no SDK, no WiX install, no admin rights. WiX 5.0.2 runs
straight from its NuGet package. Every download is pinned, and uv and WinSW are checked against SHA-256 values in the
script. The version comes from `SERVER_VERSION` in `mate_server/__init__.py`.

## Development

```sh
python -m mate_server serve --config my.env --seed-from ../leapmotor-mate   # run a local checkout
python -m pytest tests
```

## Licence

AGPL-3.0, like Mate. See [LICENSE](LICENSE).
