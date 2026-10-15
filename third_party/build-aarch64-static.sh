#!/bin/bash
# Runs on an x86_64 Linux host with aarch64-linux-gnu-gcc/g++, autoconf, automake, libtool and git.
# Builds static aarch64 stress tools. Usage: ./build-aarch64-static.sh   (set http(s)_proxy first if downloads need it)
# Sources: the repositories, tags and commits of versions.lock ("<name> <url> <tag> <commit>" lines), cloned into
# src/; a clone whose commit differs from the lock is not built. Output in out/aarch64/: the stripped binaries
# (stress-ng, stressapptest, cyclictest, hackbench, pi_stress), BUILDINFO.txt (date, compiler, host, per tool: tag,
# commit, flags, disabled features, file type) and SHA256SUMS; build logs build-<tool>.log next to this script.
set -u

# ---- settings --------------------------------------------------------------------------------------------------------
HERE=$(cd "$(dirname "$0")" && pwd)
LOCK=$HERE/versions.lock
SRC=$HERE/src
OUT=$HERE/out/aarch64
CC=aarch64-linux-gnu-gcc
STRIP=aarch64-linux-gnu-strip
JOBS=8   # parallel make jobs
mkdir -p "$SRC" "$OUT"
rm -f "$OUT"/*
: > "$OUT/BUILDINFO.txt"

# ---- helpers ---------------------------------------------------------------------------------------------------------
# info <text...>: append one line to BUILDINFO.txt.
info() { echo "$@" >> "$OUT/BUILDINFO.txt"; }
info "build date: $(date -u +%FT%TZ)"
info "compiler: $($CC --version | head -1)"
info "host: $(uname -srm)"

# lockline <name>: the versions.lock line of <name>.
lockline() { grep -E "^$1 " "$LOCK"; }
info "numactl commit: $(lockline numactl | cut -d' ' -f4) (libnuma.a only, linked into rt-tests)"
clone() { # name -> sets URL TAG SHA, clones at tag, verifies commit
  read -r _ URL TAG SHA < <(lockline "$1")
  rm -rf "$SRC/$1"
  git clone -q "$URL" "$SRC/$1" && git -C "$SRC/$1" checkout -q "$TAG" || return 1
  got=$(git -C "$SRC/$1" rev-parse HEAD)
  [ "$got" = "$SHA" ] || { echo "commit mismatch for $1: $got != $SHA"; return 1; }
}
record() { # tool binary flags [disabled]: append the tool's section to BUILDINFO.txt (uses TAG and SHA from clone)
  info ""
  info "== $1 $TAG $SHA"
  info "flags: $3"
  info "disabled: ${4:-none}"
  info "file: $(file -b "$OUT/$2")"
}

# ---- stress-ng -------------------------------------------------------------------------------------------------------
if clone stress-ng; then
  ( cd "$SRC/stress-ng" && make clean >/dev/null 2>&1
    make CC=$CC STATIC=1 -j$JOBS ) > "$HERE/build-stress-ng.log" 2>&1 \
  && {
    $STRIP -o "$OUT/stress-ng" "$SRC/stress-ng/stress-ng"
    record stress-ng stress-ng "make CC=$CC STATIC=1 -j$JOBS" "${STRESSNG_DISABLED:-none}"
  } \
  || echo "stress-ng FAILED (see build-stress-ng.log)"
fi

# ---- stressapptest ---------------------------------------------------------------------------------------------------
if clone stressapptest; then
  ( cd "$SRC/stressapptest" && ./configure --host=aarch64-linux-gnu CC=$CC CXX=aarch64-linux-gnu-g++ \
      LDFLAGS="-static" CXXFLAGS="-O2 -static" && make -j$JOBS ) > "$HERE/build-stressapptest.log" 2>&1 \
  && {
    $STRIP -o "$OUT/stressapptest" "$SRC/stressapptest/src/stressapptest"
    record stressapptest stressapptest \
      "./configure --host=aarch64-linux-gnu LDFLAGS=-static CXXFLAGS='-O2 -static'; make -j$JOBS" none
  } \
  || echo "stressapptest FAILED (see build-stressapptest.log)"
fi

# ---- numactl (only its static libnuma.a; rt-tests 2.11 includes numa.h unconditionally) ------------------------------
NUMA_PREFIX=$HERE/out/numa-prefix
if clone numactl; then
  ( cd "$SRC/numactl" && ./autogen.sh && ./configure --host=aarch64-linux-gnu CC=$CC \
      --enable-static --disable-shared --prefix="$NUMA_PREFIX" && make -j$JOBS && make install ) \
      > "$HERE/build-numactl.log" 2>&1 || echo "numactl FAILED (see build-numactl.log)"
fi

# ---- rt-tests (cyclictest, hackbench, pi_stress) ---------------------------------------------------------------------
if clone rt-tests; then
  ( cd "$SRC/rt-tests" && make clean >/dev/null 2>&1
    CPPFLAGS="-I$NUMA_PREFIX/include" LDFLAGS="-static -L$NUMA_PREFIX/lib" \
    make CC=$CC AR=aarch64-linux-gnu-ar no_libcpupower=1 -j$JOBS cyclictest hackbench pi_stress ) \
    > "$HERE/build-rt-tests.log" 2>&1
  for b in cyclictest hackbench pi_stress; do
    if [ -x "$SRC/rt-tests/$b" ]; then
      $STRIP -o "$OUT/$b" "$SRC/rt-tests/$b"
      # The flags as recorded in BUILDINFO.txt (the numactl prefix written as <numa-prefix>).
      flags="CPPFLAGS=-I<numa-prefix>/include LDFLAGS='-static -L<numa-prefix>/lib'"
      flags="$flags make CC=$CC no_libcpupower=1 -j$JOBS $b;"
      flags="$flags libnuma.a from numactl $(lockline numactl | cut -d' ' -f3) (static)"
      record rt-tests $b "$flags" "libcpupower (no_libcpupower=1, --deepest-idle-state unavailable)"
    else
      echo "rt-tests $b FAILED (see build-rt-tests.log)"
    fi
  done
fi

# ---- checksums -------------------------------------------------------------------------------------------------------
( cd "$OUT" && sha256sum $(ls | grep -v -e SHA256SUMS -e BUILDINFO.txt) > SHA256SUMS )
cat "$OUT/SHA256SUMS"
