#!/bin/sh
# agent-voice installer for Linux and macOS.
#
# One command (audit it first: curl -fsSL <url> | less):
#   curl -fsSL https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.sh \
#     | sh -s -- --yes
#
# From a checkout:
#   sh scripts/install.sh --help
#   sh scripts/install.sh --repo /path/to/checkout --dir /tmp/av --no-setup --dry-run
#
# What it does, in order:
#   1. resolves the release to install: the latest release tag by default, never
#      `main` (`main` only with an explicit --ref main), --version wins;
#   2. installs uv with the official installer, only when uv is missing, asking
#      first unless --yes -- never with sudo;
#   3. clones (or updates) the checkout into --dir;
#   4. `uv sync` the root project;
#   5. writes an absolute <install>/.env (AGENT_VOICE_HOME, DATA_DIR,
#      ENGINES_DIR), backing up any existing .env first;
#   6. writes the ~/.local/bin/agent-voice shim;
#   7. runs `agent-voice setup` with the forwarded flags (unless --no-setup).
#
# The data directory is a sibling of the install directory, outside it, and
# --uninstall never removes it.
#
# Security: like every `curl | sh` installer this executes code fetched from the
# network. Read it before running it, and use --dry-run to audit the exact plan.
# This script never calls sudo and never sends telemetry.
#
# This file must stay POSIX sh: it runs under dash, ash, busybox sh and the
# macOS /bin/sh. Only the pipefail option is bash-specific and it is guarded.

set -eu
if [ -n "${BASH_VERSION:-}" ]; then
    # pipefail exists only in bash; POSIX sh (dash/ash/macOS sh) must keep going.
    set -o pipefail
fi

PROG="agent-voice-install"
DEFAULT_REPO="https://github.com/000jd/agent-voice.git"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

# --- Options (empty string means "not given") -------------------------------
INSTALL_DIR_ARG=""
VERSION_ARG=""
REF_ARG=""
REPO_URL="$DEFAULT_REPO"
ASSUME_YES="0"
PROFILE_ARG=""
DOWNLOAD_ARG=""   # "" = leave to setup, "1" = --download, "0" = --no-download
NO_SETUP="0"
UNINSTALL="0"
DRY_RUN="0"

usage() {
    cat <<'EOF'
agent-voice installer (Linux, macOS)

Usage:
  sh install.sh [options]
  curl -fsSL <url>/scripts/install.sh | sh -s -- [options]

Options:
  --dir <path>        Where to install the checkout.
                      Default: ~/.local/share/agent-voice
                      Only this directory is written (plus the shim in
                      ~/.local/bin and the sibling data directory); the data
                      directory is never inside it.
  --version <tag>     Release tag to install, e.g. v0.6.0. Wins over the
                      resolved latest tag. A bare 0.6.0 is accepted.
  --ref <branch|sha>  Install a branch or commit instead of a release tag,
                      e.g. --ref main. Unpinned; prefer --version.
  --repo <url|path>   Repository to clone. A local path is cloned directly and
                      works fully offline; no network access is attempted for
                      the source (tag resolution still reads the local repo).
                      Default: https://github.com/000jd/agent-voice.git
  --yes, -y           Never prompt. Accepts installing uv and passing --yes to
                      `agent-voice setup`. Does not download model weights.
  --profile <name>    Hardware profile forwarded to `agent-voice setup`:
                      auto (default) | cpu-only | nvidia-gpu | apple-silicon
  --download          Forward --download to `agent-voice setup`: fetch model
                      weights now.
  --no-download       Forward --no-download to `agent-voice setup`: install the
                      engines but fetch no weights now.
  --no-setup          Stop after the install and the shim; do not run
                      `agent-voice setup`.
  --uninstall         Remove ~/.local/bin/agent-voice and the install directory.
                      The data directory is NEVER removed; its path is printed.
  --dry-run           Print every action and every argv, execute nothing, write
                      nothing. This is the audit path for `curl | sh`.
  --help, -h          Print this help and exit 0.

Environment:
  HOME                Required. The install dir, the shim dir (~/.local/bin)
                      and the default paths are derived from it.
  AGENT_VOICE_HOME    If already set, the installer warns: it writes
                      AGENT_VOICE_HOME into <install>/.env and the shim exports
                      it, so the default resolve is the install directory.

Examples:
  sh install.sh --yes
  sh install.sh --version v0.6.0 --profile nvidia-gpu --download --yes
  sh install.sh --dir ~/src/agent-voice --no-setup
  sh install.sh --repo /path/to/local/checkout --dry-run
  sh install.sh --uninstall --yes

Notes:
  - The installer never calls sudo; uv goes to ~/.local/bin.
  - `uv` is only installed when it is missing, and only after consent
    (or --yes). The official installer is https://astral.sh/uv/install.sh.
  - The data directory defaults to a sibling of the install directory:
      <install dir>/../agent-voice-data
  - Exit codes: 0 success, 1 error (message on stderr).
EOF
}

