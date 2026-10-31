"""Entry point inside the installed folder: python\\python.exe -I mate-server.py <command> ...

Run with -I (isolated) so nothing on the machine — a PYTHONPATH, a PYTHONHOME, a user site-packages
folder — can change what this bundled interpreter imports. The shell's own code is put on the path
explicitly instead.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
os.environ.setdefault("MATE_UV", os.path.join(HERE, "bin", "uv.exe"))

from mate_server.__main__ import main  # noqa: E402

sys.exit(main())
