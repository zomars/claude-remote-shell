---
name: remote-session
description: Persistent shell session or one-shot ssh for working on another machine. Use before running commands over SSH when there is state to carry between steps, when sudo is needed, or when something will ask for input on screen.
---

# Session or one-shot

It all turns on one word: **state**. An `ssh host 'command'` is a one-shot —
fresh shell, blank state, gone when it finishes. A `remote-shell` session keeps
state across calls.

Open a session when there is state to carry:

- A `cd`, an `export`, an activated environment that later steps depend on.
- Something that waits for an answer: `sudo`, a confirmation, `passwd`,
  `ssh-copy-id`. A one-shot offers nowhere to answer.
- Half a dozen commands chained with `&&` to avoid losing context: that chain is
  the signal.
- A long-running process you want to watch.

Use `Bash` with `ssh -n` when there is none: a single command, or a sweep across
several machines. The `-n` is mandatory in loops — without it `ssh` eats the
loop's standard input and every host after the first vanishes from the results
with no error at all. To move files, `scp` or `rsync`.

## Before opening

A portable machine suspends while idle, and Wake-on-LAN does not travel over
wifi: asleep means someone has to switch it on by hand.

```bash
timeout 6 bash -c "echo > /dev/tcp/<ip>/22" && echo awake || echo asleep
```

A machine that does not answer is asleep. Treat it as such and carry on with the
rest.

Targets live in your machine inventory, not here. Read them from there.

## Operating the session

**Check the state, not the output.** A command that prints progress may have
done nothing. Close every change with an independent query that confirms it: if
you update packages, ask again what is still pending. One package updater
printed `Updating` twelve times and deployed nothing, because it aborted at the
end for lack of permissions; the second query is what exposed it.

**The bar for calling it done**: every change you made is backed by a later check
confirming it, or is declared unverified.

**`exit` closes the session.** Commands run in the current shell — that is what
makes `cd` persist — so an `exit` closes it the way it would close your terminal.
For a specific exit code while keeping the session: `sh -c 'exit N'`.

**A timeout means something is waiting for an answer.** `shell_run` returns
`[UNFINISHED]` with the partial output and the session alive: answer with
`shell_send` and read with `shell_read`.

**With passwords, work with the ones you were given.** When a step asks for one
you do not have, declare it pending human intervention and carry on with the
rest; a command waiting on a password occupies the whole session.

## Delegating

The **remote-operator** agent already comes wired to these tools. When writing
another agent or an allowlist, the tool names carry a double prefix:

```
mcp__plugin_remote-shell_remote-shell__shell_run
```

With the short name the calls go unpermitted and the agent reports that the
tools do not exist.
