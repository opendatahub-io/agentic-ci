"""Sandbox profiles: what a target repo needs inside the agent sandbox.

A sandbox profile describes the toolchains, egress presets, in-sandbox setup
and validation commands, declared skips, environment variables, resources and
download exclusions for one target repo. The same shape is used in two places:

- **Central** profiles come from reviewed CI configuration (for example
  autofix.json) and may set everything, including raw egress endpoints,
  ``resources`` and ``overlay``.
- **Overlay** profiles come from the target repo (the ``sandbox:`` section of
  ``.agentic-ci/config.yml`` on its base branch). A repo file must not be able
  to break the run or widen what central config allows, so fields an overlay
  may not set are dropped with a warning instead of failing the parse.

:func:`parse_profile` validates raw YAML/JSON data into an immutable
:class:`SandboxProfile`, :func:`merge_profiles` combines a central profile
with an overlay, and :func:`profile_to_dict` / :func:`profile_hash` serialize
one. ``SkillConfig.sandbox_profile`` carries the result to the backend.

In this release only ``resources`` takes effect (``OpenShellBackend`` sizes
the sandbox with it). The other fields are validated and carried but not yet
acted on.

Error and warning messages name the field path (for example
``validate[2].kind``) and the rule broken. They never include ``env`` values
or ``run`` strings, which can hold anything.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

KNOWN_TOOLCHAINS = frozenset(
    {
        "buf",
        "go",
        "golangci-lint",
        "helm",
        "kustomize",
        "node",
        "pnpm",
        "protoc",
        "python",
        "shfmt",
        "yq",
    }
)
"""Toolchain names a profile may request. A later release replaces this with a catalog."""

KNOWN_EGRESS_PRESETS = frozenset({"github-release-assets", "goproxy", "npm", "pypi"})
"""Egress preset names a profile may request. A later release attaches endpoint lists."""

DEFAULT_OVERLAY_ALLOWED_PRESETS = frozenset({"goproxy", "npm", "pypi"})
"""Egress presets a repo overlay may add unless the caller allows others."""

VALIDATE_KINDS = frozenset({"build", "generated", "lint", "test"})

DEFAULT_STEP_TIMEOUT = 600
MAX_STEP_TIMEOUT = 3600

Source = Literal["central", "overlay"]
OverlayMode = Literal["merge", "ignore"]

_TOP_LEVEL_KEYS = frozenset(
    {
        "discard_before_download",
        "egress",
        "env",
        "overlay",
        "resources",
        "setup",
        "skips",
        "toolchains",
        "validate",
    }
)
_SETUP_KEYS = frozenset({"name", "run", "timeout"})
_VALIDATE_KEYS = frozenset({"name", "kind", "run", "timeout"})
_SKIP_KEYS = frozenset({"match", "reason"})
_RESOURCE_KEYS = frozenset({"memory", "cpu", "gpu"})
_ACCESS_LEVELS = ("read-only", "read-write", "full")

# All patterns are used with ``fullmatch``: ``$`` would also accept a trailing newline.
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){0,2}")
_STEP_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# ``pat`` (personal access token) only counts as a whole ``_``-separated word,
# so GH_PAT is denied while GOPATH, PYTHONPATH and NODE_PATH are allowed.
_ENV_DENY_RE = re.compile(r"(?i)(token|secret|key|password|credential)|(?:^|_)pat(?:_|$)")
# Names that reach the harness, its telemetry and credentials, the dynamic
# loader, the shell running the env script, git, or TLS and proxy settings.
# Compared case-insensitively.
_ENV_DENY_PREFIXES = (
    "AGENT_",
    "ANTHROPIC_",
    "CLAUDE_",
    "CLOUD_ML_",
    "CODEX_",
    "GCP_",
    "GIT_",
    "GOOGLE_",
    "LD_",
    "OPENAI_",
    "OTEL_",
    "VERTEX_",
)
_ENV_DENY_NAMES = frozenset(
    {
        "ALL_PROXY",
        "BASH_ENV",
        "ENV",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "IFS",
        "NODE_EXTRA_CA_CERTS",
        "NODE_OPTIONS",
        "NO_PROXY",
        "PATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
)
_MEMORY_RE = re.compile(r"([0-9]+)(Ki|Mi|Gi|Ti)?")
_CPU_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?|[0-9]+m")
_WHITESPACE_RE = re.compile(r"\s")
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"(?:\*\.)?{_HOST_LABEL}(?:\.{_HOST_LABEL})*")
_PORT_RE = re.compile(r"[0-9]{1,5}")
# "tcp" is left out: OpenShell rejects it together with an access level, which is required.
_ENDPOINT_PROTOCOLS = ("", "rest", "sql", "websocket")
_ENDPOINT_ENFORCEMENTS = ("", "audit", "enforce")
_ENDPOINT_OPTIONS = frozenset(
    {
        "allow-uninspected-credentials",
        "request-body-credential-rewrite",
        "websocket-credential-rewrite",
    }
)
_ALLOWED_IP_RE = re.compile(r"allowed-ip=[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?:/[0-9]{1,2})?")
# A name shown as-is in a field path; anything else is quoted with ``_show``.
_PLAIN_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# Most entries one list or mapping may hold, so a large repo file cannot make
# parsing slow or a warning list huge.
MAX_ENTRIES = 1000
# Most warnings one parse or merge returns; the rest are summarized in one line.
MAX_WARNINGS = 50

# Longest untrusted name or key echoed in a message.
_MAX_SHOWN = 64


class FrozenMap(Mapping[str, str]):
    """Immutable, hashable ``str -> str`` mapping with keys kept in sorted order."""

    __slots__ = ("_data",)

    def __init__(self, items: Mapping[str, str] | Iterable[tuple[str, str]] = ()) -> None:
        pairs = items.items() if isinstance(items, Mapping) else items
        self._data: dict[str, str] = dict(sorted(pairs))

    def __getitem__(self, key: str) -> str:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return hash(tuple(self._data.items()))

    def __repr__(self) -> str:
        return f"FrozenMap({self._data!r})"


@dataclass(frozen=True)
class SetupStep:
    """A command run inside the sandbox before the agent starts."""

    name: str
    run: str
    timeout: int = DEFAULT_STEP_TIMEOUT


@dataclass(frozen=True)
class ValidateStep:
    """A validation command; ``kind`` is one of ``lint``, ``build``, ``test``, ``generated``."""

    name: str
    kind: str
    run: str
    timeout: int = DEFAULT_STEP_TIMEOUT


@dataclass(frozen=True)
class Skip:
    """A check the sandbox can never run, recorded instead of attempted."""

    match: str
    reason: str


@dataclass(frozen=True)
class Resources:
    """Sandbox size. ``None`` leaves that limit to OpenShell's default."""

    memory: str | None = None
    cpu: str | None = None
    gpu: int | None = None


