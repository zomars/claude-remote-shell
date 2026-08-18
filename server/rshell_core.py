"""Persistent SSH shell sessions an agent can drive.

The idea: one session is a live `ssh` process with its own pty and a shell
inside it. Commands run in THAT shell, so `cd`, environment variables and any
other state survive across calls. That is what makes it feel like a local
machine, unlike `ssh host 'command'`, which starts from scratch every time.

Three problems, and how they are solved here:

1. Knowing where a command's output ends. A pty delimits nothing: it is one
   continuous stream mixing the echo of what you typed, the output, and the
   prompt. Solution: after each command a sentinel carrying a random token and
   the exit status is emitted, and we read until we see it.

2. Commands containing quotes, newlines or awkward characters. Writing them
   straight to the shell is an endless source of quoting bugs. Solution: send
   them base64-encoded and run them through `eval`. Through `eval` rather than
   piping into `bash`, because a subshell would lose the `cd`.

3. Commands that sit waiting — a sudo password, say. Blocking forever is not an
   option. Solution: `run` takes a timeout and, when it expires, returns
   whatever was read so far marked as incomplete. The session stays alive, so
   you can keep reading or answer with `send`.
"""

import base64
import errno
import os
import pty
import re
import secrets
import select
import signal
import subprocess
import time

# The remote shell starts in dumb mode: no prompt, no echo. Without this, the
# output would arrive wrapped in prompts and in a copy of every command sent.
_SETUP = (
    "export PS1= PS2= PROMPT_COMMAND=; "
    "unset HISTFILE; "
    "stty -echo 2>/dev/null; "
    "export TERM=dumb; "
)

_STRIP_ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")


class SessionError(Exception):
    pass


