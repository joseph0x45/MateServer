#!/bin/sh
# LeapMotor Mate server — Linux installer (systemd).
#
#   sudo ./packaging/linux/install.sh [--port 4000] [--password PASS | --no-password] [--tz Europe/Rome]
#
# Re-running it upgrades the server shell in place and keeps the configuration and all data.
#
# What goes where:
#   /opt/leapmotor-mate         the shell, uv and Python 3.12 (root-owned, read-only to the service)
#   /etc/leapmotor-mate/mate.env  configuration (root:leapmotor-mate, 0640 — it holds the password)
#   /var/lib/leapmotor-mate     database, certificates, Mate itself and its Python environments
set -eu

UV_VERSION="0.12.19"
PYTHON_VERSION="3.12"
PREFIX=/opt/leapmotor-mate
CONF_DIR=/etc/leapmotor-mate
DATA_DIR=/var/lib/leapmotor-mate
SERVICE_USER=leapmotor-mate
UNIT=/etc/systemd/system/leapmotor-mate.service

PORT=4000
PASSWORD=""
NO_PASSWORD=0
TZ_NAME=""

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --password) PASSWORD="$2"; shift 2 ;;
    --no-password) NO_PASSWORD=1; shift ;;
    --tz) TZ_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
[ -f "$SRC/mate_server/__main__.py" ] || die "run this from a MateServer checkout or release archive"

# ── uv (pinned, checksum-verified) ─────────────────────────────────────────────────────────
case "$(uname -m)" in
  x86_64|amd64)  UV_TARGET=x86_64-unknown-linux-musl
                 UV_SHA256=db7278c9f57981338fddff1fb250e11964bc0a4fafcb9eed8303fdb117dc067b ;;
  aarch64|arm64) UV_TARGET=aarch64-unknown-linux-musl
                 UV_SHA256=ad8d8448a2ff642ba62c2f684d7dd22a03f8eb3fc9918c2c3e8ec975f4ed6710 ;;
  armv7l)        UV_TARGET=armv7-unknown-linux-musleabihf
                 UV_SHA256=0df353aac087509c47fe660837ca5d284858ef4a5eec4042cbcdeb0118ba0f1b ;;
  *) die "unsupported CPU architecture: $(uname -m)" ;;
esac

mkdir -p "$PREFIX/bin" "$PREFIX/lib"
if [ ! -x "$PREFIX/bin/uv" ] || ! "$PREFIX/bin/uv" --version 2>/dev/null | grep -q " $UV_VERSION"; then
  say "installing uv $UV_VERSION ($UV_TARGET)"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_TARGET.tar.gz"
  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$TMP/uv.tgz" "$URL"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$TMP/uv.tgz" "$URL"
  else die "curl or wget is required"; fi
  echo "$UV_SHA256  $TMP/uv.tgz" | sha256sum -c - >/dev/null || die "uv download failed its checksum"
  tar -xzf "$TMP/uv.tgz" -C "$TMP"
  install -m 0755 "$TMP/uv-$UV_TARGET/uv" "$PREFIX/bin/uv"
fi

# ── Python 3.12 + the shell's environment ──────────────────────────────────────────────────
# Mate must stay on 3.12: Python 3.13+ verifies certificates strictly, and Leapmotor's do not pass.
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_CACHE_DIR="$PREFIX/.uv-cache"
say "installing Python $PYTHON_VERSION"
"$PREFIX/bin/uv" python install --quiet "$PYTHON_VERSION"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  "$PREFIX/bin/uv" venv --quiet --python "$PYTHON_VERSION" --managed-python "$PREFIX/venv"
fi
"$PREFIX/bin/uv" pip install --quiet --python "$PREFIX/venv/bin/python" --upgrade certifi
rm -rf "$UV_CACHE_DIR"

say "installing the mate-server shell"
rm -rf "$PREFIX/lib/mate_server"
cp -R "$SRC/mate_server" "$PREFIX/lib/mate_server"
find "$PREFIX/lib" -name '__pycache__' -prune -exec rm -rf {} +
chmod -R a+rX "$PREFIX"

