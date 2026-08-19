#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/shift8-legacy-cli.XXXXXXXX")
export ROOT FIXTURE
trap 'rm -rf -- "$FIXTURE"' EXIT
mkdir "$FIXTURE/bin"
cat >"$FIXTURE/bin/python3" <<'EOF'
#!/bin/sh
exit 1
EOF
cat >"$FIXTURE/bin/rpm" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    -qa) cat "$ROOT/tests/fixtures/remi-el7.tsv" ;;
    -qf) printf 'php-cli\n' ;;
    -ql)
        if [[ ${!#} == php-cli ]]; then printf '%s\n' "$FIXTURE/bin/php"; fi
        ;;
    -q)
        if [[ $* == *--qf* ]]; then printf 'php-cli\n'
        elif [[ $* == *--requires* || $* == *--provides* ]]; then printf 'php(api) = 20220829\n'
        fi
        ;;
    *) exit 1 ;;
esac
EOF
cat >"$FIXTURE/bin/php" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    -i)
        printf '%s\n' 'PHP Version => 8.2.20' 'PHP API => 20220829' \
            'PHP Extension => 20220829' 'Zend Extension => 420220829' \
            'Thread Safety => disabled' 'extension_dir => /usr/lib64/php/modules'
        ;;
    -m) printf '[PHP Modules]\nCore\nredis\n' ;;
    --ini) printf 'Loaded Configuration File: /etc/php.ini\n' ;;
    *) exit 1 ;;
esac
EOF
chmod +x "$FIXTURE/bin/python3" "$FIXTURE/bin/rpm" "$FIXTURE/bin/php"
export PATH="$FIXTURE/bin:$PATH"
bash "$ROOT/shift8-php" discover php-cli >"$FIXTURE/discovery.json"
python - "$FIXTURE/discovery.json" <<'PY'
import json, sys
assert sys.version_info[:2] == (2, 7), "Run this smoke with genuine Python 2.7"
with open(sys.argv[1]) as stream:
    data = json.load(stream)
assert data["runtime"]["version"] == "8.2.20"
assert data["runtime"]["module_api"] == "20220829"
assert data["runtime"]["zend_extension_api"] == "420220829"
print("Full CLI discovery passed with Python 3 unavailable")
PY
