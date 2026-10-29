#!/bin/bash
# Removes what the cc-viewer setup script installed on this Mac/Linux computer, so Claude on that server can no longer reach it.
# Run in a terminal:
#   bash -c "$(curl -fsSL <cc-viewer address>/pc-setup/uninstall.sh || wget -qO- <cc-viewer address>/pc-setup/uninstall.sh)"
# The SSH server itself stays installed (other programs may use it).
# The "devices" page of cc-viewer runs this remotely when a computer is deleted there: in the background and after a short wait,
# because it stops the very connection it arrives through.
sleep "${CCV_UNINSTALL_DELAY:-0}"
D="$HOME/.ssh/ccviewer"
if [ "$(uname)" = Darwin ]; then
    for p in "$HOME"/Library/LaunchAgents/local.ccviewer-tunnel*.plist; do
        [ -f "$p" ] || continue
        launchctl bootout "gui/$(id -u)/$(basename "$p" .plist)" 2>/dev/null
        rm -f "$p"
    done
else
    for u in "$HOME"/.config/systemd/user/ccviewer-tunnel*.service; do
        [ -f "$u" ] || continue
        systemctl --user disable --now "$(basename "$u")" >/dev/null 2>&1
        rm -f "$u"
    done
    systemctl --user daemon-reload 2>/dev/null
fi
if crontab -l 2>/dev/null | grep -q 'ccviewer'; then
    crontab -l 2>/dev/null | grep -v 'ccviewer' | crontab -
fi
pkill -f 'ccviewer/tunnel\.sh' 2>/dev/null
pkill -f 'ccviewer-tunnel-[^ ]*\.sh' 2>/dev/null
pkill -f 'ssh .*ccviewer-tunnel' 2>/dev/null

# ~/.ssh/config: our block (any version; an older one could glue its start marker onto the user's last line)
CFG="$HOME/.ssh/config"
if [ -f "$CFG" ] && grep -q '# >>> ccviewer' "$CFG"; then
    awk '
        !skip && /# >>> ccviewer[ \t]*$/ {
            pre = $0; sub(/# >>> ccviewer[ \t]*$/, "", pre)
            skip = 1; buf = $0 "\n"; keep = (pre ~ /[^ \t]/) ? pre "\n" : ""; next
        }
        skip { buf = buf $0 "\n"; if ($0 ~ /^# <<< ccviewer[ \t]*$/) { printf "%s", keep; skip = 0 } next }
        { print }
        END { if (skip) printf "%s", buf }
    ' "$CFG" > "$CFG.ccviewer-tmp" && cat "$CFG.ccviewer-tmp" > "$CFG"
    rm -f "$CFG.ccviewer-tmp"
fi
# the keys of Claude on the server (their comment is cc_viewer-gw-<id>); a read error must not truncate the file
AK="$HOME/.ssh/authorized_keys"
if [ -f "$AK" ] && grep -q ' cc_viewer-gw-[0-9a-z]*$' "$AK"; then
    grep -v ' cc_viewer-gw-[0-9a-z]*$' "$AK" > "$AK.ccviewer-tmp"
    [ $? -le 1 ] && cat "$AK.ccviewer-tmp" > "$AK"
    rm -f "$AK.ccviewer-tmp"
fi
rm -rf "$D" "$HOME"/.local/bin/ccviewer-tunnel*.sh "$HOME"/.ssh/ccviewer_tunnel_* "$HOME/.ssh/ccviewer_relay" "$HOME/.ssh/ccviewer_known_hosts"
case "$0" in /tmp/*|"${TMPDIR:-/nonexistent}"/*) rm -f "$0" ;; esac
echo 'Removed: this computer is no longer connected to cc-viewer.'