cat > /usr/local/bin/mate-server <<EOF
#!/bin/sh
# Runs as the service user so files it creates stay readable by the service.
if [ "\$(id -u)" -eq 0 ]; then
  exec runuser -u $SERVICE_USER -- env PYTHONPATH=$PREFIX/lib MATE_UV=$PREFIX/bin/uv MATE_CONFIG=$CONF_DIR/mate.env $PREFIX/venv/bin/python -m mate_server "\$@"
fi
exec env PYTHONPATH=$PREFIX/lib MATE_UV=$PREFIX/bin/uv MATE_CONFIG=$CONF_DIR/mate.env $PREFIX/venv/bin/python -m mate_server "\$@"
EOF
chmod 0755 /usr/local/bin/mate-server

# ── service user + data ────────────────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "creating system user $SERVICE_USER"
  useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$DATA_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 0750 "$DATA_DIR"

# ── configuration (written once; a re-run never overwrites it) ─────────────────────────────
mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/mate.env" ]; then
  if [ -z "$PASSWORD" ] && [ "$NO_PASSWORD" -eq 0 ]; then
    if [ -t 0 ]; then
      printf 'Choose a password for the Mate web UI (it can unlock your car): '
      stty -echo; read -r PASSWORD; stty echo; echo
      [ -n "$PASSWORD" ] || die "a password is required (or pass --no-password and set one in Mate's Settings)"
    else
      die "no terminal to ask for a password: pass --password PASS or --no-password"
    fi
  fi
  # Written literally (the config reader does no unquoting of escapes and no expansion), so refuse
  # only what a line-based file cannot hold intact.
  case "$PASSWORD" in
    *"
"*|" "*|*" "|\"*\"|\'*\') die "the password cannot contain a line break, start or end with a space, or be wrapped in quotes" ;;
  esac
  [ -n "$TZ_NAME" ] || TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -n "$TZ_NAME" ] || TZ_NAME="$(cat /etc/timezone 2>/dev/null || true)"
  say "writing $CONF_DIR/mate.env"
  umask 027
  {
    echo "# LeapMotor Mate server configuration. Restart after editing:"
    echo "#   sudo systemctl restart leapmotor-mate"
    echo "MATE_DATA_DIR=$DATA_DIR"
    echo "MATE_PORT=$PORT"
    echo "# auto = install new Mate releases by themselves; notify = only log them; off"
    echo "MATE_UPDATE=auto"
    echo "MATE_UPDATE_INTERVAL_HOURS=6"
    echo "# MATE_VERSION_PIN=4.17.0"
    if [ -n "$PASSWORD" ]; then
      printf 'MATE_AUTH_PASSWORD=%s\n' "$PASSWORD"
    else
      echo "# MATE_AUTH_PASSWORD=   (none — set one in Mate's Settings before exposing this server)"
    fi
    [ -n "$TZ_NAME" ] && echo "TZ=$TZ_NAME"
    echo "# Behind a reverse proxy that rewrites Host, name the public origin here:"
    echo "# MATE_TRUSTED_ORIGINS=https://mate.example.com"
  } > "$CONF_DIR/mate.env"
  umask 022
else
  say "keeping existing $CONF_DIR/mate.env"
fi
chown root:"$SERVICE_USER" "$CONF_DIR" "$CONF_DIR/mate.env"
chmod 0750 "$CONF_DIR"
chmod 0640 "$CONF_DIR/mate.env"

# ── service ────────────────────────────────────────────────────────────────────────────────
install -m 0644 "$SRC/packaging/linux/leapmotor-mate.service" "$UNIT"
systemctl daemon-reload
systemctl enable leapmotor-mate.service >/dev/null
systemctl restart leapmotor-mate.service

PORT_NOW="$(sed -n 's/^MATE_PORT=//p' "$CONF_DIR/mate.env" | tail -1)"
say "done. Mate is starting — the first start downloads Mate and its libraries (a minute or two)."
echo "    open:    http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT_NOW:-4000}"
echo "    logs:    journalctl -u leapmotor-mate -f"
echo "    status:  sudo mate-server status"
