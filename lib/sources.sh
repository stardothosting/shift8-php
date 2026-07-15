#!/usr/bin/env bash

acquire_sources() {
    local version url checksum archive manager
    version=$(meta get "$WORK/release.json" version)
    url=$(meta get "$WORK/release.json" source.url)
    checksum=$(meta get "$WORK/release.json" source.sha256)
    archive=$ARTIFACT_DIR/cache/php-$version.tar.xz
    fetch "$url" "$archive" "$checksum"
    meta archive "$archive" "$version"
    mkdir "$BUILD_DIR/sources"
    cp "$archive" "$BUILD_DIR/sources/php-$version.tar.xz"
    cp "$archive.receipt.json" "$BUILD_DIR/sources/php-$version.tar.xz.receipt.json"
    manager=$(meta get "$WORK/discovery.json" package_manager)
    if [[ -n $SOURCE_MAP ]]; then
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

acquire_srpm() {
    local source id candidate package_file identity url=''
    source=$(meta get "$WORK/discovery.json" family.source_reference)
    meta filename "$source"
    package_file=$ARTIFACT_DIR/cache/$source
    if [[ ! -f $package_file ]]; then
        ((!OFFLINE)) || die 6 "Exact source RPM is not cached: $source. Supply a reviewed --source-map or retrieve it from authenticated vendor metadata."
        [[ -n ${REPO_TOOL:-} ]] || die 6 "No source repository query tool is available. Supply a reviewed --source-map; no guessed vendor URLs will be tried."
        meta repository-ids "$WORK/repository-config.json" all >"$WORK/source-repository-ids"
        while IFS= read -r id; do
            [[ -n $id ]] || continue
            if ! rpm_query "$id" sources "${source%.src.rpm}" >"$WORK/source-urls" 2>>"$LOG_FILE"; then
                log "Source query failed for $id; continuing."
                continue
            fi
            while IFS= read -r candidate; do
                if [[ $candidate == https://* ]]; then
                    if (fetch "$candidate" "$package_file"); then
                        url=$candidate
                        break
                    fi
                    log "Source transfer failed for $id; continuing with other configured locations."
                fi
            done <"$WORK/source-urls"
            if [[ -n ${url:-} ]]; then break; fi
        done <"$WORK/source-repository-ids"
        [[ -n ${url:-} ]] || die 6 "Exact SRPM $source is unavailable via configured HTTPS repository metadata. Use a reviewed --source-map for individual vendor files."
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
