"""LeapMotor Mate server — runs Mate as a self-updating service on Linux and Windows.

The server is a SHELL around Mate, in the same sense as MateDesktop: Mate itself (web/ + poller/
from the leapmotor-mate repository) is the PAYLOAD and is never modified. The shell downloads it,
gives each payload version a Python environment that satisfies its requirements, runs the poller and
the web server as children, honours Mate's relaunch code, and swaps in new releases as they appear.
"""

# The shell's own version, independent of Mate's — the two ship on different schedules.
SERVER_VERSION = "0.1.0"
