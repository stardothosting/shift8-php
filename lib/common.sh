#!/usr/bin/env bash

meta() {
    "${METADATA_PYTHON:-python3}" "$BASE_DIR/lib/metadata.py" "$@"
}

source_meta() {
    "${METADATA_PYTHON:-python3}" "$BASE_DIR/lib/source_metadata.py" "$@"
}

select_metadata_python() {
    local candidate
    for candidate in python3 python2.7 python2 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(not ((2, 7) <= sys.version_info < (3, 0) or sys.version_info >= (3, 6)))'; then
            METADATA_PYTHON=$candidate
            return
        fi
    done
    die 3 "Metadata parsing requires Python 2.7 or Python 3.6+. No interpreter will be installed automatically."
}

log() {
    printf '[shift8-php] %s\n' "$*" >&2
    if [[ -n ${LOG_FILE:-} ]]; then
        printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$OPERATION" "$*" >>"$LOG_FILE"
    fi
}

debug() {
    if ((VERBOSE)); then log "$*"; fi
}

die() {
    local code=$1
    shift
    log "ERROR: $*"
    if (( ${HOST_MUTATION_POSSIBLE:-0} )); then
        log "A managed transaction was requested; inspect its durable journal before retrying or rolling back. Target PHP/service state may have changed."
    else
        log "Installed PHP was not modified; no services were restarted."
    fi
    if [[ -n ${LOG_FILE:-} && $LOG_FILE != "${WORK:-}/"* ]]; then
        log "Diagnostics: $LOG_FILE"
    fi
    exit "$code"
}

failed() {
    local code=$1 line=$2
    trap - ERR
    if [[ -n ${LOG_FILE:-} && -f $LOG_FILE ]]; then
        tail -n 30 "$LOG_FILE" >&2
    fi
    die "$code" "$OPERATION failed at shell line $line. Review the diagnostic above; incomplete inputs must not be used."
}

require() {
    command -v "$1" >/dev/null 2>&1 || die 3 "Required command is missing: $1. Install it explicitly in a suitable environment."
}

cleanup() {
    local code=$?
    trap - EXIT
    if [[ -n ${BUILD_PID:-} ]]; then
        kill -TERM -- "-$BUILD_PID" 2>/dev/null || log "Build process group already stopped."
        wait "$BUILD_PID" 2>/dev/null || true
    fi
    if [[ -n ${WORK:-} && -d $WORK && ${WORK##*/} == shift8-php.* ]]; then
        rm -rf -- "$WORK"
    fi
    exit "$code"
}

capture() {
    local destination=$1
    shift
    debug "Inspecting with $1"
    "$@" >"$destination" 2>>"$LOG_FILE"
}

harden_artifact_tree() {
    local root=$1
    [[ -d $root ]] || return 0
    find "$root" -xdev -exec chmod u+rwX,go-rwx {} +
}
