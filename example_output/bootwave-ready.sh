#!/bin/sh
# Installed at ready_script; invoked through the declared shell.
if [ "$#" -ne 2 ]; then exit 64; fi
/bin/waitfor "$1" "$2" || exit 1
[ -e "$1" ] || exit 1
exit 0
