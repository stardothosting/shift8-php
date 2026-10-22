#!/usr/bin/env bash

acquire_upstream() {
    local version url checksum archive
    version=$(meta get "$WORK/release.json" version)
    url=$(meta get "$WORK/release.json" source.url)
    checksum=$(meta get "$WORK/release.json" source.sha256)
    archive=$ARTIFACT_DIR/cache/php-$version.tar.xz
    fetch "$url" "$archive" "$checksum"
    meta archive "$archive" "$version"
    mkdir "$BUILD_DIR/sources"
    cp "$archive" "$BUILD_DIR/sources/php-$version.tar.xz"
    cp "$archive.receipt.json" "$BUILD_DIR/sources/php-$version.tar.xz.receipt.json"
}

acquire_sources() {
    local manager
    acquire_upstream
    manager=$(meta get "$WORK/discovery.json" package_manager)
    if [[ -n ${BUILD_PROFILE:-} ]]; then
        build_meta packaging "$BUILD_PROFILE" "$BUILD_DIR"
        log "Using checksum-pinned final packaging from the reviewed build profile; historical source discovery is unnecessary."
        if [[ -n $SOURCE_MAP ]]; then
            log "The complete build profile takes precedence over --source-map for this isolated build."
        fi
    elif [[ -n $SOURCE_MAP ]]; then
        acquire_map
    elif [[ $manager == rpm ]]; then
        acquire_srpm
    else
        acquire_debian
    fi
}

acquire_map() {
    local filename url checksum
    meta source-map "$SOURCE_MAP" "$WORK/discovery.json" >"$WORK/source-map.tsv"
    mkdir "$BUILD_DIR/sources/packaging"
    cp "$SOURCE_MAP" "$BUILD_DIR/sources/packaging/source-map.json"
    while IFS=$'\t' read -r filename url checksum; do
        fetch "$url" "$ARTIFACT_DIR/cache/map-$checksum-$filename" "$checksum"
        cp "$ARTIFACT_DIR/cache/map-$checksum-$filename" "$BUILD_DIR/sources/packaging/$filename"
    done <"$WORK/source-map.tsv"
    printf '%s\n' operator-reviewed-checksum-map >"$BUILD_DIR/sources/packaging-trust"
    log "Packaging is operator-attested, not vendor-authenticated. Declared-file completeness still requires an isolated packaging review."
}

acquire_provider_packaging() {
    local tree_url tree_page filename file_url count=0
    tree_url=$(meta provider-packaging-tree "$WORK/discovery.json" "$BUILD_DIR/source-discovery/plan.json")
    [[ -n $tree_url ]] || return 1
    tree_page=$BUILD_DIR/source-discovery/provider-packaging-tree.html
    log "Falling back to the provider's historical packaging tag: $tree_url"
    if ! (fetch "$tree_url" "$tree_page" '' 1048576); then
        log "Provider packaging tree could not be read; no provider packaging-tag fallback is available."
        return 1
    fi
    mkdir -p "$BUILD_DIR/sources/packaging"
    meta provider-packaging-files "$tree_page" "$tree_url" >"$BUILD_DIR/source-discovery/provider-packaging.tsv"
    while IFS=$'\t' read -r filename file_url; do
        [[ -n $filename && -n $file_url ]] || continue
        if ! (fetch "$file_url" "$BUILD_DIR/sources/packaging/$filename"); then
            log "A provider packaging file could not be retrieved; discarding the packaging-tag fallback."
            return 1
        fi
        count=$((count + 1))
    done <"$BUILD_DIR/source-discovery/provider-packaging.tsv"
    [[ $count -gt 0 ]] || return 1
    cp "$tree_page" "$BUILD_DIR/sources/packaging/provider-packaging-tree.html"
    cp "$tree_page.receipt.json" "$BUILD_DIR/sources/packaging/provider-packaging-tree.html.receipt.json"
    cp "$BUILD_DIR/source-discovery/provider-packaging.tsv" "$BUILD_DIR/sources/packaging/provider-packaging.tsv"
    printf '%s\n' provider-published-packaging-tag-https >"$BUILD_DIR/sources/packaging-trust"
    log "Retrieved $count packaging files from the provider's historical packaging tag. These files are provider-published over HTTPS, not SRPM-signed."
}

