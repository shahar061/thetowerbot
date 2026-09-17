#!/bin/zsh
set -euo pipefail

agent_root="${0:A:h}"
install_root="$HOME/Library/Application Support/TheTowerBot"
runtime_dir="$install_root/runtime"
app_root="$install_root/TowerInputAgent.app"
launch_agents="$HOME/Library/LaunchAgents"
label="com.thetowerbot.input-agent"

mkdir -p "$app_root/Contents/MacOS" "$runtime_dir" "$launch_agents"
chmod 700 "$install_root" "$runtime_dir"
cp "$agent_root/Resources/Info.plist" "$app_root/Contents/Info.plist"
swiftc "$agent_root/Sources/TowerInputAgent/main.swift" -o "$app_root/Contents/MacOS/tower-input-agent"
chmod 700 "$app_root/Contents/MacOS/tower-input-agent"

plist="$launch_agents/$label.plist"
socket_path="$runtime_dir/input-agent.sock"
sed -e "s|/usr/local/bin/tower-input-agent|$app_root/Contents/MacOS/tower-input-agent|" \
    -e "s|__SOCKET_PATH__|$socket_path|" \
  "$agent_root/LaunchAgents/$label.plist" > "$plist"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
printf 'installed %s\n' "$app_root"
