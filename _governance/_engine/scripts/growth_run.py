"""Native scheduler entry; use this instance's engine without shell PYTHONPATH.

Without a console (Windows `pythonw.exe`, how `setup --schedule` registers it) the report and
any traceback go to one log file per run under `.osk/growth/scheduler/`; the scheduler keeps
neither stream."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osk.cli import main

if __name__ == "__main__":
    if sys.stdout is None:
        from datetime import datetime
        from osk import core
        logs = core.ROOT / ".osk" / "growth" / "scheduler"
        logs.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(logs / f"{datetime.now():%Y%m%d-%H%M%S}.log", "a", encoding="utf-8")
    main(["growth", "run", *sys.argv[1:]])
