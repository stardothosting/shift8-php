#!/usr/bin/env bash

fetch() {
    local url=$1 destination=$2 expected=${3:-} temporary attempt result family=
    meta https "$url" || die 5 "Refusing an invalid HTTPS source URL."
    if [[ -f $destination ]]; then
        if meta cache-check "$destination" "$url" "$expected"; then
            if ((OFFLINE)) || [[ -n $expected ]]; then return 0; fi
        elif ((OFFLINE)); then
            die 5 "Offline cache failed validation: $destination"
        fi
    fi
    ((!OFFLINE)) || die 5 "Verified offline input is missing: $destination"
    if [[ -n $expected ]]; then
        temporary=$destination.part-$expected
        meta partial "$temporary" "$expected"
    else
        temporary=$(mktemp "$destination.part.XXXXXXXX") || die 5 "Cannot create a private download temporary file."
    fi
    if ((IPV4)); then family=-4; fi
    for attempt in 0 1 2; do
        debug "HTTPS attempt $((attempt + 1)): $url"
        local -a request=(curl --disable --fail --silent --show-error
            --location --max-redirs 5 --proto '=https' --proto-redir '=https'
            --connect-timeout 10 --max-time 300 --max-filesize 536870912)
        if [[ -n $family ]]; then request+=("$family"); fi
        if [[ -n $expected && -s $temporary ]]; then request+=(--continue-at -); fi
        if "${request[@]}" --output "$temporary" "$url" 2>>"$LOG_FILE"; then
            if [[ ! -s $temporary ]]; then
                rm -f -- "$temporary"
                die 5 "Empty HTTPS response: $url"
            fi
            if ! meta cache-write "$temporary" "$destination" "$url" "$expected"; then
                rm -f -- "$temporary"
                die 5 "Downloaded content failed verification: $url"
            fi
            return 0
        else
            result=$?
        fi
        case "$result" in
            5 | 6 | 7 | 18 | 28 | 35 | 52 | 55 | 56)
                family=-4
                log "Network failure ($result); retrying with IPv4 where necessary."
                ;;
            22)
                log "HTTP failure fetching $url; retry is bounded."
                ;;
            33)
                log "Server declined a range request; restarting this checksum-pinned download."
                rm -f -- "$temporary"
                ;;
            *)
                rm -f -- "$temporary"
                die 5 "HTTPS transfer failed (curl $result): $url. TLS verification will not be disabled."
                ;;
        esac
        if ((attempt < 2)); then sleep "$((1 << attempt))"; fi
    done
    if [[ -z $expected ]]; then rm -f -- "$temporary"; fi
    die 5 "HTTPS retries exhausted: $url. Checksum-pinned partial content, if present, can be resumed and must pass final verification."
}

select_release() {
    local branch major cache
    branch=$(meta get "$WORK/discovery.json" runtime.branch)
    major=${branch%%.*}
    if ((DRY_RUN && !OFFLINE)); then
        cache=$WORK
    else
        cache=$ARTIFACT_DIR/cache
    fi
    fetch "https://www.php.net/releases/index.php?json&version=$major" "$cache/support-$major.json"
    fetch "https://www.php.net/releases/index.php?json&version=$branch&max=100" "$cache/releases-$branch.json"
    cp "$cache/support-$major.json" "$WORK/support.json"
    cp "$cache/support-$major.json.receipt.json" "$WORK/support.json.receipt.json"
    cp "$cache/releases-$branch.json" "$WORK/releases.json"
    cp "$cache/releases-$branch.json.receipt.json" "$WORK/releases.json.receipt.json"
    meta select "$WORK/discovery.json" "$cache/support-$major.json" \
        "$cache/releases-$branch.json" "$TARGET" >"$WORK/release.json"
    log "Selected official PHP $(meta get "$WORK/release.json" version) in branch $branch; no CVE/backport claim inferred from version alone."
}
