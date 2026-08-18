# remote-shell

**Persistent** SSH shell sessions for Claude Code agents. A subagent uses a
remote machine as if it were local: `cd` and environment variables survive
across calls, and commands that ask for input on screen can be answered.

The difference from `ssh host 'command'` is that the latter opens a fresh shell
every time and loses all state.

## Install

```bash
claude --plugin-dir /path/to/remote-shell
```

## Tools

| | |
|---|---|
| `shell_open(session, host)` | Open the session. `host` is an SSH target, or `local`. |
| `shell_run(session, command, timeout)` | Run and wait. Returns output and exit code. |
| `shell_send(session, text)` | Write without waiting: for answering a prompt. |
| `shell_read(session, wait)` | Read whatever has arrived. |
| `shell_list()` / `shell_close(session)` | Housekeeping. |

Ships the **remote-operator** agent, already wired to these tools, and the
**remote-session** skill for deciding when to reach for one.

## How it works

Each session is a live `ssh -tt` with its own pty and a shell inside. Three
decisions solve the three problems of this approach:

1. **Delimiting output.** A pty is one continuous stream mixing echo, output and
   prompt. After each command a sentinel carrying a random token and the exit
   code is emitted, and we read until we find it.
2. **Quoting.** Commands travel base64-encoded and run through `eval`, so
   quotes, newlines and awkward characters stop mattering. Through `eval` rather
   than piping into `bash`, because a subshell would lose the `cd`.
3. **Commands that wait.** `shell_run` takes a timeout; when it expires it
   returns what was read, marked incomplete, and the session stays alive.

## Two things you only learn by measuring

Both were found by logging the real JSON-RPC traffic with `RSHELL_LOG`, not by
reading documentation.

**Tool names carry a double prefix.** Not `mcp__remote-shell__shell_run` but
`mcp__plugin_remote-shell_remote-shell__shell_run`, following
`mcp__plugin_<plugin>_<server>__<tool>`. An allowlist using the short name
leaves every call unpermitted.

**`server/discover` must be answered with an error.** Claude Code 2.1.234 sends
it before the classic `initialize`. Answer it with a well-formed result and the
client stays on that path, never sends `initialize`, loops on `tools/list`, and
**the tools never reach the model**. Returning `-32601` makes it fall back to
the classic handshake and everything works. It is commented in the source so
nobody "fixes" it by accident.

## Debugging

```bash
RSHELL_LOG=/tmp/mcp.log claude --plugin-dir .    # log all JSON-RPC
python3 server/test_core.py local                # 25 core tests, no network
python3 server/test_core.py user@host            # the same against a real machine
```

## Limits

- The server lives as long as the MCP client does: sessions do not survive
  closing Claude Code.
- Key-based authentication only (`BatchMode=yes`). It does not type SSH
  passwords.
- An `exit` inside a command closes the session. That is correct local-shell
  behaviour, and it is reported at the time.
