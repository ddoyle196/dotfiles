#!/usr/bin/env python3
"""Pin Claude-in-Chrome automation to the Chrome on the machine it runs on.

Several machines share one Claude account, so the extension's "last used"
default can silently route actions to another machine's browser. This hook makes the choice explicit:
every chrome tool is denied until select_browser has succeeded with a deviceId
that this machine's own extension storage claims as its bridgeDeviceId. Another
machine's id never exists in local storage, so it can never pass; a reinstall
that mints a new local id heals itself. Any lookup failure denies.
"""
import json
import os
import re
import sys
from pathlib import Path

PREFIX = "mcp__claude-in-chrome__"
EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
CHROME_ROOTS = [Path(os.environ["CHROME_PIN_CHROME_ROOT"])] if "CHROME_PIN_CHROME_ROOT" in os.environ else [
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    Path.home() / ".config" / "google-chrome",
]
MARKERS = Path(os.environ.get(
    "CHROME_PIN_MARKERS", Path.home() / ".claude" / "state" / "chrome-pin"))
ALWAYS_ALLOWED = {"list_connected_browsers"}

# LevelDB blocks are snappy-compressed, so the stored value can lose a few
# leading bytes to a back-reference. Require at least the last 28 characters
# (everything after the first UUID group) directly after the key.
MIN_SUFFIX = 28
LOCAL_ID = re.compile(
    rb"bridgeDeviceId.{0,12}?([0-9a-f-]{%d,36})\"" % MIN_SUFFIX, re.S)


def local_device_suffixes() -> set[str]:
    found: set[str] = set()
    for root in CHROME_ROOTS:
        for store in root.glob(f"*/Local Extension Settings/{EXTENSION_ID}"):
            for f in store.iterdir():
                if f.suffix in {".ldb", ".log"}:
                    found.update(m.decode() for m in LOCAL_ID.findall(f.read_bytes()))
    return found


def is_this_machine(device: str) -> bool:
    return any(device.endswith(s) for s in local_device_suffixes())


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main() -> None:
    event = json.load(sys.stdin)
    tool = event.get("tool_name", "")
    if not tool.startswith(PREFIX):
        return
    action = tool[len(PREFIX):]
    marker = MARKERS / event.get("session_id", "unknown")
    device = (event.get("tool_input") or {}).get("deviceId") or ""

    if event.get("hook_event_name") == "PostToolUse":
        if action == "select_browser" and is_this_machine(device):
            MARKERS.mkdir(parents=True, exist_ok=True)
            marker.touch()
        return

    if action in ALWAYS_ALLOWED:
        return
    if action == "switch_browser":
        deny("switch_browser pairs with any machine. Call list_connected_browsers, then select_browser with this machine's deviceId.")
    if action == "select_browser":
        if not is_this_machine(device):
            deny(f"deviceId {device} is not this machine's Chrome (not in local extension storage). Never drive another machine's browser.")
        return
    if not marker.exists():
        deny("No browser pinned this session. Call list_connected_browsers, then select_browser with this machine's deviceId.")


if __name__ == "__main__":
    try:
        main()
    except Exception as err:  # fail closed: an unreadable store must not allow anything
        deny(f"chrome-browser-pin failed ({err}); refusing browser access.")