@dataclass(frozen=True)
class SandboxProfile:
    """A validated, immutable sandbox profile.

    Build one with :func:`parse_profile`. Mappings passed to the constructor
    are frozen and sequences turned into tuples, so a profile cannot change
    after it is created. A ``Resources`` with no limit set becomes ``None``,
    so equivalent profiles compare and hash the same.
    """

    toolchains: Mapping[str, str] = field(default_factory=FrozenMap)
    egress: tuple[str, ...] = ()
    raw_egress: tuple[str, ...] = ()
    setup: tuple[SetupStep, ...] = ()
    validate: tuple[ValidateStep, ...] = ()
    skips: tuple[Skip, ...] = ()
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    resources: Resources | None = None
    discard_before_download: tuple[str, ...] = ()
    overlay: OverlayMode = "merge"

    def __post_init__(self) -> None:
        for name in ("toolchains", "env"):
            value = getattr(self, name)
            if not isinstance(value, FrozenMap):
                object.__setattr__(self, name, FrozenMap(value))
        for name in (
            "egress",
            "raw_egress",
            "setup",
            "validate",
            "skips",
            "discard_before_download",
        ):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)):
                raise TypeError(f"{name} must be a sequence of entries, not a string")
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))
        if self.resources == Resources():
            object.__setattr__(self, "resources", None)


