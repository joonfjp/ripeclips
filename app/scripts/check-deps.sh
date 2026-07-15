#!/usr/bin/env bash
#
# Checks which prerequisites are installed and what is still missing.
# Read-only: it never installs anything.
#
#   ./scripts/check-deps.sh

set -u

green=$'\033[32m'; red=$'\033[31m'; dim=$'\033[2m'; reset=$'\033[0m'
missing=0
min_cargo_version="1.85.0"

# check <label> <command-to-test> <install-hint>
check() {
  local label="$1" probe="$2" hint="$3"
  if eval "$probe" >/dev/null 2>&1; then
    printf "  ${green}ok${reset}      %-18s %s\n" "$label" "${dim}$(eval "$probe" 2>/dev/null | head -n1)${reset}"
  else
    printf "  ${red}missing${reset} %-18s ${dim}install: %s${reset}\n" "$label" "$hint"
    missing=$((missing + 1))
  fi
}

version_at_least() {
  local actual="$1" required="$2"
  local actual_major actual_minor actual_patch required_major required_minor required_patch

  IFS=. read -r actual_major actual_minor actual_patch <<< "$actual"
  IFS=. read -r required_major required_minor required_patch <<< "$required"

  [ "${actual_major:-0}" -gt "${required_major:-0}" ] && return 0
  [ "${actual_major:-0}" -lt "${required_major:-0}" ] && return 1
  [ "${actual_minor:-0}" -gt "${required_minor:-0}" ] && return 0
  [ "${actual_minor:-0}" -lt "${required_minor:-0}" ] && return 1
  [ "${actual_patch:-0}" -ge "${required_patch:-0}" ]
}

check_cargo() {
  local version output

  if ! output="$(cargo --version 2>/dev/null)"; then
    printf "  ${red}missing${reset} %-18s ${dim}install: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh${reset}\n" "Rust/Cargo"
    missing=$((missing + 1))
    return
  fi

  version="$(printf "%s\n" "$output" | awk '{print $2}')"
  if version_at_least "$version" "$min_cargo_version"; then
    printf "  ${green}ok${reset}      %-18s %s\n" "Rust/Cargo" "${dim}${output}${reset}"
  else
    printf "  ${red}missing${reset} %-18s ${dim}need Cargo >= %s; update: rustup update stable${reset}\n" "Rust/Cargo" "$min_cargo_version"
    missing=$((missing + 1))
  fi
}

check_node() {
  local output

  if output="$(mise exec -- node --version 2>/dev/null)" && printf "%s\n" "$output" | grep -q '^v24'; then
    printf "  ${green}ok${reset}      %-18s %s\n" "Node 24" "${dim}${output}${reset}"
  else
    printf "  ${red}missing${reset} %-18s ${dim}install: mise trust && mise install${reset}\n" "Node 24"
    missing=$((missing + 1))
  fi
}

echo "Checking RipeClips prerequisites..."
echo

check "Xcode CLT"  "xcode-select -p"               "xcode-select --install"
check "mise"       "mise --version"                "curl https://mise.run | sh"
check_cargo
check_node
check "FFmpeg"     "ffmpeg -version"               "Install a libass-enabled FFmpeg (see app/README.md)"

echo
if [ "$missing" -eq 0 ]; then
  printf "${green}All prerequisites present.${reset} Next: npm install && npm run setup:ffmpeg\n"
else
  printf "${red}%d missing.${reset} Install the items above, then re-run this script.\n" "$missing"
  exit 1
fi
