#!/usr/bin/env bash

build_meta() {
    "${METADATA_PYTHON:-python3}" "$BASE_DIR/lib/build_metadata.py" "$@"
}

build_phase() {
    local phase=$1 code started last_update now
    shift
    BUILD_DIR=${BUILD_DIR:?Build directory is required}
    build_meta check-inputs "$BUILD_DIR"
    OPERATION=$phase
    log "Build phase: $OPERATION"
    printf '%s\n' "$phase" >"$BUILD_DIR/build-state"
    started=$SECONDS
    last_update=$SECONDS
    setsid "$@" >"$BUILD_DIR/build-logs/$phase.stdout" 2>"$BUILD_DIR/build-logs/$phase.stderr" &
    BUILD_PID=$!
    while kill -0 "$BUILD_PID" 2>/dev/null; do
        sleep 1
        now=$SECONDS
        if ((now - last_update >= 10)); then
            log "$phase active (pid $BUILD_PID, $((now - started))s); recent output:"
            tail -n 5 "$BUILD_DIR/build-logs/$phase.stdout" >&2
            tail -n 5 "$BUILD_DIR/build-logs/$phase.stderr" >&2
            if command -v ps >/dev/null 2>&1; then
                ps -p "$BUILD_PID" -o pid=,etime=,pcpu=,pmem=,stat= >&2 || log "Process exited while sampling activity."
            fi
            df -Pk "$BUILD_DIR" >&2
            last_update=$now
        fi
    done
    if wait "$BUILD_PID"; then code=0; else code=$?; fi
    BUILD_PID=''
    harden_artifact_tree "$BUILD_DIR"
    tail -n 10 "$BUILD_DIR/build-logs/$phase.stdout" >&2
    tail -n 10 "$BUILD_DIR/build-logs/$phase.stderr" >&2
    printf '%s\t%s\t%s\n' "$phase" "$code" "$((SECONDS - started))" >>"$BUILD_DIR/build-phases.tsv"
    if ((code)); then
        printf 'failed:%s\n' "$phase" >"$BUILD_DIR/build-state"
        build_meta failure "$BUILD_DIR" "$phase" "$code"
        harden_artifact_tree "$BUILD_DIR"
        die 7 "$phase failed (exit $code). Logs: $BUILD_DIR/build-logs. Review dependency conflicts or exact rejected patch hunks; do not discard patches or force package operations."
    fi
    build_meta check-inputs "$BUILD_DIR"
}

