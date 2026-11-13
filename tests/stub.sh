#!/usr/bin/env bash
printf '\033[1;34m==>\033[0m avvio stub args: %s\n' "$*"
sleep 1
printf '\033[1;33m!!\033[0m  attenzione di prova <b>&amp;\n' >&2
sleep 1
echo "riga finale"
exit "${STUB_RC:-0}"
