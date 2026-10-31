#!/bin/sh
# osk-system CLI launcher. Runs osk.cli with this vault's own Python and the engine on PYTHONPATH.
#   sh osk-cli.sh validate | status | search "words" | update --apply | ...
# Run it with sh: a release carries file contents only, not the executable bit (Mechanism §1-2).
d=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$d/_governance/_engine" exec "$d/.venv/bin/python" -m osk.cli "$@"
