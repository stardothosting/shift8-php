#!/usr/bin/env bash
# dpkg-query, not the shell, expands its metadata template fields.
# shellcheck disable=SC2016

discover() {
    local manager record name path owner fpm
    manager=$(meta manager "$PACKAGE")
    printf '%s\n' "$manager" >"$WORK/manager"
    if [[ $manager == rpm ]]; then
        capture "$WORK/packages.tsv" rpm -qa --qf \
            '%{NAME}\t%{EPOCH}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCERPM}\t%{VENDOR}\n'
    else
        capture "$WORK/packages.tsv" dpkg-query -W \
            '-f=${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\t${db:Status-Status}\t${Maintainer}\n'
    fi
    meta inventory "$manager" "$PACKAGE" "$WORK/packages.tsv" >"$WORK/inventory.json"
    meta family-names "$WORK/inventory.json" >"$WORK/names"
    mkdir "$WORK/package-details"
    while IFS= read -r name; do
        if [[ $manager == rpm ]]; then
            capture "$WORK/package-details/$name.files" rpm -ql -- "$name"
            capture "$WORK/package-details/$name.requires" rpm -q --requires -- "$name"
            capture "$WORK/package-details/$name.provides" rpm -q --provides -- "$name"
            capture "$WORK/package-details/$name.config" rpm -q --configfiles -- "$name"
            capture "$WORK/package-details/$name.scripts" rpm -q --scripts -- "$name"
        else
            capture "$WORK/package-details/$name.files" dpkg-query -L "$name"
            capture "$WORK/package-details/$name.requires" dpkg-query -W '-f=${Depends}\n${Pre-Depends}\n' "$name"
            capture "$WORK/package-details/$name.provides" dpkg-query -W '-f=${Provides}\n' "$name"
            capture "$WORK/package-details/$name.config" dpkg-query -W '-f=${Conffiles}\n' "$name"
        fi
    done <"$WORK/names"
    meta binary "$WORK/inventory.json" "$WORK/package-details" >"$WORK/binary"
    IFS=$'\t' read -r name path <"$WORK/binary"
    [[ -x $path && -f $path ]] || die 3 "Package-owned CLI executable is not available: $path"
    if [[ $manager == rpm ]]; then
        owner=$(rpm -qf --qf '%{NAME}\n' -- "$path")
    else
        owner=$(dpkg-query -S "$path")
        owner=${owner%%: /*}
        owner=${owner%%:*}
    fi
    [[ $owner == "$name" ]] || die 3 "CLI ownership changed or is ambiguous: $path ($owner, expected $name)"
    log "Inspecting package-owned runtime: $path ($name), not the default php command."
    "$path" -i 2>>"$LOG_FILE" | meta phpinfo >"$WORK/phpinfo.json"
    capture "$WORK/modules.txt" "$path" -m
    capture "$WORK/ini.txt" "$path" --ini
    # Optional here; the source-first contract blocks when the constants are missing.
    capture "$WORK/paths.txt" "$path" -n -r "$COMPILED_PATHS_PHP" || : >"$WORK/paths.txt"
    if (( ${SOURCE_FIRST:-0} )); then
        meta snapshot "$WORK" "$PACKAGE" source-first >"$WORK/discovery.json"
        mkdir "$WORK/fpm-details"
        meta get "$WORK/discovery.json" family.fpm_binaries >"$WORK/fpm-binaries"
        local index=0
        while IFS= read -r fpm; do
            [[ -n $fpm ]] || continue
            index=$((index + 1))
            [[ -x $fpm && -f $fpm ]] || die 3 "Package-owned FPM executable is unavailable: $fpm"
            "$fpm" -i 2>>"$LOG_FILE" | meta phpinfo >"$WORK/fpm-details/$index.json"
        done <"$WORK/fpm-binaries"
        source_meta attach-fpm "$WORK/discovery.json" "$WORK/fpm-details"
    else
        meta snapshot "$WORK" "$PACKAGE" >"$WORK/discovery.json"
    fi
    meta libraries "$WORK/discovery.json" >"$WORK/libraries.json"
    meta attach-libraries "$WORK/discovery.json" "$WORK/libraries.json"
    meta get "$WORK/discovery.json" family.names >"$WORK/family-names"
    : >"$WORK/services.txt"
    while IFS= read -r name; do
        while IFS= read -r record; do
            case "$record" in
                /etc/init.d/* | */systemd/system/*.service)
                    printf '%s\n' "$record" >>"$WORK/services.txt"
                    ;;
            esac
        done <"$WORK/package-details/$name.files"
    done <"$WORK/family-names"
    meta finish-snapshot "$WORK/discovery.json" "$WORK/services.txt"
    fpm=$(meta get "$WORK/discovery.json" family.fpm_binaries)
    debug "Discovered FPM paths: ${fpm:-none}; service and web integration are not runtime-verified."
}