build_rpm() {
    local config spec item cc cxx compiler_path jobs unique input_id final_id option
    local -a mock_options compiler_environment
    require mock
    require rpm
    require df
    ((!OFFLINE)) || die 3 "Offline mock builds are disabled: a cached isolated buildroot cannot yet be authenticated."
    capture "$WORK/mock-help" mock --help
    for option in --init --uniqueext --resultdir --chroot --copyin --copyout --installdeps --clean; do
        grep -F -- "$option" "$WORK/mock-help" >/dev/null || die 3 "Installed mock lacks required capability $option. Use a compatible operator-configured build host."
    done
    build_meta stage "$BUILD_PROFILE" "$BUILD_DIR"
    mkdir "$BUILD_DIR/build-logs" "$BUILD_DIR/packages"
    input_id=$(build_meta input-id "$BUILD_DIR/build-inputs")
    printf '%s\n' "$input_id" >"$BUILD_DIR/build-input-id"
    config=$(meta get "$BUILD_DIR/build-inputs/profile.json" mock_config.path)
    cc=$(meta get "$BUILD_DIR/build-inputs/profile.json" compiler.cc)
    cxx=$(meta get "$BUILD_DIR/build-inputs/profile.json" compiler.cxx)
    compiler_path=${cc%/*}
    jobs=$(meta get "$BUILD_DIR/build-inputs/profile.json" jobs)
    unique=shift8-${BUILD_DIR##*/}-$input_id
    printf '%s\n' "$unique" >"$BUILD_DIR/mock-root-id"
    mock_options=(-r "$config" "--uniqueext=$unique" "--resultdir=$BUILD_DIR/build-logs")
    compiler_environment=(/usr/bin/env "PATH=$compiler_path:/usr/bin:/bin" "CC=$cc" "CXX=$cxx")
    build_phase mock-version mock "${mock_options[@]}" --version
    build_phase init mock "${mock_options[@]}" --init
    build_phase toolchain-packages mock "${mock_options[@]}" --chroot -- rpm -qa \
        --qf '%{NAME}\t%{EPOCH}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n'
    build_phase cc mock "${mock_options[@]}" --chroot -- "$cc" -dumpversion
    build_phase cxx mock "${mock_options[@]}" --chroot -- "$cxx" -dumpversion
    build_meta compiler "$BUILD_DIR"
    build_phase directories mock "${mock_options[@]}" --chroot -- mkdir -p \
        /builddir/build/SOURCES /builddir/build/SPECS /builddir/build/BUILD \
        /builddir/build/RPMS /builddir/build/SRPMS
    spec=$(meta spec-name "$BUILD_DIR/build-inputs/profile.json")
    for item in "$BUILD_DIR/build-inputs/"*; do
        [[ -f $item ]] || continue
        case "${item##*/}" in profile.json | audit.json) continue ;; esac
        build_phase "copy-${item##*/}" mock "${mock_options[@]}" --copyin "$item" "/builddir/build/SOURCES/${item##*/}"
    done
    build_phase spec mock "${mock_options[@]}" --chroot -- rpmspec --parse "/builddir/build/SOURCES/$spec"
    build_phase spec-identity mock "${mock_options[@]}" --chroot -- rpmspec -q --srpm \
        --qf '%{NAME}\t%{VERSION}\n' "/builddir/build/SOURCES/$spec"
    build_phase buildrequires mock "${mock_options[@]}" --chroot -- rpmspec -q --buildrequires "/builddir/build/SOURCES/$spec"
    build_meta spec "$BUILD_DIR" >"$BUILD_DIR/build-requires.txt"
    query_build_dependencies
    build_phase dependency-srpm mock "${mock_options[@]}" --chroot -- "${compiler_environment[@]}" \
        rpmbuild -bs --define '_topdir /builddir/build' "/builddir/build/SOURCES/$spec"
    mkdir "$BUILD_DIR/dependency-srpm"
    build_phase collect-dependency-srpm mock "${mock_options[@]}" --copyout /builddir/build/SRPMS "$BUILD_DIR/dependency-srpm/"
    meta flatten-rpms "$BUILD_DIR/dependency-srpm"
    local -a dependency_sources
    shopt -s nullglob
    dependency_sources=("$BUILD_DIR/dependency-srpm/"*.src.rpm)
    shopt -u nullglob
    [[ ${#dependency_sources[@]} -eq 1 ]] || die 7 "Expected exactly one final-spec SRPM for isolated dependency resolution."
    build_phase dependencies mock "${mock_options[@]}" --installdeps "${dependency_sources[0]}"
    build_phase resolved-packages mock "${mock_options[@]}" --chroot -- rpm -qa \
        --qf '%{NAME}\t%{EPOCH}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n'
    build_phase prep mock "${mock_options[@]}" --chroot -- "${compiler_environment[@]}" \
        rpmbuild -bp --define '_topdir /builddir/build' --define '__patch /usr/bin/patch --fuzz=0' \
        "/builddir/build/SOURCES/$spec"
    if grep -Ei 'with fuzz|FAILED|saving rejects|Reversed .*patch' "$BUILD_DIR/build-logs/prep."*; then
        die 7 "%prep reported fuzz/rejected/reversed patches. Review original versus target context; no patch will be silently dropped."
    fi
    build_phase compile mock "${mock_options[@]}" --chroot -- "${compiler_environment[@]}" \
        rpmbuild -ba --define '_topdir /builddir/build' --define '__patch /usr/bin/patch --fuzz=0' \
        --define "_smp_mflags -j$jobs" "/builddir/build/SOURCES/$spec"
    build_phase regenerate-source mock "${mock_options[@]}" --chroot -- "${compiler_environment[@]}" \
        rpmbuild -bs --define '_topdir /builddir/build' "/builddir/build/SOURCES/$spec"
    build_phase collect-binary mock "${mock_options[@]}" --copyout /builddir/build/RPMS "$BUILD_DIR/packages/"
    build_phase collect-source mock "${mock_options[@]}" --copyout /builddir/build/SRPMS "$BUILD_DIR/packages/"
    meta flatten-rpms "$BUILD_DIR/packages"
    build_meta artifacts "$BUILD_DIR"
    final_id=$(build_meta input-id "$BUILD_DIR/build-inputs")
    [[ $final_id == "$input_id" ]] || die 7 "Build inputs changed; all generated artifacts are invalid and must be rebuilt."
    build_phase cleanup mock "${mock_options[@]}" --clean
    printf '%s\n' static-build-complete-runtime-unverified >"$BUILD_DIR/build-state"
    log "Isolated build completed; all outputs are local unsigned artifacts, not deployable packages."
}
