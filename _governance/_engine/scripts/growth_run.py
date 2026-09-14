"""Native scheduler entry; use this instance's engine without shell PYTHONPATH."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osk.cli import main

if __name__ == "__main__":
    main(["growth", "run", *sys.argv[1:]])