acquire_srpm() {
    local source id candidate package_file identity query_code provider_index provider_page index=0 queries=0 locations=0 url=''
    source=$(meta get "$WORK/discovery.json" family.source_reference)
    meta filename "$source"
    package_file=$ARTIFACT_DIR/cache/$source
    if [[ ! -f $package_file ]]; then
        ((!OFFLINE)) || die 6 "Exact source RPM is not cached: $source. Supply a reviewed --source-map or retrieve it from authenticated vendor metadata."
        mkdir "$BUILD_DIR/source-discovery"
        meta source-repositories "$WORK/discovery.json" "$WORK/repository-config.json" \
            "$WORK/repositories.json" >"$BUILD_DIR/source-discovery/plan.json"
        {
            meta get "$BUILD_DIR/source-discovery/plan.json" source_repositories
            meta get "$BUILD_DIR/source-discovery/plan.json" mixed_repositories
        } >"$WORK/source-repository-ids"
        if [[ -z ${REPO_TOOL:-} ]]; then
            log "Native source query tool is unavailable; only a supported provider's published source index can be checked."
            : >"$WORK/source-repository-ids"
        fi
        if [[ -z $(meta get "$BUILD_DIR/source-discovery/plan.json" source_repositories) ]]; then
            log "No provider source repository is configured. Only exact-match binary indexes will be checked for mixed source metadata."
        fi
        while IFS= read -r id; do
            [[ -n $id ]] || continue
            index=$((index + 1))
            queries=$((queries + 1))
            log "Looking for exact source package in $id: $source"
            if rpm_query "$id" sources "${source%.src.rpm}" >"$BUILD_DIR/source-discovery/$index.locations" \
                2>"$BUILD_DIR/source-discovery/$index.stderr"; then
                query_code=0
            else
                query_code=$?
            fi
            printf '%s\t%s\n' "$id" "$query_code" >"$BUILD_DIR/source-discovery/$index.query"
            cat "$BUILD_DIR/source-discovery/$index.stderr" >>"$LOG_FILE"
            if ((query_code)); then
                log "Source query failed for $id (exit $query_code); details: $BUILD_DIR/source-discovery/$index.stderr"
                tail -n 3 "$BUILD_DIR/source-discovery/$index.stderr" >&2
                continue
            fi
            while IFS= read -r candidate; do
                [[ -n $candidate ]] || continue
                if ! url=$(meta source-location "$candidate" "$source" 2>>"$LOG_FILE"); then
                    log "Ignoring an invalid or non-matching SRPM location from $id; see $LOG_FILE."
                    url=''
                    continue
                fi
                locations=$((locations + 1))
                if [[ $candidate == http://* ]]; then
                    log "$id advertised HTTP; trying the same source location over HTTPS."
                fi
                if (fetch "$url" "$package_file"); then
                    break
                fi
                log "Source transfer failed for $id; continuing with other configured locations."
                url=''
            done <"$BUILD_DIR/source-discovery/$index.locations"
            if [[ -n ${url:-} ]]; then break; fi
            if [[ ! -s $BUILD_DIR/source-discovery/$index.locations ]]; then
                log "$id returned no locations for the exact SRPM."
            fi
        done <"$WORK/source-repository-ids"
        if [[ -z $url ]]; then
            provider_index=$(meta provider-source-index "$WORK/discovery.json" "$BUILD_DIR/source-discovery/plan.json")
            if [[ -n $provider_index ]]; then
                provider_page=$BUILD_DIR/source-discovery/provider-index.html
                log "Checking the provider's package-specific source index: $provider_index"
                if (fetch "$provider_index" "$provider_page" '' 524288); then
                    meta provider-source-locations "$provider_page" "$provider_index" "$source" \
                        >"$BUILD_DIR/source-discovery/provider.locations"
                    while IFS= read -r candidate; do
                        [[ -n $candidate ]] || continue
                        locations=$((locations + 1))
                        if (fetch "$candidate" "$package_file"); then
                            url=$candidate
                            break
                        fi
                        log "Provider source download failed; checking other exact-match locations."
                    done <"$BUILD_DIR/source-discovery/provider.locations"
                    if [[ -z $url ]]; then
                        log "The provider's current source page did not yield a retrievable copy of $source. Newer or differently named SRPMs will not be substituted."
                    fi
                else
                    log "Provider source index could not be read; no source authenticity or availability claim can be made."
                fi
            fi
        fi
        if [[ -z ${url:-} ]] && acquire_provider_packaging; then
            return 0
        fi
        [[ -n ${url:-} ]] || die 6 "Could not retrieve exact SRPM $source: $queries relevant indexes queried, $locations exact locations returned. See $BUILD_DIR/source-discovery/. A binary repository's SOURCERPM field does not provide a source download URL. The provider packaging-tag fallback also failed. Obtain the exact signed historical SRPM from the vendor, configure a trusted source index, or use --source-map with reviewed historical packaging. Unrelated archived repositories were not enabled."
    fi
    url=$(meta get "$package_file.receipt.json" url)
    meta cache-check "$package_file" "$url" "$(meta get "$package_file.receipt.json" sha256)"
    capture "$BUILD_DIR/sources/srpm-signature.txt" rpm --checksig --verbose "$package_file"
    meta rpm-signature "$BUILD_DIR/sources/srpm-signature.txt"
    identity=$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.src.rpm\t%{SOURCEPACKAGE}\n' "$package_file")
    [[ $identity == "$source"$'\t1' ]] || die 6 "Authenticated source RPM does not match installed metadata: $identity"
    cp "$package_file" "$BUILD_DIR/sources/$source"
    cp "$package_file.receipt.json" "$BUILD_DIR/sources/$source.receipt.json"
    capture "$BUILD_DIR/sources/srpm-files.txt" rpm -qpl "$package_file"
    printf '%s\n' rpm-signature-verified-with-installed-keys >"$BUILD_DIR/sources/packaging-trust"
}

acquire_debian() {
    local source version url filename checksum keyring
    source=$(meta get "$WORK/discovery.json" family.source_name)
    version=$(meta get "$WORK/discovery.json" family.source_version)
    keyring=$SOURCE_KEYRING
    if [[ -z $keyring ]]; then
        for keyring in /usr/share/keyrings/debian-keyring.gpg /usr/share/keyrings/ubuntu-archive-keyring.gpg; do
            [[ ! -f $keyring ]] || break
        done
    fi
    [[ -f $keyring ]] || die 6 "Debian source authentication needs a trusted --source-keyring containing the packaging signer's key. No keys will be downloaded automatically."
    require gpgv
    if ((OFFLINE)); then
        [[ -f $ARTIFACT_DIR/cache/debian-$source-$version.url ]] || die 6 "Exact Debian source URL is not cached."
        IFS= read -r url <"$ARTIFACT_DIR/cache/debian-$source-$version.url"
    else
        require apt-get
        (
            cd "$WORK" || exit
            apt-get -o APT::Get::AllowUnauthenticated=false \
                -o Acquire::AllowInsecureRepositories=false \
                --print-uris --download-only --only-source source "$source=$version"
        ) >"$WORK/source-uris" 2>>"$LOG_FILE"
        url=$(meta dsc-url "$WORK/source-uris")
        printf '%s\n' "$url" >"$ARTIFACT_DIR/cache/debian-$source-$version.url"
    fi
    fetch "$url" "$ARTIFACT_DIR/cache/$source-$version.dsc"
    mkdir "$BUILD_DIR/sources/debian"
    cp "$ARTIFACT_DIR/cache/$source-$version.dsc" "$BUILD_DIR/sources/debian/source.dsc"
    cp "$ARTIFACT_DIR/cache/$source-$version.dsc.receipt.json" "$BUILD_DIR/sources/debian/source.dsc.receipt.json"
    gpgv --keyring "$keyring" --output "$WORK/source.control" \
        "$BUILD_DIR/sources/debian/source.dsc" >"$BUILD_DIR/sources/debian/signature.txt" 2>&1
    meta dsc "$WORK/source.control" "$source" "$version" "$url" >"$WORK/dsc-files"
    while IFS=$'\t' read -r filename url checksum; do
        fetch "$url" "$ARTIFACT_DIR/cache/$checksum-$filename" "$checksum"
        cp "$ARTIFACT_DIR/cache/$checksum-$filename" "$BUILD_DIR/sources/debian/$filename"
    done <"$WORK/dsc-files"
    printf '%s\n' dsc-signature-and-sha256-verified >"$BUILD_DIR/sources/packaging-trust"
}
