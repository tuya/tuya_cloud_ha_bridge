#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./scripts/install_to_ha_custom_components.sh /path/to/homeassistant/config

Examples:
  ./scripts/install_to_ha_custom_components.sh "/Volumes/homeassistant/config"
  ./scripts/install_to_ha_custom_components.sh "/Users/me/.homeassistant"

Notes:
  - Pass the Home Assistant config directory, not the plugin directory.
  - The script will back up an existing target directory before replacing it.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -ne 1 ]]; then
    usage
    exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="${repo_root}/custom_components/tuya_cloud_ha_bridge"
ha_config_dir="${1%/}"
custom_components_dir="${ha_config_dir}/custom_components"
target_dir="${custom_components_dir}/tuya_cloud_ha_bridge"
timestamp="$(date +"%Y%m%d_%H%M%S")"
tmp_target_dir="${custom_components_dir}/.tuya_cloud_ha_bridge.tmp_${timestamp}"
backup_dir=""

cleanup() {
    if [[ -d "${tmp_target_dir}" ]]; then
        rm -rf "${tmp_target_dir}"
    fi
}

trap cleanup EXIT

if [[ ! -d "${source_dir}" ]]; then
    echo "Source directory not found: ${source_dir}" >&2
    exit 1
fi

if [[ ! -d "${ha_config_dir}" ]]; then
    echo "Home Assistant config directory not found: ${ha_config_dir}" >&2
    exit 1
fi

if [[ ! -f "${ha_config_dir}/configuration.yaml" ]]; then
    echo "Warning: configuration.yaml was not found in ${ha_config_dir}" >&2
    echo "Please confirm this is the correct Home Assistant config directory." >&2
fi

mkdir -p "${custom_components_dir}"

if [[ -d "${target_dir}" ]]; then
    backup_dir="${target_dir}.backup_${timestamp}"
    echo "Backing up existing plugin directory to:"
    echo "  ${backup_dir}"
    mv "${target_dir}" "${backup_dir}"
fi

echo "Copying plugin files to:"
echo "  ${target_dir}"
cp -R "${source_dir}" "${tmp_target_dir}"
mv "${tmp_target_dir}" "${target_dir}"

echo
echo "Install complete."
echo "Next steps:"
echo "  1. Restart Home Assistant"
echo "  2. Go to Settings > Devices & Services"
echo "  3. Add the tuya_cloud_ha_bridge integration"
