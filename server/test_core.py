"""Session core tests. Usage: python3 test_core.py [target]

With no argument it uses 'local' (bash right here), which needs no network.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rshell_core import Manager, SessionError  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "local"
failures = 0


def check(desc, condition, detail=""):
    global failures
    if condition:
        print(f"  ok   {desc}")
    else:
        failures += 1
        print(f"  FAIL {desc}   {detail}")


m = Manager()
print(f"\n=== target: {TARGET} ===")
t0 = time.time()
s, is_new = m.open("t", TARGET)
print(f"  (session opened in {time.time()-t0:.1f}s)\n")

# --- the whole point: does state survive between calls?
r = s.run("cd /tmp && pwd")
check("cd changes directory", r["output"].endswith("/tmp"), repr(r))
r = s.run("pwd")
check("the cd PERSISTS into the next call", r["output"].endswith("/tmp"), repr(r))

s.run("export TEST_VAR=hello123")
r = s.run("echo $TEST_VAR")
check("environment variables persist", r["output"] == "hello123", repr(r))

# --- exit codes
r = s.run("true")
check("exit code 0 on success", r["code"] == 0, repr(r))
r = s.run("sh -c 'exit 42'")
check("arbitrary exit code (42)", r["code"] == 42, repr(r))
r = s.run("false")
check("exit code 1 on failure", r["code"] == 1, repr(r))
r = s.run("echo still-alive")
check("session survives a non-zero exit code", r["output"] == "still-alive", repr(r))

# A bare 'exit' DOES close the session, and it should: eval runs in the current
# shell, which is exactly what makes the cd persist. What is checked here is
# that it gets reported at the time, not on the following call.
s2, _ = m.open("suicidal", TARGET)
r = s2.run("echo before; exit 7")
check("an 'exit' is reported as a dead session", r.get("died") is True, repr(r))
check("and keeps the output produced before it", "before" in r["output"], repr(r))
m.close("suicidal")

# --- quoting: what breaks the naive approaches
cases = [
    ("""echo 'single quotes'""", "single quotes"),
    ('''echo "double with $HOME"''', None),
    ("""printf '%s\\n' 'a"b'"'"'c'""", 'a"b\'c'),
    ("echo line1; echo line2", "line1\nline2"),
    ("printf 'no newline'", "no newline"),
]
for cmd, expected in cases:
    r = s.run(cmd)
    if expected is None:
        check(f"quoting: {cmd[:34]}", r["code"] == 0, repr(r))
    else:
        check(f"quoting: {cmd[:34]}", r["output"] == expected, repr(r))

# --- multi-line command
r = s.run("for i in 1 2 3\ndo\n  echo n$i\ndone")
check("multi-line command", r["output"] == "n1\nn2\nn3", repr(r))

# --- large output (must not truncate or interleave)
r = s.run("seq 1 5000 | tail -1")
check("large output intact", r["output"] == "5000", repr(r))

# --- timeout: what makes answering a sudo prompt possible
r = s.run("sleep 30", timeout=2)
check("timeout returns incomplete", r["complete"] is False, repr(r))
check("timeout yields no exit code", r["code"] is None, repr(r))
s.send("\x03")          # Ctrl-C to kill the sleep
time.sleep(0.5)
s.read(1.0)
r = s.run("echo recovered", timeout=10)
check("session recovers after a timeout", r["output"].endswith("recovered"), repr(r))

# --- interactive input
s.run("true")
s.send("read -r X; echo got:$X")
time.sleep(0.3)
s.send("value42")
time.sleep(0.4)
output = s.read(2.0)
check("send() answers a command waiting for input", "got:value42" in output, repr(output))

# --- session management
check("list sees the session", len(m.list()) == 1, repr(m.list()))
try:
    m.get("nosuch")
    check("asking for a missing session fails", False)
except SessionError:
    check("asking for a missing session fails", True)

check("close returns True", m.close("t") is True)
check("nothing left after closing", m.list() == [])

print(f"\n  failures: {failures}")
sys.exit(1 if failures else 0)
