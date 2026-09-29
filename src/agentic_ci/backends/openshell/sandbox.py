"""OpenShell sandbox lifecycle management."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import yaml

from agentic_ci import log
from agentic_ci.backends.openshell.policy import (
    BASE_POLICY,
    EGRESS_PHASES,
    resolve_endpoints,
)
from agentic_ci.backends.openshell.provider import PROVIDER_NAME, requires_provider
from agentic_ci.sandbox_profile import SandboxProfile, is_raw_endpoint

SANDBOX_NAME = "ci"

AGENT_BINARY_PATHS = (
    "/usr/local/bin/claude",
    "/usr/local/sbin/claude",
    "/usr/local/bin/opencode",
    "/usr/local/sbin/opencode",
    "/usr/local/bin/codex",
    "/usr/local/sbin/codex",
)

# Setup and validate steps run under this shim, shipped in the sandbox images.
# OpenShell matches a connection to rules by the caller's executable and its
# parent chain, so rules bound to the shim apply to a step and everything it
# starts, and to nothing else. The shim forks and waits instead of exec-ing,
# so it stays in the parent chain.
#
# The shim lets setup and validate reach preset hosts without widening the
# agent's egress. It is an extra layer, not isolation: any sandbox process can
# run it, including one the agent left behind, and it would get the shim's
# egress whenever shim rules are live. What keeps the agent out is that shim
# rules exist only in the setup and validate phases, the agent's rules are
# parked then, every process left from an earlier exec is killed before a
# shim phase opens (stop_leftover_processes), and the credential provider is
# detached while it is open (detach_provider).
SANDBOX_SETUP_SHIM = "/usr/local/bin/agentic-ci-sandbox-setup"

# Rules the phase switch adds are named PHASE_RULE_PREFIX + index. Any rule
# with this prefix is replaced on every switch.
PHASE_RULE_PREFIX = "agentic_ci_phase_"

# Rules OpenShell composes for an attached provider. They are not part of
# ``policy get --base`` and the server refuses them in ``policy set``.
_PROVIDER_RULE_PREFIX = "_provider_"

# While the setup shim's egress is open (the setup and validate phases), every
# agent binary path in a user rule is rewritten to this prefix plus the path,
# so the rule stays in the policy with its credential bindings but matches no
# process: a step cannot reach agent egress by running an agent binary under
# the shim. The agent phase strips the prefix again. Nothing can be created
# under /proc, so no executable can ever have a parked path.
PARKED_BINARY_PREFIX = "/proc/agentic-ci-parked"

_ALLOWED_IP_OPTION = "allowed-ip="


def _run(args, **kwargs):
    """Run an openshell command with logging."""
    log.detail("exec", " ".join(args))
    return subprocess.run(args, **kwargs)


def exists():
    """Check if the sandbox already exists."""
    result = _run(
        ["openshell", "sandbox", "get", SANDBOX_NAME],
        capture_output=True,
    )
    return result.returncode == 0


def create(
    image: str | None = None,
    policy_path: str | None = None,
    otel_port: int | None = None,
    workdir: str = ".",
    approval_mode: str | None = None,
    auth_mode: str | None = None,
    memory: str | None = None,
    cpu: str | None = None,
    gpu: int | None = None,
    profile: SandboxProfile | None = None,
) -> None:
    """Create a persistent sandbox with the CI provider attached, if *auth_mode* uses one.

    The sandbox is created first, then the network policy is applied
    via ``openshell policy update --wait`` to ensure the supervisor
    has compiled and activated the rules before the agent starts.

    ``memory``, ``cpu`` and ``gpu`` size the sandbox. All three default to
    ``None``, which passes no resource flag and leaves the effective limits to
    the compute driver and host configuration. An agent that exceeds a memory
    limit is OOM-killed by the cgroup, and from inside the sandbox that looks
    like the sandbox simply vanishing: the supervisor's log stops mid-line and
    the next command reports ``sandbox is not ready``.

    Args:
        image: Sandbox image to create from.
        policy_path: Path to a network policy file to merge in.
        otel_port: Port for the OTEL collector on the host.
        workdir: Directory the policy file is resolved relative to.
        approval_mode: Enables agent policy proposals when set.
        memory: Memory limit, e.g. ``"8Gi"``. None uses OpenShell's default.
        cpu: CPU limit, e.g. ``"4"`` or ``"2.5"``. None uses OpenShell's default.
        gpu: GPU count to request, e.g. ``1``. None requests no GPU, which
            means an accelerator on the host is not visible to the agent even
            when the container running this can see it.
        profile: Sandbox profile whose agent-phase egress is added to the
            policy. None keeps the policy exactly as without profiles.
    """
    args = [
        "openshell",
        "sandbox",
        "create",
        "--name",
        SANDBOX_NAME,
        "--no-tty",
        "--no-auto-providers",
    ]
    if requires_provider(auth_mode):
        args.extend(["--provider", PROVIDER_NAME])
    if approval_mode:
        args.extend(["--approval-mode", approval_mode])
    if image:
        args.extend(["--from", image])
    if memory:
        args.extend(["--memory", str(memory)])
    if cpu:
        args.extend(["--cpu", str(cpu)])
    if gpu:
        args.extend(["--gpu", str(gpu)])
    # Always hand the supervisor an explicit base policy; see BASE_POLICY
    # for why image policy discovery must not run.
    fd, base_policy_file = tempfile.mkstemp(prefix="agentic-ci-base-policy-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(BASE_POLICY, fh, default_flow_style=False)
        args.extend(["--policy", base_policy_file])
        # The trailing argv becomes the sandbox's canonical main process.
        # Use a persistent process so the supervisor stays alive to accept
        # policy updates; --detach returns control to the caller immediately.
        args.extend(["--detach", "--", "sleep", "infinity"])
        _run(args, check=True)
    finally:
        os.unlink(base_policy_file)

    if approval_mode:
        _run(
            [
                "openshell",
                "settings",
                "set",
                SANDBOX_NAME,
                "--key",
                "agent_policy_proposals_enabled",
                "--value",
                "true",
            ],
            check=True,
        )

    _apply_policy(
        policy_path,
        otel_port=otel_port,
        workdir=workdir,
        auth_mode=auth_mode,
        profile=profile,
    )


def _apply_policy(policy_path, otel_port=None, workdir=".", auth_mode=None, profile=None):
    """Apply network policy endpoints and wait for activation.

    Uses ``openshell policy update`` to add endpoints incrementally, which
    preserves filesystem_policy and the other static fields of the base
    policy. The inference endpoints and credential resolution of an
    attached provider come from the rule OpenShell composes from its
    profile, so nothing is bound to the provider here: OpenShell refuses a
    sandbox policy that binds a provider whose profile declares endpoints.
    """
    endpoints = resolve_endpoints(
        policy_path, workdir=workdir, auth_mode=auth_mode, profile=profile
    )
    if otel_port:
        endpoints.append(f"host.openshell.internal:{otel_port}:read-write")
    if not endpoints:
        return

    args = [
        "openshell",
        "policy",
        "update",
        "--wait",
    ]
    for binary_path in AGENT_BINARY_PATHS:
        args.extend(["--binary", binary_path])
    for ep in endpoints:
        args.extend(["--add-endpoint", ep])
    args.append(SANDBOX_NAME)
    _run(args, check=True)


def _endpoint_to_policy(spec, index):
    """Convert one ``host:port:access[:protocol[:enforcement[:options]]]`` string.

    Returns the endpoint mapping a policy file uses (the shape
    ``openshell policy get -o json`` prints). The grammar is the one a central
    profile's raw egress must follow (:func:`agentic_ci.sandbox_profile.is_raw_endpoint`).
    Only ``allowed-ip=`` options are accepted: the credential options are
    never given to a setup-shim rule. Raises ``ValueError`` naming only the
    endpoint's position, never its text.
    """
    if not is_raw_endpoint(spec):
        raise ValueError(
            f"phase endpoint {index} is not host:port:access[:protocol[:enforcement[:options]]]"
        )
    host, port, access, protocol, enforcement, options = (spec.split(":") + [""] * 6)[:6]
    endpoint = {"host": host, "port": int(port), "access": access}
    if protocol:
        endpoint["protocol"] = protocol
    if enforcement:
        endpoint["enforcement"] = enforcement
    allowed_ips = []
    for option in options.split(",") if options else ():
        if not option.startswith(_ALLOWED_IP_OPTION):
            raise ValueError(
                f"phase endpoint {index} has a credential option, "
                "which a setup-shim rule never gets"
            )
        value = option[len(_ALLOWED_IP_OPTION) :]
        if value not in allowed_ips:
            allowed_ips.append(value)
    if allowed_ips:
        endpoint["allowed_ips"] = allowed_ips
    return endpoint


def _rebind(rule, park):
    """Return *rule* with its ``binaries`` adjusted for a phase, or None to drop it.

    Removes :data:`SANDBOX_SETUP_SHIM`, dropping the rule only when the shim
    was its only binary. With *park*, every agent binary path gets
    :data:`PARKED_BINARY_PREFIX`; without it, parked paths get their original
    path back (without duplicating one that is already there). A rule is
    never turned into one with an empty ``binaries`` list, whose meaning
    differs across OpenShell versions (it depends on the release and on
    ``require_binary_identity``: no binary at all, or any binary); a rule
    that already had no binaries is left as it is.
    """
    binaries = rule.get("binaries") if isinstance(rule, dict) else None
    if not isinstance(binaries, list) or not binaries:
        return rule
    kept = []
    for binary in binaries:
        path = binary.get("path") if isinstance(binary, dict) else None
        if path == SANDBOX_SETUP_SHIM:
            continue
        if isinstance(path, str):
            if park and path in AGENT_BINARY_PATHS:
                binary = {**binary, "path": PARKED_BINARY_PREFIX + path}
            elif not park and path.startswith(PARKED_BINARY_PREFIX + "/"):
                binary = {**binary, "path": path[len(PARKED_BINARY_PREFIX) :]}
            if any(isinstance(b, dict) and b.get("path") == binary["path"] for b in kept):
                continue
        kept.append(binary)
    if not kept:
        return None
    rule["binaries"] = kept
    return rule


def build_phase_policy(base_get_output, endpoints: Iterable[str], *, park_agent=False):
    """Build the policy for an egress phase from ``openshell policy get --base -o json``.

    Takes the ``.policy`` object, deep-copies it, and in ``network_policies``:

    1. drops every rule named with :data:`PHASE_RULE_PREFIX` (the previous
       phase's rules) or ``_provider_`` (composed by the server);
    2. removes :data:`SANDBOX_SETUP_SHIM` from every other rule's
       ``binaries``, dropping a rule only when the shim was its only binary;
    3. with *park_agent* (the setup and validate phases), parks every agent
       binary path under :data:`PARKED_BINARY_PREFIX` so no process matches
       the agent's rules while the shim's egress is open; without it (the
       agent phase), restores the parked paths;
    4. adds one rule per endpoint, in order and de-duplicated, named
       ``PHASE_RULE_PREFIX + index`` and bound to the shim only.

    One rule per endpoint mirrors the rules ``openshell policy update``
    creates and the shape the September 2026 spike verified, and keeps each
    endpoint's options independent. Every other field (``version``,
    ``filesystem_policy``, ``landlock``, ``process``, rule names and endpoints,
    and the credential bindings on user rules) is returned unchanged:
    ``openshell policy set`` needs the whole object and refuses to drop the
    static fields of a live sandbox.

    Returns the raw policy dict for ``openshell policy set``. Raises
    ``ValueError`` when ``.policy`` or its ``network_policies`` is missing or
    an endpoint is malformed; the message never includes policy content.
    """
    raw_policy = base_get_output.get("policy") if isinstance(base_get_output, dict) else None
    if not isinstance(raw_policy, dict):
        raise ValueError("policy get output has no 'policy' object")
    if not isinstance(raw_policy.get("network_policies"), dict):
        raise ValueError("policy get output has no 'network_policies' mapping")

    new_rules = [
        _endpoint_to_policy(spec, index) for index, spec in enumerate(dict.fromkeys(endpoints))
    ]

    policy = copy.deepcopy(raw_policy)
    rules = {}
    for name, rule in policy["network_policies"].items():
        if str(name).startswith((PHASE_RULE_PREFIX, _PROVIDER_RULE_PREFIX)):
            continue
        kept = _rebind(rule, park_agent)
        if kept is not None:
            rules[name] = kept
    for index, endpoint in enumerate(new_rules):
        name = f"{PHASE_RULE_PREFIX}{index}"
        rules[name] = {
            "name": name,
            "endpoints": [endpoint],
            "binaries": [{"path": SANDBOX_SETUP_SHIM}],
        }
    policy["network_policies"] = rules
    return policy


def _log_stderr(result):
    stderr = (result.stderr or "").strip()
    if stderr:
        log.detail("openshell stderr", stderr)


# Bounds for the gateway calls of a phase switch, so a gateway that stops
# answering fails the switch instead of hanging the run. `policy set --wait`
# waits up to its own 60 s default for the supervisor to load the policy.
_POLICY_GET_TIMEOUT_SECONDS = 30
_POLICY_SET_TIMEOUT_SECONDS = 90


def apply_phase_policy(phase, endpoints):
    """Switch the sandbox's setup-shim egress to *phase*.

    Reads the base policy, builds the phase policy with
    :func:`build_phase_policy` and applies the whole object with
    ``openshell policy set --wait``. ``openshell policy update`` is never
    used here: it folds an endpoint whose host overlaps an existing rule into
    that rule, where the shim could not be removed again.

    *phase* is one of ``setup``, ``validate`` or ``agent``. ``setup`` and
    ``validate`` also park the agent's rules (see :data:`PARKED_BINARY_PREFIX`),
    so running an agent binary under the shim gains nothing. ``agent`` strips
    every shim-bound rule, restores the agent's rules and takes no endpoints.
    Any policy change closes every open proxied connection in the sandbox,
    so switch only while nothing runs there. Nothing is applied when the
    policy would not change, so switching to the phase already in effect is
    free.

    Raises ``RuntimeError`` when an ``openshell`` command fails or prints
    unusable output. The message names the step and the failure class only;
    stderr goes to the job log.
    """
    if phase not in EGRESS_PHASES:
        raise ValueError(f"phase must be one of {', '.join(EGRESS_PHASES)}, not {phase!r}")
    endpoints = list(endpoints)
    if phase == "agent" and endpoints:
        raise ValueError("the agent phase opens no setup-shim endpoints")

    try:
        result = _run(
            ["openshell", "policy", "get", "--base", "-o", "json", SANDBOX_NAME],
            capture_output=True,
            text=True,
            timeout=_POLICY_GET_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: openshell policy get "
            f"timed out ({type(exc).__name__}); see the job log"
        ) from exc
    if result.returncode != 0:
        _log_stderr(result)
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: openshell policy get "
            f"exited with status {result.returncode}; see the job log"
        )
    try:
        current = json.loads(result.stdout)
        policy = build_phase_policy(current, endpoints, park_agent=phase != "agent")
    except ValueError as exc:
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: unusable policy get output "
            f"({type(exc).__name__}); see the job log"
        ) from exc

    if policy == current["policy"]:
        log.info(f"Egress phase {phase}: policy unchanged")
        return

    fd, policy_file = tempfile.mkstemp(prefix="agentic-ci-phase-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(policy, fh, default_flow_style=False)
        result = _run(
            ["openshell", "policy", "set", "--wait", "--policy", policy_file, SANDBOX_NAME],
            capture_output=True,
            text=True,
            timeout=_POLICY_SET_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: openshell policy set "
            f"timed out ({type(exc).__name__}); see the job log"
        ) from exc
    finally:
        os.unlink(policy_file)
    if result.returncode != 0:
        _log_stderr(result)
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: openshell policy set "
            f"exited with status {result.returncode}; see the job log"
        )
    opened = len(dict.fromkeys(endpoints))
    log.info(f"Egress phase {phase}: {opened} setup-shim endpoint(s) open")


# In-sandbox helpers run with the image's own interpreter, by absolute path:
# PATH starts with /sandbox/.local/bin, which the agent can write. -I ignores
# PYTHON* variables, the user site and the working directory; -S skips site.
_SANDBOX_PYTHON = ("/usr/bin/python3", "-I", "-S", "-c")

# Every internal exec passes --no-login-shell. Without it OpenShell runs the
# command as "bash -lc", which sources the agent-writable ~/.bash_profile
# first: code the agent left there could define a shell function named
# /usr/bin/python3 that forges the scan's or the probe's output, or set an
# EXIT trap that starts a daemon once the scan is done. With it the command
# runs under "bash -c", which reads no startup file. A supervisor too old to
# honor the flag refuses the exec (non-zero exit), which fails closed. Setup
# and validate steps, once they run through the shim, need it as well.
_INTERNAL_EXEC = ("openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty")
_NO_LOGIN_SHELL = "--no-login-shell"

# Finds or kills the sandbox user's processes from inside the sandbox.
# OpenShell has no per-exec kill: a process an exec leaves running (a
# "nohup ... &", a setsid daemon) survives the exec, reparented to the
# supervisor. Run as the sandbox user, this sees exactly the processes that
# user can signal.
#
#   main          print "MAIN <pid> <start_time>" for the sandbox's main
#                 process: the only process of this user whose parent is
#                 this exec's parent (the supervisor), which must be
#                 "sleep infinity" (its command line is printed for the job
#                 log; no agent code has run yet). Exits 3 when there is
#                 none yet, 5 when there is more than one, 6 when it is
#                 something else.
#   kill PID START DEADLINE
#                 SIGKILL every other process of this user, except this one
#                 and its ancestors, then scan again until none is left or
#                 DEADLINE seconds pass (exit 4). PID is spared only while
#                 its start time is START. When it is not, the record is
#                 stale (the supervisor exits with its entrypoint, so a live
#                 sandbox still has it), and the scan kills nothing and
#                 exits 7: killing the real main process would take the
#                 sandbox down.
#
# Processes are signalled one by one, never by process group or session:
# exec'd commands share the supervisor's process group and session (1), and
# kill(-1) would signal every process, the main one included. Stopping each
# round's processes before killing them keeps a watcher from respawning the
# ones it sees die. A zombie (every thread exited) counts as gone. Only
# ENOENT and ESRCH mean a process is gone; a /proc entry that cannot be read
# for any other reason counts as a live process of this user, so the scan
# fails closed. Process names are printed with ascii() for the job log only.
# The definitions are kept apart so tests can load them without running the
# scan.
_PROCESS_HELPERS = r"""
import errno
import os
import signal
import sys
import time

