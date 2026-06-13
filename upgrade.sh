#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REPO="bkono/edcom-ce"
REPO="${EDCOM_UPDATE_REPO:-$DEFAULT_REPO}"
API_BASE="${EDCOM_UPDATE_API_BASE:-https://api.github.com}"
GITHUB_BASE="${EDCOM_UPDATE_GITHUB_BASE:-https://github.com}"

usage() {
    cat <<EOF
Usage: $0 [--check-only] [--no-prompt] [--repo owner/repo]

--check-only: Check for latest version and exit.
--no-prompt:  Do not prompt for confirmation before upgrading.
--repo:        Override GitHub repository. Defaults to $DEFAULT_REPO.
EOF
}

get_non_empty_input() {
    local prompt="$1"
    local input

    while true; do
        read -rp "$prompt" input
        if [[ -n "$input" ]]; then
            echo "$input"
            break
        fi
    done
}

require_command() {
    local command="$1"
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Missing required command: $command" >&2
        exit 1
    fi
}

json_string_value() {
    local key="$1"
    sed -nE "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"([^\"]+)\".*/\\1/p" | head -n 1
}

if [[ $UID -ne 0 ]]; then
    echo ""
    echo "This script must be run as root"
    echo "Exiting the setup. Become root by running 'sudo su' and then run this script again."
    echo ""
    exit 1
fi

if [[ ! -f "upgrade.sh" || ! -d "config" || ! -f "docker-compose.yml" ]]; then
    echo ""
    echo "This script must be run from the edcom-install directory."
    echo "Exiting the setup. Please cd into the edcom-install directory and run this script again."
    echo ""
    exit 1
fi

check_only=false
no_prompt=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-only)
            check_only=true
            shift
            ;;
        --no-prompt)
            no_prompt=true
            shift
            ;;
        --repo)
            if [[ $# -lt 2 || -z "$2" ]]; then
                usage
                exit 1
            fi
            REPO="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

require_command curl
require_command tar
require_command docker

if [[ -f "./config/VERSION" ]]; then
    file_version=$(tr -d '[:space:]' < ./config/VERSION)
else
    file_version="unknown"
fi

arch=amd64
if [[ -f "./config/ARM64-VERSION" ]]; then
    arch=arm64
fi

asset="edcom-install-$arch.tgz"
release_json=$(curl -fsSL -H "Accept: application/vnd.github+json" "$API_BASE/repos/$REPO/releases/latest")
latest_tag=$(printf '%s\n' "$release_json" | json_string_value "tag_name")
latest_version="${latest_tag#v}"

if [[ -z "$latest_version" || "$latest_version" == "$latest_tag" ]]; then
    echo ""
    echo "Cannot determine the latest version from $REPO."
    echo ""
    exit 1
fi

echo ""
if [[ "$latest_version" == "$file_version" ]]; then
    echo "Your platform version and the latest version are equal"
else
    echo "Your platform version is NOT the latest version"
fi
echo ""
echo "Update repository:     $REPO"
echo "Architecture:          $arch"
echo "Your platform version: $file_version"
echo "Latest version:        $latest_version"
echo ""

if [[ "$latest_version" == "$file_version" || "$check_only" == true ]]; then
    exit 0
fi

if [[ "$no_prompt" == false ]]; then
    response=$(get_non_empty_input "Do you want to proceed with upgrading your platform? [y/n]: ")

    if [[ "$response" != "y" && "$response" != "Y" ]]; then
        echo ""
        echo "Exiting."
        echo ""
        exit 0
    fi
fi

tmpdir=$(mktemp -d)
cleanup() {
    rm -rf "$tmpdir"
}
trap cleanup EXIT

download_url="$GITHUB_BASE/$REPO/releases/latest/download/$asset"
checksums_url="$GITHUB_BASE/$REPO/releases/latest/download/checksums.txt"
upgrade_filename="$tmpdir/$asset"
checksums_file="$tmpdir/checksums.txt"

echo ""
echo "Downloading upgrade package..."
echo "$download_url"
echo ""
curl -fL "$download_url" -o "$upgrade_filename"

if curl -fsL "$checksums_url" -o "$checksums_file"; then
    if command -v sha256sum >/dev/null 2>&1; then
        echo ""
        echo "Verifying checksum..."
        (cd "$tmpdir" && grep "  $asset$" checksums.txt | sha256sum -c -)
    else
        echo ""
        echo "sha256sum is unavailable; skipping checksum verification."
    fi
else
    echo ""
    echo "No checksums.txt found; skipping checksum verification."
fi

echo ""
echo "Extracting..."
echo ""

current_dir=$(pwd)
dir_name=$(basename "$current_dir")

if [[ "$dir_name" == "/" ]]; then
    echo ""
    echo "Your installation appears to be in the root directory, automatic upgrade cannot complete."
    echo ""
    exit 1
fi

cd ..

if [[ "$dir_name" != "edcom-install" ]]; then
    if [[ -d "edcom-install" ]]; then
        echo ""
        echo "Cannot upgrade because the install directory is not named edcom-install and another edcom-install directory exists."
        echo ""
        exit 1
    fi
    mv "$dir_name" edcom-install
fi

tar -zxf "$upgrade_filename" -C .

if [[ "$dir_name" != "edcom-install" ]]; then
    mv edcom-install "$dir_name"
fi

cd "$dir_name"

echo ""
echo "Loading new images..."
echo ""
./load_images.sh

echo ""
echo "Restarting..."
echo ""
./restart.sh

echo ""
echo "Cleaning up old Docker images..."
echo ""
docker system prune -f

echo ""
echo "Upgrade complete!"
echo ""
cat ./config/VERSION
