# tools/rhel8/Dockerfile — old-glibc-floor build image for the Linux V8 lane (task #24b).
#
# WHY: the default Linux cell builds on ubuntu-24.04 (glibc ~2.39). Because
# linux_gn_args sets use_sysroot=false + use_custom_libcxx=false, the sealed
# libv8.so links the BUILD HOST's glibc + libstdc++, so the .so inherits the
# host's glibc symbol-version floor. A 2.39 floor means the artifact won't
# dlopen on older enterprise Linux (RHEL/Rocky/Alma 8, Amazon Linux 2, SLES 15,
# Debian 10). Building inside Rocky Linux 8 (glibc 2.28) lowers that floor so
# the same .so loads on any glibc >= 2.28 host — the same baseline Node ships
# libnode against, and the reason build-v8.py's linux_gn_args comment calls out
# a "RHEL/Rocky-8 build image" as the release-portability follow-up.
#
# HOW THE FLOOR WORKS: V8 still compiles with its OWN bundled clang (llvmorg-23,
# x86_64-Linux-host). The glibc floor is NOT set by the compiler — it is set by
# the glibc DEVELOPMENT HEADERS / crt objects the bundled clang resolves against
# (/usr/include, /usr/lib64) and by the libstdc++ that use_custom_libcxx=false
# links. On Rocky 8 those are glibc 2.28, so every versioned libc reference the
# linker stamps (GLIBC_x.y in libv8.so's .gnu.version_r) is <= 2.28.
#
# gcc-toolset-12: Rocky 8's stock gcc is 8.x (no C++20). use_custom_libcxx=false
# links the SYSTEM libstdc++, which must be C++20-complete for V8 15.1. The
# gcc-toolset-12 SCL ships gcc-12 + its modern libstdc++ ON the glibc-2.28 base
# (the libstdc++ runtime symbols are GLIBCXX_*, independent of glibc's floor),
# so we get C++20 without raising the glibc floor. The entrypoint enables it.
#
# WHERE THIS RUNS: a native x86_64 Linux host. In CI that is the ubuntu-24.04
# runner executing this rockylinux:8 container (.github/workflows/build-v8-rhel8.yml).
# Locally it needs an x86_64 Docker host (the arm64 Mac emulates x86_64 slowly).
FROM rockylinux:8

# Core build deps for depot_tools / gn / ninja + V8 monolith.
#   git, python3        — depot_tools, gclient, build-v8.py
#   curl, ca-certificates, which, file, xz, bzip2, zip/unzip — depot_tools fetch + tooling
#   glibc-langpack-en   — a UTF-8 locale (gn/ninja + V8's non-ASCII BUILD.gn need one)
#   binutils            — objdump/readelf for the seal audit + the glibc-floor gate
#   pkgconfig           — use_sysroot=false resolves system libs via pkg-config
#   gcc-toolset-12      — modern gcc/g++/libstdc++ (C++20) on the glibc-2.28 base
#   libatomic           — V8 monolith references __atomic_* on some TUs
#   ccache              — optional build-object cache (build-v8.py auto-detects it)
RUN dnf -y install dnf-plugins-core epel-release \
 && dnf -y install \
      git python3.11 python3.11-pip \
      curl ca-certificates which file xz bzip2 zip unzip tar \
      glibc-langpack-en \
      binutils pkgconfig libatomic \
      gcc-toolset-12 gcc-toolset-12-libatomic-devel \
 && dnf -y install ccache || true \
 && dnf clean all

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    DEPOT_TOOLS_UPDATE=1

# Rocky 8's default python3 is 3.6, but V8/depot_tools build scripts use 3.8+ syntax
# (bare @functools.lru_cache → "Expected maxsize to be an integer"). Make python3.11 the
# python3/python on PATH (/usr/local/bin precedes /usr/bin) so gn/ninja/build-v8.py use it.
RUN ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
 && ln -sf /usr/bin/python3.11 /usr/local/bin/python

WORKDIR /work

# Enable gcc-toolset-12 for every RUN/entrypoint shell so use_custom_libcxx=false
# finds a C++20 libstdc++. `scl enable` rewrites PATH/LD_LIBRARY_PATH to the SCL
# gcc-12 + its libstdc++; without it the build falls back to system gcc-8 (no C++20).
ENTRYPOINT ["scl", "enable", "gcc-toolset-12", "--"]
CMD ["bash"]