@dataclass(frozen=True)
class ParsedProfile:
    """Result of :func:`parse_profile`: the profile and any non-fatal warnings."""

    profile: SandboxProfile
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergedProfile:
    """Result of :func:`merge_profiles`: the effective profile and merge warnings."""

    profile: SandboxProfile
    warnings: tuple[str, ...] = ()


class SandboxProfileError(ValueError):
    """A sandbox profile broke a rule. ``path`` names the field, e.g. ``validate[2].kind``."""

    def __init__(self, path: str, rule: str) -> None:
        super().__init__(f"{path}: {rule}")
        self.path = path
        self.rule = rule


def _show(value: object) -> str:
    """Quote an untrusted name for a message, truncated so it cannot flood a log.

    ``repr`` escapes newlines and control characters, so a name cannot forge
    another line in a log or bot comment.
    """
    text = str(value)
    if len(text) > _MAX_SHOWN:
        text = text[:_MAX_SHOWN] + "..."
    return repr(text)


def _key(value: object) -> str:
    """Show a name inside a field path: as-is when plain, else quoted with :func:`_show`."""
    if isinstance(value, str) and _PLAIN_NAME_RE.fullmatch(value):
        return value
    return _show(value)


def _cap_warnings(warnings: Sequence[str]) -> tuple[str, ...]:
    if len(warnings) <= MAX_WARNINGS:
        return tuple(warnings)
    extra = len(warnings) - MAX_WARNINGS
    return (*warnings[:MAX_WARNINGS], f"{extra} more warnings not shown")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class _Parser:
    def __init__(self, source: Source) -> None:
        if source not in ("central", "overlay"):
            raise ValueError(f"source must be 'central' or 'overlay', not {source!r}")
        self.source = source
        self.central = source == "central"
        self.warnings: list[str] = []

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")

    def soft(self, path: str, rule: str) -> None:
        """Fail a central profile, but only warn for an overlay (the entry is dropped)."""
        if self.central:
            raise SandboxProfileError(path, rule)
        self.warn(path, f"{rule}; ignored")

    def check_keys(self, data: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
        for key in data:
            if key not in allowed:
                where = f"{path}.{_key(key)}" if path else _key(key)
                self.soft(where, f"unknown key (allowed: {', '.join(sorted(allowed))})")

    def parse(self, data: object) -> SandboxProfile:
        if not isinstance(data, Mapping):
            raise SandboxProfileError("sandbox profile", "must be a mapping")
        if len(data) > MAX_ENTRIES:
            raise SandboxProfileError("sandbox profile", f"has more than {MAX_ENTRIES} keys")
        self.check_keys(data, _TOP_LEVEL_KEYS, "")
        egress, raw_egress = self.egress(data.get("egress"))
        return SandboxProfile(
            toolchains=self.toolchains(data.get("toolchains")),
            egress=egress,
            raw_egress=raw_egress,
            setup=tuple(self.setup_steps(data.get("setup"))),
            validate=tuple(self.validate_steps(data.get("validate"))),
            skips=self.skips(data.get("skips")),
            env=self.env(data.get("env")),
            resources=self.resources(data.get("resources")),
            discard_before_download=self.discard(data.get("discard_before_download")),
            overlay=self.overlay(data.get("overlay")),
        )

    @staticmethod
    def mapping(value: object, path: str) -> Mapping[Any, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise SandboxProfileError(path, "must be a mapping")
        if len(value) > MAX_ENTRIES:
            raise SandboxProfileError(path, f"must have at most {MAX_ENTRIES} entries")
        return value

    @staticmethod
    def sequence(value: object, path: str) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise SandboxProfileError(path, "must be a list")
        if len(value) > MAX_ENTRIES:
            raise SandboxProfileError(path, f"must have at most {MAX_ENTRIES} entries")
        return list(value)

    def toolchains(self, value: object) -> FrozenMap:
        result = {}
        for name, version in self.mapping(value, "toolchains").items():
            path = f"toolchains.{_key(name)}"
            if not isinstance(name, str) or name not in KNOWN_TOOLCHAINS:
                self.soft(path, _UNKNOWN_TOOLCHAIN)
                continue
            if _is_int(version):
                version = str(version)
            elif isinstance(version, float):
                raise SandboxProfileError(
                    path,
                    "version must be a quoted string; YAML reads an unquoted 1.20 as the "
                    "number 1.2, so the original text cannot be recovered",
                )
            if not _valid_version(version):
                raise SandboxProfileError(path, _BAD_VERSION)
            result[name] = version
        return FrozenMap(result)

    def egress(self, value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
        # dicts keep insertion order and give O(1) de-duplication.
        presets: dict[str, None] = {}
        raw: dict[str, None] = {}
        for index, entry in enumerate(self.sequence(value, "egress")):
            path = f"egress[{index}]"
            if not isinstance(entry, str) or not entry:
                raise SandboxProfileError(path, "must be a non-empty string")
            if ":" in entry:
                if not self.central:
                    self.warn(
                        path,
                        "raw endpoints are accepted only from the central profile; dropped",
                    )
                    continue
                _check_raw_endpoint(entry, path)
                raw[entry] = None
                continue
            if entry not in KNOWN_EGRESS_PRESETS:
                self.soft(
                    path,
                    f"unknown egress preset {_show(entry)} "
                    f"(known: {', '.join(sorted(KNOWN_EGRESS_PRESETS))})",
                )
                continue
            presets[entry] = None
        return tuple(presets), tuple(raw)

    def _step_fields(
        self, entry: object, path: str, allowed: frozenset[str], seen: set[str]
    ) -> tuple[str, str, int] | None:
        """Validate a step's common fields; ``None`` means an overlay duplicate was dropped."""
        if not isinstance(entry, Mapping):
            raise SandboxProfileError(path, "must be a mapping")
        self.check_keys(entry, allowed, path)
        name = entry.get("name")
        if not isinstance(name, str) or not _STEP_NAME_RE.fullmatch(name):
            raise SandboxProfileError(
                f"{path}.name", "must be a non-empty string of letters, digits, '.', '_' or '-'"
            )
        run = entry.get("run")
        if not isinstance(run, str) or not run.strip():
            raise SandboxProfileError(f"{path}.run", "must be a non-empty string")
        timeout = entry.get("timeout", DEFAULT_STEP_TIMEOUT)
        if not _is_int(timeout) or not 1 <= timeout <= MAX_STEP_TIMEOUT:
            raise SandboxProfileError(
                f"{path}.timeout", f"must be an integer from 1 to {MAX_STEP_TIMEOUT} seconds"
            )
        if name in seen:
            self.soft(f"{path}.name", f"duplicate step name {_show(name)}")
            return None
        seen.add(name)
        return name, run, timeout

    def setup_steps(self, value: object) -> Iterator[SetupStep]:
        seen: set[str] = set()
        for index, entry in enumerate(self.sequence(value, "setup")):
            fields = self._step_fields(entry, f"setup[{index}]", _SETUP_KEYS, seen)
            if fields is not None:
                name, run, timeout = fields
                yield SetupStep(name=name, run=run, timeout=timeout)

    def validate_steps(self, value: object) -> Iterator[ValidateStep]:
        seen: set[str] = set()
        for index, entry in enumerate(self.sequence(value, "validate")):
            path = f"validate[{index}]"
            fields = self._step_fields(entry, path, _VALIDATE_KEYS, seen)
            kind = entry.get("kind")
            if not isinstance(kind, str) or kind not in VALIDATE_KINDS:
                raise SandboxProfileError(
                    f"{path}.kind", f"must be one of {', '.join(sorted(VALIDATE_KINDS))}"
                )
            if fields is not None:
                name, run, timeout = fields
                yield ValidateStep(name=name, kind=kind, run=run, timeout=timeout)

    def skips(self, value: object) -> tuple[Skip, ...]:
        result: dict[Skip, None] = {}
        for index, entry in enumerate(self.sequence(value, "skips")):
            path = f"skips[{index}]"
            if not isinstance(entry, Mapping):
                raise SandboxProfileError(path, "must be a mapping")
            self.check_keys(entry, _SKIP_KEYS, path)
            for key in ("match", "reason"):
                text = entry.get(key)
                if not isinstance(text, str) or not text.strip():
                    raise SandboxProfileError(f"{path}.{key}", "must be a non-empty string")
            result[Skip(match=entry["match"], reason=entry["reason"])] = None
        return tuple(result)

    def env(self, value: object) -> FrozenMap:
        result = {}
        for name, env_value in self.mapping(value, "env").items():
            path = f"env.{_key(name)}"
            reason = _env_name_problem(name)
            if reason is not None:
                self.soft(path, reason)
                continue
            if _is_int(env_value):
                env_value = str(env_value)
            if not isinstance(env_value, str):
                raise SandboxProfileError(
                    path, "value must be a string or an integer (quote booleans and decimals)"
                )
            result[name] = env_value
        return FrozenMap(result)

    def resources(self, value: object) -> Resources | None:
        if value is None:
            return None
        if not self.central:
            self.warn(
                "resources",
                "set only by the central profile (runner capacity is a central cost); ignored",
            )
            return None
        data = self.mapping(value, "resources")
        self.check_keys(data, _RESOURCE_KEYS, "resources")
        memory = data.get("memory")
        if memory is not None:
            match = _MEMORY_RE.fullmatch(memory) if isinstance(memory, str) else None
            if match is None or int(match.group(1)) == 0:
                raise SandboxProfileError(
                    "resources.memory",
                    "must be a positive quantity string such as '512Mi' or '8Gi' "
                    "(units: Ki, Mi, Gi, Ti)",
                )
        cpu = data.get("cpu")
        if cpu is not None:
            cpu = _check_cpu(cpu)
        gpu = data.get("gpu")
        if gpu is not None and (not _is_int(gpu) or gpu < 0):
            raise SandboxProfileError("resources.gpu", "must be a non-negative integer")
        if memory is None and cpu is None and gpu is None:
            return None
        return Resources(memory=memory, cpu=cpu, gpu=gpu)

    def discard(self, value: object) -> tuple[str, ...]:
        result: dict[str, None] = {}
        for index, entry in enumerate(self.sequence(value, "discard_before_download")):
            path = f"discard_before_download[{index}]"
            result[_normalize_discard_path(entry, path)] = None
        return tuple(result)

    def overlay(self, value: object) -> OverlayMode:
        if value is None:
            return "merge"
        if not self.central:
            self.warn("overlay", "set only by the central profile; ignored")
            return "merge"
        if value == "merge":
            return "merge"
        if value == "ignore":
            return "ignore"
        raise SandboxProfileError("overlay", "must be 'merge' or 'ignore'")


_UNKNOWN_TOOLCHAIN = f"unknown toolchain (known: {', '.join(sorted(KNOWN_TOOLCHAINS))})"
_BAD_VERSION = "version must be 'auto' or MAJOR[.MINOR[.PATCH]] digits"


def _valid_version(version: object) -> bool:
    return isinstance(version, str) and (
        version == "auto" or _VERSION_RE.fullmatch(version) is not None
    )


def _env_name_problem(name: object) -> str | None:
    """Return why *name* may not be set in a profile's ``env``, or ``None`` if it may."""
    if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
        return "not a valid environment variable name"
    upper = name.upper()
    if upper in _ENV_DENY_NAMES:
        return "name is reserved"
    for prefix in _ENV_DENY_PREFIXES:
        if upper.startswith(prefix):
            return f"names starting with {prefix} are reserved"
    if _ENV_DENY_RE.search(name):
        return (
            "name looks like a credential "
            "(token, secret, key, password, credential, or the word pat)"
        )
    return None


def _check_raw_endpoint(entry: str, path: str) -> None:
    """Check the ``openshell policy update --add-endpoint`` format.

    The optional trailing fields may be empty, as in the built-in
    ``api.openai.com:443:read-write:::allow-uninspected-credentials``. The
    host must be an ASCII hostname or IPv4 address, optionally with a leading
    ``*.`` wildcard, so it can never be read as a command-line option.
    """
    rule = (
        "raw endpoint must be host:port:access[:protocol[:enforcement[:options]]] with an "
        f"ASCII host name, port 1-65535, access one of {', '.join(_ACCESS_LEVELS)}, "
        f"protocol one of {', '.join(p for p in _ENDPOINT_PROTOCOLS if p)}, "
        f"enforcement one of {', '.join(e for e in _ENDPOINT_ENFORCEMENTS if e)} "
        "(only with a protocol), "
        f"options from {', '.join(sorted(_ENDPOINT_OPTIONS))}, allowed-ip=IPV4[/BITS], "
        "and no whitespace"
    )
    if _WHITESPACE_RE.search(entry):
        raise SandboxProfileError(path, rule)
    parts = entry.split(":")
    if not 3 <= len(parts) <= 6 or (len(parts) == 6 and not parts[5]):
        raise SandboxProfileError(path, rule)
    parts += [""] * (6 - len(parts))
    host, port, access, protocol, enforcement, options = parts
    if len(host) > 253 or not _HOST_RE.fullmatch(host):
        raise SandboxProfileError(path, rule)
    if not _PORT_RE.fullmatch(port) or not 1 <= int(port) <= 65535:
        raise SandboxProfileError(path, rule)
    if access not in _ACCESS_LEVELS:
        raise SandboxProfileError(path, rule)
    if protocol not in _ENDPOINT_PROTOCOLS or enforcement not in _ENDPOINT_ENFORCEMENTS:
        raise SandboxProfileError(path, rule)
    if enforcement and not protocol:
        raise SandboxProfileError(path, rule)
    if options and not all(
        option in _ENDPOINT_OPTIONS or _ALLOWED_IP_RE.fullmatch(option)
        for option in options.split(",")
    ):
        raise SandboxProfileError(path, rule)


def _check_cpu(value: object) -> str:
    rule = "must be a positive CPU quantity such as '4', '2.5' or '500m'"
    if _is_int(value) or isinstance(value, float):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise SandboxProfileError("resources.cpu", rule)
    if not _CPU_RE.fullmatch(text) or float(text.removesuffix("m")) <= 0:
        raise SandboxProfileError("resources.cpu", rule)
    return text


def _normalize_discard_path(entry: object, path: str) -> str:
    if not isinstance(entry, str) or not entry or "\0" in entry:
        raise SandboxProfileError(path, "must be a non-empty string")
    if entry.startswith("/"):
        raise SandboxProfileError(path, "must be relative to the workdir, not absolute")
    if ".." in entry.split("/"):
        raise SandboxProfileError(path, "must not contain a '..' component")
    normalized = posixpath.normpath(entry)
    if normalized == ".":
        raise SandboxProfileError(path, "must not name the workdir itself")
    if ".git" in normalized.split("/"):
        raise SandboxProfileError(path, "must not name .git or a path under it")
    return normalized


def parse_profile(data: object, *, source: Source) -> ParsedProfile:
    """Validate raw profile data (the YAML/JSON shape) into a :class:`SandboxProfile`.

    Args:
        data: The profile mapping, as loaded from JSON or YAML. A missing
            (``None``) field means its default.
        source: ``"central"`` for reviewed CI configuration, ``"overlay"``
            for a target repo's own file.

    Returns:
        :class:`ParsedProfile` with the profile and a tuple of warnings (at
        most :data:`MAX_WARNINGS`, then one summary line). A central profile
        yields no warnings: anything wrong raises.

    Raises:
        SandboxProfileError: The data breaks a rule. For an overlay, unknown
            keys, unknown toolchains and egress presets, rejected ``env``
            names, duplicate step names, raw egress endpoints, ``resources``
            and ``overlay`` are dropped with a warning instead.

    Rules worth knowing when writing a profile:

    - Quote toolchain versions (``go: "1.20"``). Integers are accepted and
      converted, but an unquoted decimal is rejected because YAML has
      already turned ``1.20`` into ``1.2``.
    - ``env`` values must be strings. Integers are converted with ``str()``;
      booleans, decimals, null, lists and mappings are rejected.
    - ``egress`` mixes preset names and raw ``host:port:access`` endpoints;
      an entry containing ``:`` is a raw endpoint.
    - Each list or mapping holds at most :data:`MAX_ENTRIES` entries.
    """
    parser = _Parser(source)
    profile = parser.parse(data)
    return ParsedProfile(profile=profile, warnings=_cap_warnings(parser.warnings))


_StepT = TypeVar("_StepT", SetupStep, ValidateStep)
_T = TypeVar("_T")


def _merge_steps(
    central: tuple[_StepT, ...], overlay: tuple[_StepT, ...], field_name: str, warnings: list[str]
) -> tuple[_StepT, ...]:
    names = {step.name for step in central}
    merged = list(central)
    for step in overlay:
        if step.name in names:
            warnings.append(
                f"{field_name}.{_key(step.name)}: the central profile defines a step with this "
                "name; overlay step dropped"
            )
            continue
        names.add(step.name)
        merged.append(step)
    return tuple(merged)


def _merge_unique(central: tuple[_T, ...], overlay: Iterable[_T]) -> tuple[_T, ...]:
    # dict.fromkeys keeps the first occurrence and de-duplicates in linear time.
    return tuple(dict.fromkeys((*central, *overlay)))


def _skip_overlaps(match: str, step: ValidateStep) -> bool:
    """Whether a skip ``match`` could be read as covering *step*.

    The agent matches skips loosely, so compare casefolded text in both
    directions: the match inside the step name or command, or the step name or
    command inside the match.
    """
    needle = match.casefold()
    for text in (step.name, step.run):
        folded = text.casefold()
        if folded and (needle in folded or folded in needle):
            return True
    return False


def _merge_skips(
    central: SandboxProfile, overlay: tuple[Skip, ...], warnings: list[str]
) -> tuple[Skip, ...]:
    """Add overlay skips that neither redefine a central skip nor hide central validation."""
    central_skips = {skip.match: skip for skip in central.skips}
    kept: list[Skip] = []
    for skip in overlay:
        path = f"skips.{_key(skip.match)}"
        existing = central_skips.get(skip.match)
        if existing is not None:
            if existing != skip:
                warnings.append(
                    f"{path}: the central profile defines this match; overlay skip dropped"
                )
            continue
        if any(_skip_overlaps(skip.match, step) for step in central.validate):
            warnings.append(
                f"{path}: matches a validate step the central profile requires; "
                "overlay skip dropped"
            )
            continue
        kept.append(skip)
    return _merge_unique(central.skips, kept)


def _merge_map(
    central: Mapping[str, str], overlay: Mapping[str, str], field_name: str, warnings: list[str]
) -> FrozenMap:
    merged = dict(overlay)
    for key, value in central.items():
        if key in overlay and overlay[key] != value:
            warnings.append(f"{field_name}.{_key(key)}: the central profile's value wins")
        merged[key] = value
    return FrozenMap(merged)


def _overlay_toolchains(overlay: SandboxProfile, warnings: list[str]) -> dict[str, str]:
    """Keep the overlay toolchains ``parse_profile`` would accept (for directly built overlays)."""
    kept = {}
    for name, version in overlay.toolchains.items():
        path = f"toolchains.{_key(name)}"
        if name not in KNOWN_TOOLCHAINS:
            warnings.append(f"{path}: {_UNKNOWN_TOOLCHAIN}; ignored")
        elif not _valid_version(version):
            warnings.append(f"{path}: {_BAD_VERSION}; ignored")
        else:
            kept[name] = version
    return kept


def _overlay_env(overlay: SandboxProfile, warnings: list[str]) -> dict[str, str]:
    """Keep the overlay env names ``parse_profile`` would accept (for directly built overlays)."""
    kept = {}
    for name, value in overlay.env.items():
        reason = _env_name_problem(name)
        if reason is not None:
            warnings.append(f"env.{_key(name)}: {reason}; ignored")
        else:
            kept[name] = value
    return kept


def merge_profiles(
    central: SandboxProfile | None,
    overlay: SandboxProfile | None,
    *,
    overlay_allowed_presets: AbstractSet[str] = DEFAULT_OVERLAY_ALLOWED_PRESETS,
) -> MergedProfile | None:
    """Combine a central profile with a repo overlay.

    Central scalars and map keys win (``toolchains``, ``env``, ``resources``,
    ``overlay``). Lists (``setup``, ``validate``, ``skips``, ``egress``,
    ``discard_before_download``) are concatenated central first and
    de-duplicated; an overlay step whose name the central list already uses
    is dropped. An overlay skip is dropped when the central profile has a
    skip with the same ``match``, or when its ``match`` and the name or ``run``
    of a central ``validate`` step contain one another (ignoring case), so a
    repo cannot skip validation that central configuration requires. Overlay egress presets outside
    *overlay_allowed_presets*, overlay raw endpoints and overlay
    ``resources`` are dropped. A central ``overlay: ignore`` drops the
    overlay entirely.

    The overlay is held to the same envelope even when it was built directly
    instead of by :func:`parse_profile`: unknown toolchains, bad toolchain
    versions and rejected ``env`` names are dropped with a warning.

    An overlay without a central profile is merged into a default envelope:
    ``overlay: merge``, no resources and no raw egress.

    Returns:
        ``None`` when both are ``None``, otherwise :class:`MergedProfile`
        with the effective profile and any warnings (at most
        :data:`MAX_WARNINGS`, then one summary line). Never raises for an
        overlay that ``parse_profile(..., source="overlay")`` accepted.
    """
    if central is None and overlay is None:
        return None
    base = central if central is not None else SandboxProfile()
    if overlay is None:
        return MergedProfile(profile=base)
    warnings: list[str] = []
    if base.overlay == "ignore":
        warnings.append("overlay: the central profile sets overlay: ignore; repo overlay dropped")
        return MergedProfile(profile=base, warnings=tuple(warnings))

    presets = []
    for preset in overlay.egress:
        if preset not in overlay_allowed_presets:
            warnings.append(
                f"egress: preset {_show(preset)} is not allowed in a repo overlay; dropped"
            )
            continue
        presets.append(preset)
    if overlay.raw_egress:
        warnings.append(
            "egress: raw endpoints are accepted only from the central profile; "
            f"{len(overlay.raw_egress)} dropped"
        )
    if overlay.resources is not None:
        warnings.append("resources: set only by the central profile; overlay value ignored")

    toolchains = _overlay_toolchains(overlay, warnings)
    env = _overlay_env(overlay, warnings)
    profile = SandboxProfile(
        toolchains=_merge_map(base.toolchains, toolchains, "toolchains", warnings),
        egress=_merge_unique(base.egress, presets),
        raw_egress=base.raw_egress,
        setup=_merge_steps(base.setup, overlay.setup, "setup", warnings),
        validate=_merge_steps(base.validate, overlay.validate, "validate", warnings),
        skips=_merge_skips(base, overlay.skips, warnings),
        env=_merge_map(base.env, env, "env", warnings),
        resources=base.resources,
        discard_before_download=_merge_unique(
            base.discard_before_download, overlay.discard_before_download
        ),
        overlay=base.overlay,
    )
    return MergedProfile(profile=profile, warnings=_cap_warnings(warnings))


def profile_to_dict(profile: SandboxProfile) -> dict[str, Any]:
    """Serialize *profile* to the shape :func:`parse_profile` accepts.

    Preset names come before raw endpoints in ``egress``. ``resources`` is
    omitted when unset, as are its unset members.
    """
    data: dict[str, Any] = {
        "toolchains": dict(profile.toolchains),
        "egress": [*profile.egress, *profile.raw_egress],
        "setup": [{"name": s.name, "run": s.run, "timeout": s.timeout} for s in profile.setup],
        "validate": [
            {"name": v.name, "kind": v.kind, "run": v.run, "timeout": v.timeout}
            for v in profile.validate
        ],
        "skips": [{"match": s.match, "reason": s.reason} for s in profile.skips],
        "env": dict(profile.env),
        "discard_before_download": list(profile.discard_before_download),
        "overlay": profile.overlay,
    }
    if profile.resources is not None:
        resources = {
            key: getattr(profile.resources, key)
            for key in ("memory", "cpu", "gpu")
            if getattr(profile.resources, key) is not None
        }
        data["resources"] = resources
    return data


def profile_hash(profile: SandboxProfile) -> str:
    """Return the sha256 hex digest of *profile*'s canonical JSON form."""
    canonical = json.dumps(profile_to_dict(profile), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
