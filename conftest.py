# Keeps `src` on sys.path for pytest runs started from the repository root, and
# supplies placeholder recipients so the suite never depends on real addresses.
import os
import sys
from pathlib import Path

os.environ.setdefault(
    "VCA_EMAIL_RECIPIENTS", "recipient-one@example.test,recipient-two@example.test"
)

SRC = Path(__file__).parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