UID = os.getuid()
SELF = os.getpid()
GONE = (errno.ENOENT, errno.ESRCH)
UNREADABLE = "?"


def stat(path):
    try:
        with open(f"/proc/{path}/stat", "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return None if exc.errno in GONE else UNREADABLE
    head, _, tail = data.rpartition(b")")
    fields = tail.split()
    if not head or len(fields) < 20:
        return UNREADABLE
    comm = head.partition(b"(")[2].decode("utf-8", "replace")
    return fields[0].decode(), int(fields[1]), int(fields[19]), comm


def owned(pid):
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return UID in [int(u) for u in line.split()[1:]]
    except OSError as exc:
        if exc.errno in GONE:
            return False
    except ValueError:
        pass
    return True


def alive(pid):
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError as exc:
        return exc.errno not in GONE
    for tid in tids:
        info = stat(f"{pid}/task/{tid}")
        if info == UNREADABLE or (info is not None and info[0] not in ("Z", "X")):
            return True
    return False


def lineage():
    seen = set()
    pid = SELF
    while pid > 0 and pid not in seen:
        seen.add(pid)
        info = stat(pid)
        if not isinstance(info, tuple):
            break
        pid = info[1]
    return seen


def others(spared):
    for name in os.listdir("/proc"):
        if name.isdigit() and int(name) not in spared and owned(int(name)):
            yield int(name)


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def is_sleep_infinity(data):
    argv = data.split(b"\0")
    if argv and argv[-1] == b"":
        argv.pop()
    name = [os.path.basename(arg) for arg in argv]
    if len(argv) == 2:
        return name[0] == b"sleep" and argv[1] == b"infinity"
    # A multi-call coreutils (the Hummingbird images) installs sleep as a
    # "#!/usr/bin/coreutils --coreutils-prog-shebang=sleep" script.
    return (
        len(argv) == 4
        and name[0] == b"coreutils"
        and argv[1] == b"--coreutils-prog-shebang=sleep"
        and name[2] == b"sleep"
        and argv[3] == b"infinity"
    )
"""

_PROCESS_SCRIPT = (
    _PROCESS_HELPERS
    + r"""

spared = lineage()
if sys.argv[1] == "main":
    parent = stat(SELF)[1]
    found = []
    for pid in others(spared):
        info = stat(pid)
        if isinstance(info, tuple) and info[1] == parent and alive(pid):
            found.append((info[2], pid))
    print(f"CANDIDATES {len(found)}")
    if not found:
        print("NO_MAIN")
        sys.exit(3)
    if len(found) > 1:
        print("AMBIGUOUS")
        sys.exit(5)
    start, pid = found[0]
    data = cmdline(pid)
    print(f"CMDLINE {ascii(data)}")
    if not is_sleep_infinity(data):
        print("NOT_SLEEP")
        sys.exit(6)
    print(f"MAIN {pid} {start}")
    sys.exit(0)

main_pid, main_start = int(sys.argv[2]), int(sys.argv[3])
info = stat(main_pid)
if not (isinstance(info, tuple) and info[2] == main_start and alive(main_pid)):
    print("MAIN_GONE")
    sys.exit(7)
spared.add(main_pid)
killed = {}
deadline = time.monotonic() + float(sys.argv[4])
while True:
    left = [pid for pid in others(spared) if alive(pid)]
    if not left or time.monotonic() > deadline:
        break
    for pid in left:
        info = stat(pid)
        killed.setdefault(pid, info[3] if isinstance(info, tuple) else "?")
    for sig in (signal.SIGSTOP, signal.SIGKILL):
        for pid in left:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
    time.sleep(0.1)
for pid, comm in sorted(killed.items()):
    print(f"KILLED pid={pid} comm={ascii(comm)}")
print(f"LEFT {len(left)}")
sys.exit(4 if left else 0)
"""
)

# Exit statuses of _PROCESS_SCRIPT, and what each means for the error text.
_SCAN_NO_MAIN = 3
_SCAN_SURVIVORS = 4
_SCAN_MAIN_GONE = 7
_SCAN_FAILURES = {
    _SCAN_NO_MAIN: "no process was found",
    5: "more than one candidate process was found",
    6: "the only candidate is not the sandbox's sleep infinity",
    _SCAN_MAIN_GONE: (
        "the recorded main process is not running, so the record is stale; "
        "run agentic-ci stop to recreate the sandbox"
    ),
}

# How long the in-sandbox scan keeps killing before it gives up, and how long
# the host waits for the exec as a whole. A leftover can stop or kill the
# scan itself (same user), which then fails closed on the timeout.
_KILL_DEADLINE_SECONDS = 10
_PROCESS_EXEC_TIMEOUT_SECONDS = 60

# How long find_main_process() waits for the entrypoint to be spawned after
# ``sandbox create --detach`` returns, and how often it looks.
_MAIN_WAIT_SECONDS = 10
_MAIN_POLL_SECONDS = 1


@dataclass(frozen=True)
class MainProcess:
    """The sandbox's canonical main process (``sleep infinity`` from :func:`create`).

    ``start_time`` is field 22 of ``/proc/<pid>/stat`` (clock ticks after
    boot). Together with the PID it identifies the process even if the agent
    names another process ``sleep infinity`` or the PID is reused.
    """

    pid: int
    start_time: int

    def to_record(self) -> dict[str, int]:
        return {"pid": self.pid, "start_time": self.start_time}

    @classmethod
    def from_record(cls, record: object) -> MainProcess | None:
        """Read a :meth:`to_record` mapping, or return None if it is not one."""
        if not isinstance(record, dict):
            return None
        pid, start_time = record.get("pid"), record.get("start_time")
        if type(pid) is not int or type(start_time) is not int or pid <= 0 or start_time < 0:
            return None
        return cls(pid=pid, start_time=start_time)


def python_exec_args(script: str, args: Sequence[str]) -> list[str]:
    """The argv that runs *script* with the sandbox's python, as the sandbox user, no login shell.

    For in-sandbox helpers that manage the process themselves, such as the
    setup and validate step runner (``agentic_ci.backends.openshell.steps``).
    """
    return [*_INTERNAL_EXEC, _NO_LOGIN_SHELL, "--", *_SANDBOX_PYTHON, script, *args]


def _exec_python(label, script, args, timeout=_PROCESS_EXEC_TIMEOUT_SECONDS):
    """Run *script* with the sandbox's python as the sandbox user, with no login shell.

    Logs *label* in place of the script text. Returns the CompletedProcess;
    raises ``subprocess.TimeoutExpired`` after *timeout* seconds.
    """
    prefix = [*_INTERNAL_EXEC, _NO_LOGIN_SHELL, "--"]
    log.detail("exec", " ".join([*prefix, *_SANDBOX_PYTHON, f"<{label}>", *args]))
    return subprocess.run(
        python_exec_args(script, args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def exec_python(label, script, args, timeout=_PROCESS_EXEC_TIMEOUT_SECONDS):
    """Run *script* with the sandbox image's ``/usr/bin/python3`` (see :func:`_exec_python`).

    For in-sandbox helpers outside this module, such as toolchain
    installation (``agentic_ci.backends.openshell.provision``).
    """
    return _exec_python(label, script, args, timeout=timeout)


def _process_script(args, what):
    """Run :data:`_PROCESS_SCRIPT` in the sandbox; return its exit status and stdout lines.

    Raises ``RuntimeError`` naming *what* and the failure class only when the
    exec times out; the output (which can hold process names) goes to the job
    log.
    """
    try:
        result = _exec_python("process scan", _PROCESS_SCRIPT, args)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Could not {what}: the in-sandbox process scan timed out ({type(exc).__name__})"
        ) from exc
    lines = (result.stdout or "").splitlines()
    for line in lines:
        log.detail("sandbox processes", line)
    _log_stderr(result)
    return result.returncode, lines


def _scan_failed(what, returncode):
    """The ``RuntimeError`` for a scan that exited with *returncode*: a class, no output."""
    reason = _SCAN_FAILURES.get(
        returncode, f"the in-sandbox process scan exited with status {returncode}"
    )
    return RuntimeError(f"Could not {what}: {reason}; see the job log")


def find_main_process() -> MainProcess:
    """Identify the sandbox's main process, right after :func:`create`.

    Call it before anything else runs in the sandbox: the main process is the
    only sandbox-user child of the supervisor, and it must be ``sleep
    infinity``. Waits up to :data:`_MAIN_WAIT_SECONDS` for it to appear.
    Raises ``RuntimeError`` when there is none, more than one, or it is
    something else.
    """
    what = "identify the sandbox's main process"
    deadline = time.monotonic() + _MAIN_WAIT_SECONDS
    while True:
        returncode, lines = _process_script(["main"], what)
        if returncode != _SCAN_NO_MAIN or time.monotonic() >= deadline:
            break
        time.sleep(_MAIN_POLL_SECONDS)
    if returncode != 0:
        raise _scan_failed(what, returncode)
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and parts[0] == "MAIN" and parts[1].isdigit() and parts[2].isdigit():
            main = MainProcess(pid=int(parts[1]), start_time=int(parts[2]))
            log.info(f"Sandbox main process: pid {main.pid}")
            return main
    raise RuntimeError(f"Could not {what}: unusable scan output; see the job log")


def stop_leftover_processes(main: MainProcess, phase: str) -> None:
    """Kill every sandbox-user process except *main* before *phase* opens.

    Processes started by earlier execs (the agent, earlier setup or validate
    steps, and whatever they started, daemons included) are still running
    after their exec ends. The setup shim is not a boundary against them:
    once the setup or validate phase opens, any of them could run it. So
    every one of them is killed first; the main process (matched by PID and
    start time), the supervisor (root) and the killing exec itself survive.

    Raises ``RuntimeError`` (a class and a count, never a process name or
    command line) when a process is still alive afterwards, or when *main* is
    not running: the record is then stale, nothing is killed, and the sandbox
    must be recreated. The caller must not run the phase. Details go to the
    job log.
    """
    what = f"stop the processes left in the sandbox before the {phase} phase"
    returncode, lines = _process_script(
        ["kill", str(main.pid), str(main.start_time), str(_KILL_DEADLINE_SECONDS)], what
    )
    if returncode == _SCAN_SURVIVORS:
        left = next((line.split()[1] for line in lines if line.startswith("LEFT ")), "?")
        raise RuntimeError(
            f"Could not {what}: {left if left.isdigit() else 'some'} process(es) survived; "
            "see the job log"
        )
    if returncode != 0:
        raise _scan_failed(what, returncode)
    if "LEFT 0" not in lines:
        raise RuntimeError(f"Could not {what}: unusable scan output; see the job log")
    killed = sum(1 for line in lines if line.startswith("KILLED "))
    log.info(f"Stopped {killed} leftover sandbox process(es) before the {phase} phase")


# How long a switch waits for the supervisor to pick up an attach or detach.
# OpenShell's supervisor polls the gateway every 10 s, so a running sandbox
# keeps injecting a detached provider's key for up to about that long.
# ``openshell sandbox provider attach|detach --wait`` returns once the
# supervisor has installed the change (credentials, policy and the
# environment of new processes), and the probe that follows then confirms
# it at once; the probe stays as a bounded, fail-closed check.
_PROVIDER_WAIT_SECONDS = 30
_PROVIDER_POLL_SECONDS = 2
# A probe exec gets the time left before the deadline, but at least this.
_PROVIDER_PROBE_MIN_TIMEOUT_SECONDS = 5
# The --wait bound handed to openshell, and how much longer the command may
# take before it counts as a gateway that does not answer.
_PROVIDER_COMMAND_WAIT_SECONDS = _PROVIDER_WAIT_SECONDS
_PROVIDER_COMMAND_TIMEOUT_SECONDS = _PROVIDER_COMMAND_WAIT_SECONDS + 15

# Prefix of the placeholder OpenShell puts in a provider credential's
# variable. Only the prefix is ever compared; the value is not printed.
_PLACEHOLDER_PREFIX = "openshell:resolve:env:"

# Prints ATTACHED when any of the variables named in its arguments holds a
# provider placeholder, else DETACHED, and nothing else.
_PROVIDER_ENV_SCRIPT = (
    "import os, sys; "
    "print('ATTACHED' if any(os.environ.get(name, '')"
    f".startswith({_PLACEHOLDER_PREFIX!r}) for name in sys.argv[1:]) else 'DETACHED')"
)


def _provider_command(action: str, phase: str) -> None:
    what = f"Could not {action} the credential provider for the {phase} phase"
    try:
        result = _run(
            [
                "openshell",
                "sandbox",
                "provider",
                action,
                "--wait",
                "--timeout",
                str(_PROVIDER_COMMAND_WAIT_SECONDS),
                SANDBOX_NAME,
                PROVIDER_NAME,
            ],
            capture_output=True,
            text=True,
            timeout=_PROVIDER_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{what}: openshell sandbox provider {action} timed out ({type(exc).__name__})"
        ) from exc
    if result.returncode != 0:
        _log_stderr(result)
        raise RuntimeError(
            f"{what}: openshell sandbox provider {action} exited with status "
            f"{result.returncode}; see the job log"
        )


def detach_provider(phase: str) -> None:
    """Detach the CI provider from the sandbox for a setup or validate *phase*.

    The rule OpenShell composes from a provider profile binds the API hosts
    to the agent binaries and is not part of ``policy get --base``, so
    parking the agent's rules cannot remove it: a step running an agent
    binary under the shim could still spend the credential. Detaching is the
    only lever. ``--wait`` returns once the supervisor has installed the
    change. Idempotent: detaching a detached provider succeeds at once.
    Follow it with :func:`wait_for_provider_env` before any step runs.
    """
    _provider_command("detach", phase)


def attach_provider(phase: str) -> None:
    """Attach the CI provider again, idempotently."""
    _provider_command("attach", phase)


def provider_env_state(
    env_vars: Sequence[str], timeout: float = _PROCESS_EXEC_TIMEOUT_SECONDS
) -> str | None:
    """Return ``ATTACHED`` or ``DETACHED`` as a fresh exec sees *env_vars*, or None.

    ``ATTACHED`` when any of *env_vars* holds a provider placeholder. None
    means the probe failed (a timeout, a refused exec, odd output) and says
    nothing about the provider.
    """
    names = _env_var_names(env_vars)
    try:
        result = _exec_python("provider probe", _PROVIDER_ENV_SCRIPT, names, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.detail("provider probe", "timed out")
        return None
    if result.returncode != 0:
        _log_stderr(result)
        return None
    state = (result.stdout or "").strip()
    return state if state in ("ATTACHED", "DETACHED") else None


def _env_var_names(env_vars: Sequence[str]) -> list[str]:
    """*env_vars* as a list, refusing a bare string (which would probe each character)."""
    if isinstance(env_vars, str) or not env_vars:
        raise ValueError("env_vars must be a non-empty sequence of variable names")
    return list(env_vars)


def wait_for_provider_env(env_vars: Sequence[str], *, attached: bool, phase: str) -> None:
    """Wait until the supervisor has applied an attach or detach of the provider.

    A new exec gets the provider's variables from the supervisor's current
    provider snapshot, which OpenShell replaces together with the credential
    bindings the proxy injects from. So a fresh exec in which one of
    *env_vars* holds a provider placeholder proves the credential is injected
    again, and one in which none does proves the proxy no longer resolves any
    placeholder for it, including one issued before the detach. It proves
    nothing about the network rule composed from the provider profile: the
    supervisor reloads the policy right after the environment, in the same
    poll, and a ``policy set --wait`` issued after the detach confirms it
    (see ``OpenShellBackend._set_egress_phase``). The probe needs no network.

    A DETACHED result only counts when the probe has been seen to report
    ATTACHED for this sandbox: call it with ``attached=False`` only after an
    ``attached=True`` wait or an ATTACHED :func:`provider_env_state`.

    Polls for up to :data:`_PROVIDER_WAIT_SECONDS` in total, probe execs
    included, and raises ``RuntimeError`` when the state cannot be confirmed,
    so the caller does not run the phase.
    """
    names = _env_var_names(env_vars)
    want = "ATTACHED" if attached else "DETACHED"
    deadline = time.monotonic() + _PROVIDER_WAIT_SECONDS
    while True:
        left = max(_PROVIDER_PROBE_MIN_TIMEOUT_SECONDS, deadline - time.monotonic())
        if provider_env_state(names, timeout=left) == want:
            log.info(f"Credential provider {want.lower()} for the {phase} phase")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(_PROVIDER_POLL_SECONDS)
    action = "resume" if attached else "stop"
    raise RuntimeError(
        f"Could not confirm that credential injection {action}s for the {phase} phase "
        f"within {_PROVIDER_WAIT_SECONDS}s; see the job log"
    )


def upload(local_path, timeout=None):
    """Upload a local path into the sandbox.

    With *timeout* (seconds), ``subprocess.TimeoutExpired`` is raised when the
    upload takes longer; without it the upload is not bounded.
    """
    kwargs = {} if timeout is None else {"timeout": timeout}
    _run(
        ["openshell", "sandbox", "upload", "--no-git-ignore", SANDBOX_NAME, local_path],
        check=True,
        **kwargs,
    )


def download(sandbox_path, local_dest):
    """Download a path from the sandbox to a local destination."""
    _run(
        ["openshell", "sandbox", "download", SANDBOX_NAME, sandbox_path, local_dest],
        check=True,
    )


def exec_cmd(cmd):
    """Run a command inside the sandbox. Returns the CompletedProcess."""
    return _run(
        ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd,
        check=True,
    )


def exec_cmd_streaming(cmd):
    """Run a command inside the sandbox with stdout piped. Returns a Popen."""
    args = ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd
    log.detail("exec", " ".join(args))
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def delete():
    """Delete the sandbox."""
    _run(
        ["openshell", "sandbox", "delete", SANDBOX_NAME],
        check=True,
    )
