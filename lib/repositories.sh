#!/usr/bin/env bash

query_repositories() {
    local manager name id state result index=0
    manager=$(meta get "$WORK/discovery.json" package_manager)
    name=$(meta get "$WORK/discovery.json" runtime.owner)
    mkdir "$WORK/repository-results" "$WORK/repository-cache"
    meta repository-config "$manager" >"$WORK/repository-config.json"
    if ((OFFLINE)); then
        meta repositories "$WORK" offline >"$WORK/repositories.json"
        return
    fi
    if [[ $manager == rpm ]]; then
        if command -v dnf >/dev/null 2>&1; then
            REPO_TOOL=dnf
        elif command -v repoquery >/dev/null 2>&1; then
            REPO_TOOL=repoquery
        else
            log "RPM repository inspection unavailable: install repoquery/yum-utils explicitly. No Yum options will be guessed."
            meta repositories "$WORK" unavailable >"$WORK/repositories.json"
            return
        fi
        meta repository-ids "$WORK/repository-config.json" >"$WORK/repository-ids"
        while IFS= read -r id; do
            [[ -n $id ]] || continue
            index=$((index + 1))
            state=available
            if rpm_query "$id" candidates "$name" >"$WORK/repository-results/$index.out" 2>"$WORK/repository-results/$index.err"; then
                debug "Queried repository: $id"
            else
                result=$?
                state=unavailable
                log "Repository $id failed ($result); continuing with other configured repositories."
            fi
            printf '%s\t%s\n' "$id" "$state" >"$WORK/repository-results/$index.state"
        done <"$WORK/repository-ids"
    else
        if ! command -v apt-cache >/dev/null 2>&1; then
            meta repositories "$WORK" unavailable >"$WORK/repositories.json"
            return
        fi
        state=available
        if ! apt-cache policy "$name" >"$WORK/repository-results/1.out" 2>"$WORK/repository-results/1.err"; then
            state=unavailable
            log "APT candidate inspection failed; source acquisition will require exact authenticated packaging."
        fi
        printf 'apt-configured-indices\t%s\n' "$state" >"$WORK/repository-results/1.state"
    fi
    meta repositories "$WORK" queried >"$WORK/repositories.json"
}

rpm_query() {
    local id=$1 operation=$2 identifier=$3
    local -a options=(
        "--setopt=cachedir=$WORK/repository-cache"
        "--setopt=persistdir=$WORK/repository-cache"
        "--setopt=plugins=0"
        "--setopt=timeout=15" "--setopt=retries=2"
        "--setopt=sslverify=1" "--setopt=$id.sslverify=1"
        "--setopt=reposdir=/etc/yum.repos.d"
        "--disablerepo=*" "--enablerepo=$id"
    )
    if [[ $REPO_TOOL == dnf ]]; then
        options+=("--setopt=logdir=$WORK/repository-cache")
    else
        options+=("--setopt=logfile=$WORK/repository-cache/yum.log")
    fi
    if ((IPV4)); then options+=("--setopt=ip_resolve=4"); fi
    if [[ $REPO_TOOL == dnf ]]; then
        if [[ $operation == providers ]]; then
            dnf "${options[@]}" -q repoquery --whatprovides "$identifier"
        elif [[ $operation == reverse ]]; then
            dnf "${options[@]}" -q repoquery --installed --whatrequires --alldeps "$identifier"
        elif [[ $operation == sources ]]; then
            dnf "${options[@]}" -q repoquery --arch=src --location "$identifier"
        else
            dnf "${options[@]}" -q repoquery --qf '%{name}\t%{epoch}\t%{version}\t%{release}\t%{arch}\t%{sourcerpm}' "$identifier"
        fi
    elif [[ $operation == providers ]]; then
        repoquery "${options[@]}" --whatprovides "$identifier"
    elif [[ $operation == reverse ]]; then
        repoquery "${options[@]}" --installed --whatrequires --alldeps "$identifier"
    elif [[ $operation == sources ]]; then
        repoquery "${options[@]}" --show-duplicates --archlist=src --location "$identifier"
    else
        repoquery "${options[@]}" --qf '%{name}\t%{epoch}\t%{version}\t%{release}\t%{arch}\t%{sourcerpm}' "$identifier"
    fi
}

query_build_dependencies() {
    local capability id state index=0
    mkdir "$BUILD_DIR/dependency-queries"
    if [[ -z ${REPO_TOOL:-} ]]; then
        log "Host repository-query tool unavailable; isolated mock dependency resolution remains authoritative."
        printf '%s\n' unavailable >"$BUILD_DIR/dependency-queries/status"
        return
    fi
    meta repository-ids "$WORK/repository-config.json" all >"$WORK/dependency-repository-ids"
    while IFS= read -r capability; do
        while IFS= read -r id; do
            [[ -n $id ]] || continue
            index=$((index + 1))
            state=available
            if ! rpm_query "$id" providers "$capability" >"$BUILD_DIR/dependency-queries/$index.providers" \
                2>"$BUILD_DIR/dependency-queries/$index.errors"; then
                state=unavailable
                log "Capability lookup failed for $id: $capability; other repositories will still be queried."
            fi
            if ! rpm_query "$id" reverse "$capability" >"$BUILD_DIR/dependency-queries/$index.reverse" \
                2>>"$BUILD_DIR/dependency-queries/$index.errors"; then
                log "Reverse-dependency query unavailable for $id: $capability"
            fi
            printf '%s\t%s\t%s\n' "$id" "$capability" "$state" >"$BUILD_DIR/dependency-queries/$index.state"
        done <"$WORK/dependency-repository-ids"
    done <"$BUILD_DIR/build-requires.txt"
    log "Enabled and disabled repository capability/reverse-dependency reports recorded. No host packages were swapped."
}