# --- Small helpers ----------------------------------------------------------
say() {
    printf '%s\n' "$*"
}

warn() {
    printf '%s: warning: %s\n' "$PROG" "$*" >&2
}

die() {
    printf '%s: error: %s\n' "$PROG" "$*" >&2
    exit 1
}

# Quote one argument for the human-readable dry-run trace. The trace is never
# executed: the real path always uses the argv form.
shell_quote() {
    case $1 in
        '' | *[!A-Za-z0-9_./:=@%+-]*)
            _escaped=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
            printf "'%s'" "$_escaped"
            ;;
        *)
            printf '%s' "$1"
            ;;
    esac
}

print_argv() {
    _sep=''
    for _arg in "$@"; do
        printf '%s' "$_sep"
        shell_quote "$_arg"
        _sep=' '
    done
    printf '\n'
}

# run <argv>... -- execute it, or print it under --dry-run.
run() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '+ '
        print_argv "$@"
        return 0
    fi
    "$@"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# ask "question" -- yes only on an explicit y/yes from a real terminal.
ask() {
    if [ "$ASSUME_YES" = "1" ]; then
        return 0
    fi
    if [ ! -e /dev/tty ] || ! : </dev/tty 2>/dev/null; then
        return 1
    fi
    printf '%s [y/N] ' "$1" >/dev/tty
    _reply=''
    if ! IFS= read -r _reply </dev/tty; then
        return 1
    fi
    case $_reply in
        y | Y | yes | YES | Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# absolutize <path> -- expand ~, make absolute, drop trailing slashes.
absolutize() {
    _p=$1
    case $_p in
        '~') _p=$HOME ;;
        '~/'*) _p=$HOME/${_p#"~/"};;
    esac
    case $_p in
        /*) ;;
        # `pwd` is a builtin; PWD may be unset under `env -i`.
        *) _p=$(pwd)/$_p ;;
    esac
    while [ "$_p" != "/" ] && [ "${_p%/}" != "$_p" ]; do
        _p=${_p%/}
    done
    printf '%s' "$_p"
}

# --- Argument parsing -------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --dir)
                shift
                [ $# -gt 0 ] || die "--dir needs a value"
                INSTALL_DIR_ARG=$1
                ;;
            --dir=*)
                INSTALL_DIR_ARG=${1#*=}
                ;;
            --version)
                shift
                [ $# -gt 0 ] || die "--version needs a value"
                VERSION_ARG=$1
                ;;
            --version=*)
                VERSION_ARG=${1#*=}
                ;;
            --ref)
                shift
                [ $# -gt 0 ] || die "--ref needs a value"
                REF_ARG=$1
                ;;
            --ref=*)
                REF_ARG=${1#*=}
                ;;
            --repo)
                shift
                [ $# -gt 0 ] || die "--repo needs a value"
                REPO_URL=$1
                ;;
            --repo=*)
                REPO_URL=${1#*=}
                ;;
            --yes | -y)
                ASSUME_YES="1"
                ;;
            --profile)
                shift
                [ $# -gt 0 ] || die "--profile needs a value"
                PROFILE_ARG=$1
                ;;
            --profile=*)
                PROFILE_ARG=${1#*=}
                ;;
            --download)
                [ "$DOWNLOAD_ARG" = "0" ] && die "--download and --no-download are mutually exclusive"
                DOWNLOAD_ARG="1"
                ;;
            --no-download)
                [ "$DOWNLOAD_ARG" = "1" ] && die "--download and --no-download are mutually exclusive"
                DOWNLOAD_ARG="0"
                ;;
            --no-setup)
                NO_SETUP="1"
                ;;
            --uninstall)
                UNINSTALL="1"
                ;;
            --dry-run)
                DRY_RUN="1"
                ;;
            --help | -h)
                usage
                exit 0
                ;;
            *)
                printf '%s: error: unknown argument: %s\n' "$PROG" "$1" >&2
                printf "Try '%s --help'.\n" "$PROG" >&2
                exit 1
                ;;
        esac
        shift
    done
}

validate_args() {
    if [ -n "$INSTALL_DIR_ARG" ]; then
        case $INSTALL_DIR_ARG in
            *[![:space:]]*) ;;
            *) die "--dir must not be empty" ;;
        esac
    fi

    if [ -n "$PROFILE_ARG" ]; then
        case $PROFILE_ARG in
            auto | cpu-only | nvidia-gpu | apple-silicon) ;;
            *) die "--profile must be one of: auto, cpu-only, nvidia-gpu, apple-silicon (got '$PROFILE_ARG')" ;;
        esac
    fi

    if [ -z "$REPO_URL" ]; then
        die "--repo must not be empty"
    fi

    if [ -n "$VERSION_ARG" ] && [ -n "$REF_ARG" ]; then
        die "--version and --ref are mutually exclusive: a release tag or a branch/commit"
    fi

    case $VERSION_ARG in
        '' | v*) ;;
        [0-9]*) VERSION_ARG="v$VERSION_ARG" ;;
        *) die "--version must be a release tag such as v0.6.0 (got '$VERSION_ARG')" ;;
    esac
}

# --- Release resolution -----------------------------------------------------
# Latest release tag = highest vX.Y.Z from `git ls-remote --tags --refs`,
# semver-sorted numerically. Never falls back to main: an empty answer is a
# hard error telling the user to pass --version or --ref.
resolve_latest_tag() {
    _repo=$1
    _out=$(git ls-remote --tags --refs "$_repo" 2>/dev/null) || return 1
    # Keep only plain release tags (vX.Y.Z; no -rc/beta), drop the "v" so a
    # plain numeric field sort is exact, then put the "v" back. GNU and BSD sort
    # both support this; `sort -V` is not available on macOS.
    _tag=$(
        printf '%s\n' "$_out" |
            awk '{print $2}' |
            sed -n 's|^refs/tags/\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$|\1|p' |
            sed 's/^v//' |
            LC_ALL=C sort -t. -k1,1n -k2,2n -k3,3n |
            tail -n 1
    ) || return 1
    [ -n "$_tag" ] || return 0
    printf 'v%s\n' "$_tag"
}

# --- uv ---------------------------------------------------------------------
ensure_uv() {
    if have uv; then
        UV_BIN=$(command -v uv)
        say "uv: $UV_BIN"
        return 0
    fi

    say "uv is not installed; it is required to sync the project."
    say "The official installer would be run: curl -fsSL $UV_INSTALL_URL | sh"
    say "It installs uv into \$HOME/.local/bin (no sudo, no system directories)."

    if [ "$DRY_RUN" = "1" ]; then
        # Never prompt during an audit run: print the action instead.
        printf '+ curl -fsSL %s | sh\n' "$UV_INSTALL_URL"
        # Where the official installer puts it; keeps the traced argv honest.
        UV_BIN="$HOME/.local/bin/uv"
        return 0
    fi

    if ! ask "Install uv now?"; then
        die "uv is required; install it from https://docs.astral.sh/uv/ or re-run with --yes"
    fi

    have curl || die "curl is required to install uv; install curl or install uv manually"
    curl -fsSL "$UV_INSTALL_URL" | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    have uv || die "the uv installer finished but uv is not on PATH ($HOME/.local/bin)"
    UV_BIN=$(command -v uv)
    say "uv: $UV_BIN"
}

# --- Checkout ---------------------------------------------------------------
check_git() {
    if have git; then
        return 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        warn "git was not found on PATH; a real run would fail here"
        return 0
    fi
    die "git is required; install git and re-run"
}

checkout_target() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '+ '
        print_argv git -C "$INSTALL_DIR" checkout --detach "$TARGET"
        return 0
    fi
    if git -C "$INSTALL_DIR" checkout --detach "$TARGET" >/dev/null 2>&1; then
        say "checked out $TARGET"
        return 0
    fi
    # A branch fetched from a remote lives under refs/remotes/origin/<name>.
    if git -C "$INSTALL_DIR" checkout --detach "origin/$TARGET" >/dev/null 2>&1; then
        say "checked out origin/$TARGET"
        return 0
    fi
    die "could not check out '$TARGET' from $REPO_URL"
}

fetch_repo() {
    # Explicit refspecs: independent of how the checkout's origin is configured,
    # and identical for a local path and a URL.
    run git -C "$INSTALL_DIR" fetch --tags --force "$REPO_URL" \
        '+refs/heads/*:refs/remotes/origin/*'
}

precheck_checkout() {
    # Read-only: refuse a bad install dir before uv is installed or a clone runs.
    [ -e "$INSTALL_DIR" ] || return 0
    [ -d "$INSTALL_DIR/.git" ] ||
        die "install directory exists but is not a git checkout: $INSTALL_DIR (move it away or pick another --dir)"
    # Tracked files only: the installer itself writes .env, .env.bak and .venv
    # here, and untracked local artifacts must not block an update. A modified
    # tracked file is a real edit and is refused.
    _dirty=$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no 2>/dev/null || true)
    [ -z "$_dirty" ] ||
        die "install directory has local changes to tracked files: $INSTALL_DIR (commit/stash them or pick another --dir)"
    return 0
}

sync_checkout() {
    if [ -e "$INSTALL_DIR" ]; then
        precheck_checkout
        say "Updating existing checkout at $INSTALL_DIR"
        fetch_repo
        checkout_target
        return 0
    fi

    say "Cloning $REPO_URL into $INSTALL_DIR"
    run mkdir -p "$(dirname "$INSTALL_DIR")"
    run git clone "$REPO_URL" "$INSTALL_DIR"
    checkout_target
}

# --- .env -------------------------------------------------------------------
backup_env() {
    [ -f "$ENV_FILE" ] || return 0
    _ts=$(date -u +%Y%m%dT%H%M%SZ)
    if [ -e "$ENV_FILE.bak" ]; then
        # Never overwrite an existing .env.bak: it may be the operator's only copy.
        _backup="$ENV_FILE.$_ts.bak"
        _n=1
        while [ -e "$_backup" ]; do
            _backup="$ENV_FILE.$_ts-$_n.bak"
            _n=$((_n + 1))
        done
    else
        _backup="$ENV_FILE.bak"
    fi
    say "Backing up existing .env: $ENV_FILE -> $_backup"
    run cp "$ENV_FILE" "$_backup"
}

write_env() {
    backup_env

    if [ "$DRY_RUN" = "1" ]; then
        printf '+ write %s:\n' "$ENV_FILE"
        printf '    AGENT_VOICE_HOME=%s\n' "$INSTALL_DIR"
        printf '    DATA_DIR=%s\n' "$DATA_DIR"
        printf '    ENGINES_DIR=%s\n' "$ENGINES_DIR"
        return 0
    fi

    _tmp="$ENV_FILE.installer.$$"
    if [ -f "$ENV_FILE" ]; then
        # Preserve every other line (comments, unknown keys) byte for byte. The
        # banner is filtered too, otherwise a re-run appends a second copy.
        grep -v -E '^(AGENT_VOICE_HOME|DATA_DIR|ENGINES_DIR)=|^# --- Installer paths \(absolute, managed by scripts/install\.sh\) ---$' \
            "$ENV_FILE" >"$_tmp" || true
    else
        {
            printf '%s\n' '# agent-voice configuration written by scripts/install.sh.'
            printf '%s\n' '# Paths are absolute so the CLI works from any directory.'
        } >"$_tmp"
    fi
    {
        printf '\n# --- Installer paths (absolute, managed by scripts/install.sh) ---\n'
        printf 'AGENT_VOICE_HOME=%s\n' "$INSTALL_DIR"
        printf 'DATA_DIR=%s\n' "$DATA_DIR"
        printf 'ENGINES_DIR=%s\n' "$ENGINES_DIR"
    } >>"$_tmp"
    chmod 600 "$_tmp"
    mv "$_tmp" "$ENV_FILE"
    say "Wrote $ENV_FILE (0600)"
}

# --- Shim -------------------------------------------------------------------
write_shim() {
    _quoted_path=$(printf '%s' "$INSTALL_DIR" | sed "s/'/'\\\\''/g")

    if [ "$DRY_RUN" = "1" ]; then
        printf '+ write %s (#!/bin/sh, exports AGENT_VOICE_HOME=%s, execs %s/.venv/bin/agent-voice "$@")\n' \
            "$SHIM" "$INSTALL_DIR" "$INSTALL_DIR"
        printf '+ chmod +x %s\n' "$SHIM"
        return 0
    fi

    mkdir -p "$BIN_DIR"
    cat >"$SHIM" <<SHIM_EOF
#!/bin/sh
# Generated by agent-voice scripts/install.sh -- edits are overwritten.
AGENT_VOICE_HOME='$_quoted_path'
export AGENT_VOICE_HOME
exec '$_quoted_path/.venv/bin/agent-voice' "\$@"
SHIM_EOF
    chmod +x "$SHIM"
    say "Wrote $SHIM"
}

warn_path() {
    case ":${PATH:-}:" in
        *":$BIN_DIR:"*) ;;
        *)
            warn "$BIN_DIR is not on PATH; add it, e.g.:  export PATH=\"\$HOME/.local/bin:\$PATH\""
            ;;
    esac
}

# --- setup ------------------------------------------------------------------
run_setup() {
    set -- setup
    if [ "$ASSUME_YES" = "1" ]; then
        set -- "$@" --yes
    fi
    if [ -n "$PROFILE_ARG" ]; then
        set -- "$@" --profile "$PROFILE_ARG"
    fi
    case $DOWNLOAD_ARG in
        1) set -- "$@" --download ;;
        0) set -- "$@" --no-download ;;
    esac
    run env "AGENT_VOICE_HOME=$INSTALL_DIR" "$INSTALL_DIR/.venv/bin/agent-voice" "$@"
}

# --- uninstall --------------------------------------------------------------
do_uninstall() {
    say "Uninstalling agent-voice"
    say "  shim:    $SHIM"
    say "  install: $INSTALL_DIR"
    say "  data:    $DATA_DIR (kept)"

    case $INSTALL_DIR in
        / | "$HOME") die "refusing to remove '$INSTALL_DIR'" ;;
    esac

    if [ -e "$SHIM" ]; then
        run rm -f "$SHIM"
    else
        say "  shim not present, nothing to remove"
    fi

    if [ -d "$INSTALL_DIR" ]; then
        run rm -rf "$INSTALL_DIR"
    else
        say "  install directory not present, nothing to remove"
    fi

    if [ "$DRY_RUN" = "1" ]; then
        say "Dry run: nothing was removed."
    fi
    say "Data directory kept: $DATA_DIR"
    say "To delete it yourself: rm -rf $(shell_quote "$DATA_DIR")"
}

# --- Main -------------------------------------------------------------------
main() {
    parse_args "$@"

    [ -n "${HOME:-}" ] || die "HOME is not set; export HOME and re-run"

    if [ -z "$INSTALL_DIR_ARG" ]; then
        INSTALL_DIR_ARG="$HOME/.local/share/agent-voice"
    fi
    INSTALL_DIR=$(absolutize "$INSTALL_DIR_ARG")
    case $INSTALL_DIR in
        /) die "--dir must not be /" ;;
        "$HOME") die "--dir must not be HOME itself ($HOME)" ;;
    esac

    # Data is a sibling of the install dir, never inside it, so a reinstall or
    # --uninstall cannot destroy voices, cache or downloaded weights.
    DATA_DIR=$(dirname "$INSTALL_DIR")/agent-voice-data
    ENGINES_DIR="$INSTALL_DIR/engines"
    ENV_FILE="$INSTALL_DIR/.env"
    BIN_DIR="$HOME/.local/bin"
    SHIM="$BIN_DIR/agent-voice"

    validate_args

    if [ "$UNINSTALL" = "1" ]; then
        do_uninstall
        exit 0
    fi

    # Refuse a bad install dir before anything is installed or cloned.
    precheck_checkout

    # Resolve what to install. --version wins; --ref is explicit and unpinned;
    # otherwise the latest release tag, never main.
    if [ -n "$VERSION_ARG" ]; then
        VERSION=$VERSION_ARG
        TARGET=$VERSION
        TARGET_SOURCE="--version"
    elif [ -n "$REF_ARG" ]; then
        TARGET=$REF_ARG
        VERSION=""
        TARGET_SOURCE="--ref"
    else
        check_git
        if VERSION=$(resolve_latest_tag "$REPO_URL"); then
            [ -n "$VERSION" ] ||
                die "no vX.Y.Z release tag found in $REPO_URL; pass --version <tag> or --ref <branch|sha>"
        else
            die "could not read tags from $REPO_URL (offline?); pass --version <tag> or --ref <branch|sha>"
        fi
        TARGET=$VERSION
        TARGET_SOURCE="latest release tag"
    fi

    if [ "$DRY_RUN" = "1" ]; then
        say "agent-voice installer -- DRY RUN (nothing will be written)"
    else
        say "agent-voice installer"
    fi
    say "  repo        $REPO_URL"
    say "  target      $TARGET ($TARGET_SOURCE)"
    say "  install dir $INSTALL_DIR"
    say "  data dir    $DATA_DIR"
    say "  engines dir $ENGINES_DIR"
    say "  shim        $SHIM"
    say "  profile     ${PROFILE_ARG:-auto}"
    case $DOWNLOAD_ARG in
        1) say "  weights     download now (--download)" ;;
        0) say "  weights     no download (--no-download)" ;;
        *) say "  weights     setup decides" ;;
    esac
    if [ "$NO_SETUP" = "1" ]; then
        say "  setup       skipped (--no-setup)"
    else
        say "  setup       agent-voice setup"
    fi
    if [ -n "${AGENT_VOICE_HOME:-}" ] && [ "$AGENT_VOICE_HOME" != "$INSTALL_DIR" ]; then
        warn "AGENT_VOICE_HOME is set to '$AGENT_VOICE_HOME'; this installer writes '$INSTALL_DIR' into .env and the shim"
    fi

    check_git
    ensure_uv
    sync_checkout

    say "Syncing the root project with uv"
    run "$UV_BIN" sync --project "$INSTALL_DIR"

    if [ "$DRY_RUN" != "1" ] && [ ! -x "$INSTALL_DIR/.venv/bin/agent-voice" ]; then
        die "uv sync finished but $INSTALL_DIR/.venv/bin/agent-voice is missing"
    fi

    run mkdir -p "$DATA_DIR"
    write_env
    write_shim
    warn_path

    if [ "$NO_SETUP" = "1" ]; then
        say "Skipping 'agent-voice setup' (--no-setup)."
    else
        say "Running 'agent-voice setup'"
        run_setup
    fi

    say ""
    if [ "$DRY_RUN" = "1" ]; then
        say "Dry run complete: no changes were made."
    else
        _display_version=${VERSION:-$TARGET}
        say "agent-voice $_display_version installed."
    fi
    say "  install dir $INSTALL_DIR"
    say "  data dir    $DATA_DIR"
    say "  shim        $SHIM"
    say "Next: ensure $BIN_DIR is on PATH, then run 'agent-voice doctor'."
}

main "$@"
