# Dotfiles

Personal configuration for tmux and neovim. Cross-platform support for macOS, Linux, and Windows.

## Quick Start

```bash
git clone git@github.com:YOUR_USERNAME/dotfiles.git ~/dotfiles
cd ~/dotfiles
./setup.sh        # macOS / Linux / WSL / Git Bash
```

**Windows (PowerShell):**
```powershell
git clone git@github.com:YOUR_USERNAME/dotfiles.git $HOME\dotfiles
cd $HOME\dotfiles
.\setup.ps1
```

## Platform Support

| Platform | Neovim | Tmux | Script |
|----------|--------|------|--------|
| macOS | ✅ | ✅ | `setup.sh` |
| Linux | ✅ | ✅ | `setup.sh` |
| WSL | ✅ | ✅ | `setup.sh` |
| Windows (Git Bash) | ✅ | ❌ | `setup.sh` |
| Windows (PowerShell) | ✅ | ❌ | `setup.ps1` |

> **Note:** Tmux doesn't run natively on Windows. Use WSL for full tmux support.

## Config Locations

| Platform | Neovim | Tmux |
|----------|--------|------|
| macOS/Linux/WSL | `~/.config/nvim` | `~/.tmux.conf` |
| Windows | `%LOCALAPPDATA%\nvim` | N/A |

## Windows Requirements

For symlinks on Windows, you need **one** of:
- **Developer Mode** enabled (Settings → Privacy & Security → For developers)
- Run PowerShell/Terminal as **Administrator**

## Cleanup

Remove symlinks and restore original configs:

```bash
./cleanup.sh        # macOS / Linux / WSL / Git Bash
```

```powershell
.\cleanup.ps1       # Windows PowerShell
```

## Structure

```
dotfiles/
├── nvim/           # Neovim config (LazyVim)
├── tmux.conf       # Tmux config
├── openlogi/       # OpenLogi (Logitech mouse) config and helpers
├── setup.sh        # Unix setup script
├── setup.ps1       # Windows PowerShell setup
├── cleanup.sh      # Unix cleanup script
└── cleanup.ps1     # Windows PowerShell cleanup
```

## OpenLogi (MX Master 4)

[OpenLogi](https://github.com/AprilNEA/OpenLogi) replaces Logi Options+: open
source, local, and on macOS and Linux. `setup.sh` installs it on macOS, links
`openlogi/config.toml` into `~/.config/openlogi/`, builds the thumb wheel helper,
and disables Logi Options+ (the two fight over the mouse, and Logi relaunches
itself when quit). Logi stays installed; `cleanup.sh` prints how to turn it back on.

| Control | Does |
|---|---|
| Back / Forward | Return / Ctrl+Cmd+O |
| Gesture button | screenshot tool in window mode (hover highlights, click saves) |
| Button under the wheel | toggle ratchet / free-spin |
| Haptic panel | Actions Ring: cut, copy, paste, forward, back, undo, redo |
| Thumb wheel up / down | Mission Control / App Exposé, and either one closes them |

The thumb wheel runs `scripts/thumbwheel-key.c`, compiled per machine, instead of
OpenLogi's own shortcut action: that action leaves out the fn flag macOS needs for
Ctrl+arrow hotkeys, and `osascript` launched from OpenLogi takes ~2.5s per call.

After setup, give **OpenLogi Agent** Input Monitoring, Accessibility and Screen
Recording in System Settings → Privacy & Security, then quit and reopen OpenLogi.
The config is keyed to the mouse's serial, so the same mouse works on any machine.

The thumb wheel helper and the screenshot script are macOS-only. On Linux the
config links but those two bindings do nothing yet.

## Post-Setup

- **Tmux**: Press `prefix + I` to install plugins via TPM
- **Neovim**: Plugins auto-install on first launch via lazy.nvim

## Claude cockpit

A list of your Claude Code conversations on the left, the one you picked on the
right. `prefix + a` opens it; the list follows your cursor, so moving down the
list swaps the conversation beside it.

A conversation is a registry file under `~/.claude/cockpit/`, not a process. The
tmux session is only how it happens to be running right now — when that dies the
entry stays, and `enter` resumes it from its transcript.

| | |
|---|---|
| `enter` | open it (resumes a parked one) |
| `a` | reply without leaving the list |
| `/` | fuzzy jump by name, then by what was said |
| `t` / `n` | new topic / new conversation |
| `x` / `d` | park (stop the process) / remove |
| `u` | tokens and time per topic |
| `?` | the full legend lives in the status bar |

`/` matches labels as you type, and when the query names nothing it searches what
was *said* instead, showing the line it was found on. Deleting back to something
that does name a conversation returns to names, so one prompt covers both and
there is no second key to remember — `ctrl-g` forces either mode for when a name
matches but the words you want are inside it.

cc-harvest.py keeps the prose of each conversation in a file of its own for this:
a transcript is about 99% tool calls, tool output and thinking, so the part worth
searching is small enough to scan on a pause in typing. Removed conversations are
searched too and marked `⌕`; `enter` offers to bring one back.

Rows show what each conversation needs from you (`answer`, `pick up`, `waiting`,
`done`), whether it has spoken since you last looked, and any pull requests it
has worked on.

A conversation starts unfiled. `t` makes a topic when you want one and `T` files
a conversation into it once you can see what it turned out to be, rather than
asking you to name a category before the work exists.

Claude Code's own `/branch` splits a conversation in two: it copies the history
and moves you into the copy. The cockpit follows that move, so a `SessionStart`
hook registers the conversation you branched away from as a parked row of its
own, keeping the label it already had. `/branch cars` names the tangent you are
now in; unnamed, it becomes `… (branch)`. One gesture, two rows, and the train of
thought you left is one `enter` away.

Each conversation remembers the directory it was started in and resumes there,
so one cockpit can span several projects. `setup.sh` asks once where *new* ones
should start and records it in `~/.claude/cockpit/dir`; `COCKPIT_DIR` in the
environment overrides it. Nothing needs adding to a shell rc - the tmux server
usually has a stale environment anyway.

Working below a subdirectory that has its own `CLAUDE.md` loads it on demand, so
a cockpit rooted at a parent still picks up each subproject's conventions.

```sh
./setup.sh          # links everything, registers the hooks, asks for the directory
cockpit doctor      # checks this machine has what the cockpit needs
```

Needs `tmux`, `jq`, `python3` and the `claude` CLI. `gh` is optional and only
used for pull-request badges. Nothing about the cockpit is synced except the
code: the conversations themselves are per-machine, so it starts empty.
