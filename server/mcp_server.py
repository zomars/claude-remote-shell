#!/usr/bin/env python3
"""stdio MCP server exposing persistent remote shell sessions.

No dependencies: JSON-RPC 2.0 over stdin/stdout, one message per line.

On the handshake: `initialize` is answered with the SAME `protocolVersion` the
client asked for, rather than a hard-coded one. That is what keeps the server
working when the client moves to a newer revision of the spec. An unknown
method gets a proper JSON-RPC error rather than silence, which is what would
leave the client hanging.

Setting RSHELL_LOG=/path/to/file records every message in and out. Useful for
seeing what the client actually speaks, instead of assuming it.
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rshell_core import Manager, SessionError  # noqa: E402

LOG_PATH = os.environ.get("RSHELL_LOG")
MANAGER = Manager()


def log(direction, payload):
    if not LOG_PATH:
        return
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{direction} {json.dumps(payload, ensure_ascii=False)}\n")
    except Exception:
        pass


# --------------------------------------------------------------------- tools
TOOLS = [
    {
        "name": "shell_open",
        "description": (
            "Open a persistent shell session on a remote machine over SSH. The "
            "session keeps the working directory and environment variables "
            "across calls, so you use it like a local shell. If a live session "
            "with that name already exists, it is reused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "Name you will refer to the session by, e.g. 'web1'."},
                "host": {"type": "string", "description": "SSH target, e.g. 'user@192.0.2.10'. Use 'local' for a shell on this machine."},
            },
            "required": ["session", "host"],
        },
    },
    {
        "name": "shell_run",
        "description": (
            "Run a command in an open session and wait for it to finish. Returns "
            "the output and the exit code. On timeout it returns the partial "
            "output and the session stays alive: pair it with shell_send to "
            "answer whatever is asking for input, such as a sudo password."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "command": {"type": "string", "description": "Shell command. Multiple lines, quotes and pipes are all fine."},
                "timeout": {"type": "number", "description": "Seconds to wait. Defaults to 120."},
            },
            "required": ["session", "command"],
        },
    },
    {
        "name": "shell_send",
        "description": (
            "Write text into the session without waiting for anything to finish. "
            "For answering a command that is waiting for input. Returns no "
            "output; read it afterwards with shell_read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "text": {"type": "string"},
                "newline": {"type": "boolean", "description": "Append a newline. Defaults to true."},
            },
            "required": ["session", "text"],
        },
    },
    {
        "name": "shell_read",
        "description": "Return whatever the session has written since the last read.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "wait": {"type": "number", "description": "Seconds to wait for output. Defaults to 2."},
            },
            "required": ["session"],
        },
    },
    {
        "name": "shell_list",
        "description": "List open sessions, with their target and whether they are still alive.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "shell_close",
        "description": "Close a session and release the SSH connection.",
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string"}},
            "required": ["session"],
        },
    },
]


def execute(name, args):
    """Returns (text, is_error)."""
    if name == "shell_open":
        s, is_new = MANAGER.open(args["session"], args["host"])
        i = s.info()
        verb = "opened" if is_new else "reused"
        return f"Session '{i['name']}' {verb} on {i['target']}.", False

    if name == "shell_list":
        sessions = MANAGER.list()
        if not sessions:
            return "No sessions are open.", False
        return "\n".join(
            f"{i['name']}: {i['target']} · {'alive' if i['alive'] else 'dead'} · "
            f"opened {i['age_s']}s ago · idle {i['idle_s']}s"
            for i in sessions
        ), False

    if name == "shell_close":
        return ("Session closed." if MANAGER.close(args["session"])
                else f"There was no session named '{args['session']}'."), False

    s = MANAGER.get(args["session"])

    if name == "shell_run":
        r = s.run(args["command"], timeout=float(args.get("timeout") or 120))
        parts = [r["output"] or "(no output)"]
        if r.get("died"):
            parts.append(f"\n[SESSION DIED: {r['reason']}]")
            return "\n".join(parts), True
        if not r["complete"]:
            parts.append(
                "\n[UNFINISHED: the timeout expired. The command is still "
                "running or waiting for input. Use shell_send to answer it, or "
                "shell_read to keep reading.]"
            )
            return "\n".join(parts), False
        parts.append(f"\n[exit code: {r['code']}]")
        return "\n".join(parts), r["code"] != 0

    if name == "shell_send":
        s.send(args["text"], newline=args.get("newline", True))
        return "Sent. Use shell_read to see the response.", False

    if name == "shell_read":
        output = s.read(wait=float(args.get("wait") or 2))
        return output or "(nothing new)", False

    return f"Unknown tool: {name}", True


# ------------------------------------------------------------------ JSON-RPC
def reply(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(msg):
    method = msg.get("method")
    id_ = msg.get("id")

    # Notifications (no id) take no response. Answering one is a protocol error.
    if id_ is None:
        return None

    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return reply(id_, {
            # Echo back the version the client asked for, so there is no need to
            # chase every revision of the spec.
            "protocolVersion": asked,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "remote-shell", "version": "0.1.0"},
        })

    if method == "server/discover":
        # Claude Code 2.1.234 sends this BEFORE the classic initialize. Measured,
        # not assumed: the real traffic was logged.
        #
        # And there is a trap here that cost one attempt. Answer with a
        # WELL-FORMED discovery result (resultType, supportedVersions,
        # capabilities) and this client stays on that path, never sends
        # `initialize`, repeats `tools/list` over and over, and the tools never
        # reach the model. Declare the method unsupported instead and it falls
        # back to the classic handshake, and everything works.
        #
        # So the error below is deliberate. It is not an oversight: it is the
        # honest answer — this server does not implement that method — and also
        # the one that makes the client do the right thing.
        return error(id_, -32601, "server/discover not supported; use initialize")

    if method == "tools/list":
        return reply(id_, {"tools": TOOLS})

    if method == "ping":
        return reply(id_, {})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text, is_error = execute(name, args)
        except SessionError as e:
            text, is_error = str(e), True
        except KeyError as e:
            text, is_error = f"Missing required argument {e}", True
        except Exception:
            text, is_error = "Unexpected failure:\n" + traceback.format_exc(), True
        return reply(id_, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })

    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return reply(id_, {key: []})

    # Never stay silent: an unknown method with no response leaves the client
    # waiting forever.
    return error(id_, -32601, f"Unsupported method: {method}")


def main():
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log("<<malformed", {"line": line[:400]})
                continue
            log("<<", msg)
            out = handle(msg)
            if out is not None:
                log(">>", out)
                try:
                    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
                except BrokenPipeError:
                    # The client closed the pipe: a normal shutdown, not a
                    # failure. Without catching it the process dies on a
                    # traceback.
                    break
    finally:
        MANAGER.close_all()


if __name__ == "__main__":
    main()
