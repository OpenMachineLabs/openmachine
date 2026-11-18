#!/bin/sh
# Installs Open Machine for the user who runs it:
#
#   curl -fsSL https://openmachine.app/install.sh | sh -s -- --key omkey_...
#   curl -fsSL https://openmachine.app/install.sh | sh        # prints a sign-in link
#
# Options: --key KEY, --key-file FILE (or OM_KEY), --name NAME, --no-up.
#
# Cloud serves this file with the release it verified written into the quoted
# values below: the version and the SHA-256 of each architecture's archive.
# Nothing the download storage holds is run unless it matches. The whole script
# is one function, called on the last line, so a truncated download runs nothing.

_om_install() {
    set -eu
    umask 022 # Core refuses group-writable files

    version='@VERSION@'
    channel='@CHANNEL@'
    downloads='@DOWNLOADS@'
    server=${OM_SERVER:-'@SERVER@'}

    fail() {
        echo "install.sh: $*" >&2
        exit 1
    }

    # The options om install takes are kept as this function's arguments; a key goes by the environment.
    left=$#
    while [ "$left" -gt 0 ]; do
        case $1 in
            --key)
                [ "$left" -ge 2 ] || fail "--key needs a key."
                OM_KEY=$2
                export OM_KEY
                shift 2
                left=$((left - 2)) ;;
            --key-file | --name)
                [ "$left" -ge 2 ] || fail "$1 needs a value."
                set -- "$@" "$1" "$2"
                shift 2
                left=$((left - 2)) ;;
            --no-up)
                set -- "$@" "$1"
                shift
                left=$((left - 1)) ;;
            *)
                fail "unknown option. Options: --key KEY, --key-file FILE, --name NAME, --no-up." ;; # never echoed: it may hold a key
        esac
    done

    [ "$(uname -s)" = Linux ] || fail "Open Machine installs on Linux; this system is $(uname -s)."
    arch=$(uname -m)
    case $arch in
        x86_64) sha256='@SHA256_X86_64@' ;;
        aarch64) sha256='@SHA256_AARCH64@' ;;
        *) fail "Open Machine needs x86_64 or aarch64; this machine is $arch." ;;
    esac
    [ -n "$sha256" ] || fail "Open Machine $version has no build for $arch."
    libc=$(getconf GNU_LIBC_VERSION 2>/dev/null) || libc=
    case $libc in
        'glibc '[0-9]*.[0-9]*) ;;
        *) fail "Open Machine needs glibc 2.17 or later; this system does not use glibc." ;;
    esac
    libc=${libc#glibc }
    major=${libc%%.*}
    minor=${libc#*.}
    minor=${minor%%.*}
    [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 17 ]; } ||
        fail "Open Machine needs glibc 2.17 or later; this system has $libc."
    for tool in tar gzip sha256sum; do
        command -v "$tool" >/dev/null 2>&1 || fail "$tool is missing. Install it and run this again."
    done
    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 ||
        fail "curl or wget is missing. Install one and run this again."
    user=$(id -un)
    case $user in
        '' | *[!A-Za-z0-9._-]*) fail "the OS user name '$user' has characters other than letters, digits, '.', '_' and '-'." ;;
    esac
    [ ${#user} -le 32 ] || fail "the OS user name '$user' is longer than 32 characters."
    # Open Machine runs from HOME: in another user's, as under a sudo that keeps HOME, that user could replace it.
    [ -n "${HOME:-}" ] && [ -d "$HOME" ] && [ -O "$HOME" ] && [ -w "$HOME" ] || fail "HOME is not $user's own directory."

    state=$HOME/.local/state/openmachine-om
    mkdir -p "$HOME/.local/state"
    [ -d "$state" ] || mkdir -m 700 "$state"
    mkdir -p "$state/releases"
    name=openmachine-$version-linux-$arch
    installer=$state/releases/$version/$name/connector/om
    # A run that was interrupted, or is still installing, unpacked this verified release already.
    [ ! -x "$installer" ] ||
        exec "$installer" install --server "$server" --channel "$channel" --downloads "$downloads" "$@"
    new=$state/releases/$version.unpacking
    rm -rf "$new"
    mkdir "$new"
    url=$downloads/$version/$name.tar.gz
    if command -v curl >/dev/null 2>&1; then
        curl -fsL -o "$new.tar.gz" "$url"
    else
        wget -q -O "$new.tar.gz" "$url"
    fi || {
        rm -rf "$new" "$new.tar.gz"
        fail "$url could not be downloaded."
    }
    sum=$(sha256sum "$new.tar.gz")
    if [ "${sum%% *}" != "$sha256" ]; then
        rm -rf "$new" "$new.tar.gz"
        fail "the downloaded archive is not Open Machine $version; nothing was installed."
    fi
    # Only now is it unpacked, as this user whoever owns the files in it, and run.
    tar -xzof "$new.tar.gz" -C "$new" || {
        rm -rf "$new" "$new.tar.gz"
        fail "the archive could not be unpacked."
    }
    rm -rf "$new.tar.gz" "$state/releases/$version"
    mv "$new" "$state/releases/$version"
    exec "$installer" install --server "$server" --channel "$channel" --downloads "$downloads" "$@"
}

{ _om_install "$@"; }