class Session:
    """A live remote shell."""

    def __init__(self, name, target, ssh_command=None, startup_timeout=30):
        self.name = name
        self.target = target
        self.created = time.time()
        self.last_used = self.created
        self.closed = False
        self._buffer = b""

        if ssh_command is None:
            # -tt forces a pty even with no terminal on this side; without it
            # the remote shell is not interactive and prompts cannot be answered.
            ssh_command = [
                "ssh", "-tt",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=15",
                "-o", "StrictHostKeyChecking=accept-new",
                target,
            ]
        # 'local' is a special case, handy for testing without a network.
        if target == "local":
            ssh_command = ["bash", "--norc", "--noprofile", "-i"]

        self._master, slave = pty.openpty()
        try:
            self._proc = subprocess.Popen(
                ssh_command,
                stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, start_new_session=True,
            )
        finally:
            os.close(slave)

        os.set_blocking(self._master, False)

        # Prepare the shell and check that it answers. If ssh fails — host
        # asleep, key rejected — this catches it here rather than inside the
        # first useful command, where it would be confusing.
        self._write(_SETUP + "\n")
        r = self.run("printf ready", timeout=startup_timeout)
        if not r["complete"] or "ready" not in r["output"]:
            detail = (r["output"] or "").strip()[-400:]
            self.close()
            raise SessionError(
                f"could not open a shell on '{target}'. Last bytes: {detail!r}"
            )

    # ------------------------------------------------------------------ io
    def _alive(self):
        return self._proc.poll() is None

    def _write(self, text):
        data = text.encode()
        while data:
            try:
                n = os.write(self._master, data)
                data = data[n:]
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    select.select([], [self._master], [], 1)
                    continue
                raise SessionError(f"session '{self.name}' died: {e}")

    def _read_some(self, wait):
        ready, _, _ = select.select([self._master], [], [], wait)
        if not ready:
            return b""
        try:
            return os.read(self._master, 65536)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            return b""  # the pty closed: the caller deals with it

    # --------------------------------------------------------------- api
    def run(self, command, timeout=120):
        """Run a command and wait for the sentinel.

        Returns {'output', 'code', 'complete'}. When 'complete' is False the
        command is still running (or waiting for input) and 'code' is None: the
        output is partial and the session remains usable.
        """
        if self.closed or not self._alive():
            raise SessionError(f"session '{self.name}' is no longer alive")

        self.last_used = time.time()
        token = secrets.token_hex(8)
        b64 = base64.b64encode(command.encode()).decode()
        # eval in the current shell: this is what keeps cd and variables across
        # calls.
        line = (
            f'eval "$(printf %s {b64} | base64 -d)"; '
            f'__rc=$?; printf "\\n__RS_{token}_%d__\\n" "$__rc"\n'
        )
        self._write(line)

        pattern = re.compile(rb"__RS_" + token.encode() + rb"_(\d+)__")
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self._read_some(min(0.5, max(0.05, deadline - time.time())))
            if chunk:
                self._buffer += chunk
            m = pattern.search(self._buffer)
            if m:
                raw = self._buffer[: m.start()]
                self._buffer = self._buffer[m.end():]
                return {
                    "output": self._present(raw),
                    "code": int(m.group(1)),
                    "complete": True,
                }
            if not chunk and not self._alive():
                # The shell died mid-command. This really happens: `exit`,
                # `kill -9 $$`, or the connection dropping. Since `eval` runs in
                # the current shell — which is what makes `cd` persist — an
                # `exit` in the command closes the session exactly as it would
                # close a real terminal.
                #
                # Report it HERE, along with whatever output was produced,
                # rather than letting the next call blow up with an error that
                # can no longer be traced to its cause.
                raw, self._buffer = self._buffer, b""
                self.closed = True
                return {
                    "output": self._present(raw),
                    "code": None,
                    "complete": False,
                    "died": True,
                    "reason": "the shell exited during the command "
                              "(an 'exit', or the connection dropped); "
                              "open the session again",
                }

        raw, self._buffer = self._buffer, b""
        return {"output": self._present(raw), "code": None, "complete": False}

    def send(self, text, newline=True):
        """Write into the session without waiting for a sentinel.

        For answering whatever a command is asking for on screen: a password, a
        confirmation. Returns no output; use read() for that.
        """
        if self.closed or not self._alive():
            raise SessionError(f"session '{self.name}' is no longer alive")
        self.last_used = time.time()
        self._write(text + ("\n" if newline else ""))

    def read(self, wait=2.0):
        """Return whatever has arrived, without looking for a sentinel."""
        deadline = time.time() + wait
        while time.time() < deadline:
            chunk = self._read_some(min(0.3, max(0.05, deadline - time.time())))
            if chunk:
                self._buffer += chunk
            elif self._buffer:
                break
        raw, self._buffer = self._buffer, b""
        self.last_used = time.time()
        return self._present(raw)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self._alive():
                try:
                    self._write("exit\n")
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
            if self._alive():
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except Exception:
            pass
        finally:
            try:
                os.close(self._master)
            except Exception:
                pass

    # ------------------------------------------------------------ presentation
    @staticmethod
    def _present(raw):
        clean = _STRIP_ANSI.sub(b"", raw)
        return clean.decode("utf-8", errors="replace").strip("\n")

    def info(self):
        return {
            "name": self.name,
            "target": self.target,
            "alive": self._alive() and not self.closed,
            "age_s": round(time.time() - self.created, 1),
            "idle_s": round(time.time() - self.last_used, 1),
        }


class Manager:
    """The open sessions, by name."""

    def __init__(self):
        self._sessions = {}

    def open(self, name, target):
        s = self._sessions.get(name)
        if s and s.info()["alive"]:
            return s, False
        if s:
            s.close()
        s = Session(name, target)
        self._sessions[name] = s
        return s, True

    def get(self, name):
        s = self._sessions.get(name)
        if not s:
            open_ones = ", ".join(self._sessions) or "none"
            raise SessionError(f"no session named '{name}'. Open: {open_ones}")
        if not s.info()["alive"]:
            raise SessionError(f"session '{name}' died; open it again")
        return s

    def close(self, name):
        s = self._sessions.pop(name, None)
        if not s:
            return False
        s.close()
        return True

    def list(self):
        return [s.info() for s in self._sessions.values()]

    def close_all(self):
        for n in list(self._sessions):
            self.close(n)
