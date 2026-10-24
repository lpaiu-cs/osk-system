#!/bin/sh
# osk-system CLI launcher. Runs osk.cli with this vault's own Python and the engine on PYTHONPATH.
#   ./osk-cli.sh validate | status | search "words" | update --apply | ...
d=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$d/_governance/_engine" exec "$d/.venv/bin/python" -m osk.cli "$@"
