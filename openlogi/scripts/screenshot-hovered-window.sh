#!/bin/sh
# Open macOS's screenshot tool straight in window mode: hovered windows
# highlight, a click saves that window. Cmd+Shift+4 starts in region mode and
# needs Space to switch, which is what this skips.
#
# Backgrounded with output detached: OpenLogi waits for a command to exit
# before handling the next button, and the tool stays open until a click.

dir=$(defaults read com.apple.screencapture location 2>/dev/null)
case "$dir" in "~"*) dir="$HOME${dir#\~}" ;; esac
[ -d "$dir" ] || dir="$HOME/Desktop"
file="$dir/Screenshot $(date '+%Y-%m-%d at %H.%M.%S').png"

/usr/sbin/screencapture -iW "$file" >/dev/null 2>&1 &
