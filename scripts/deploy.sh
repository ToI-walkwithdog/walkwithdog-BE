#!/bin/bash -p
set -euo pipefail
umask 077
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT

for path in / /opt /opt/toi /opt/toi/scripts /opt/toi/scripts/deployment_common.sh; do
    if [[ $EUID != 0 || -L $path || ! -e $path ]] ||
        ! attrs=$(/usr/bin/stat -c '%u %g %a' -- "$path") ||
        ! [[ $attrs =~ ^0\ 0\ ([0-7]{3,4})$ ]] ||
        (( (8#${BASH_REMATCH[1]} & 8#7022) != 0 )); then
        printf '%s\n' 'Unsafe deployment executable path or caller.' >&2
        exit 3
    fi
done
exec /opt/toi/scripts/deployment_common.sh deploy "$@"
