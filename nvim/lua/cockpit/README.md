# cockpit

One view of every Claude conversation: a sidebar you scan, and the conversation
you picked docked beside it.

## The split that matters

**nvim shows it, tmux holds it.** Each conversation runs in its own tmux session
on the default server; the cockpit is a client attached to one of them. Close
nvim, crash nvim, reboot the terminal — the conversations keep running, and
reopening the cockpit finds them where they were.

**A conversation is a file, not a process.** `~/.claude/cockpit/threads/<id>.json`
is the conversation; the tmux session is only how it happens to be running right
now. When that dies — crash, reboot, `/exit` — the entry stays, marked parked,
and opening it resumes from the Claude transcript. This is why a lost tmux server
costs nothing.

## Files

| file        | holds                                                        |
|-------------|--------------------------------------------------------------|
| `init.lua`  | the window layout, the key table, the help window, `:Cockpit` |
| `view.lua`  | the sidebar: ordering, rendering, every action                |
| `stage.lua` | the docked terminal, one buffer per conversation              |
| `host.lua`  | the only thing that talks to `~/.tmux/scripts/cc-host.sh`     |

## Where the states come from

Nothing is set by hand. `answer` and `running` are read live from
`claude agents --json`; the rest come from a one-line classification written by
the `Stop` hook into `~/.claude/session-index/<session>.json`, which is also
where the plain-English recap comes from. Parked conversations keep whatever
they were last seen holding.
