"""OpenShell sandbox backend for agentic-ci."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_ci import log, toolchains
from agentic_ci.backend import Backend
from agentic_ci.backends.openshell import environment, gateway, provider, sandbox, steps
from agentic_ci.backends.openshell.policy import phase_endpoints
from agentic_ci.backends.openshell.provision import OpenShellInstaller
from agentic_ci.harness import AGENT_EFFORT_ENV_VAR
from agentic_ci.redact import secret_values
from agentic_ci.sandbox_profile import profile_hash

if TYPE_CHECKING:
    from agentic_ci.harness import Harness
    from agentic_ci.sandbox_profile import SandboxProfile

# GCP access tokens minted by the OpenShell gateway live for 3600s. The
# gateway's refresh worker is supposed to rotate them ahead of expiry, but
# around the hourly boundary a transient mint failure (retried only every 60s)
# can let the token lapse, producing a burst of 401s that exhausts the agent's
# retry budget and kills the run mid-way (see NVIDIA/OpenShell PR #1763).
#
# Force a rotation well inside the token lifetime so a freshly minted token is
# always present, tolerating a couple of failed rotations without draining the
# token's remaining life.
_TOKEN_KEEPALIVE_INTERVAL = 1200  # rotate every 20 min

# Phase-offset the first rotation by 10 min so the 20-min cadence lands at
# 10/30/50/70/... min, never coinciding with the ~hourly expiry boundary that
# the gateway refresh worker and the agent's client token cache already act on.
# Rotating on top of that natural re-fetch correlated with extra transient
# errors; offsetting avoids the collision.
_TOKEN_KEEPALIVE_OFFSET = 600  # 10 min


def _token_keepalive(stop: threading.Event) -> None:
    """Force-rotate the gateway's GCP access token on a phase-offset 20-min
    cadence until *stop* is set. Failures are logged but never raised."""
    if stop.wait(_TOKEN_KEEPALIVE_OFFSET):
        return
    while True:
        try:
            provider.rotate_token()
        except subprocess.CalledProcessError as exc:
            print(
                f"  [token-keepalive] rotate failed (rc={exc.returncode}): "
                f"{exc.stderr.strip() if exc.stderr else ''}",
                flush=True,
            )
        if stop.wait(_TOKEN_KEEPALIVE_INTERVAL):
            return


# Claude Code's API retry budget (the "Retry N/10" counter in the stream).
# The default is 10, but a Vertex token-rotation lapse can produce a burst of
# retryable "unknown" errors that, on stock 60-min token intervals, exhausted
# all 10 retries and killed the run. The 20-min token keepalive shortens those
# windows; this widens the budget so even an unlucky long lapse recovers.
# Belt-and-suspenders with the keepalive above. Overridable via env var.
_DEFAULT_MAX_RETRIES = "20"

_OPENSHELL_HOST = "host.openshell.internal"
_OPENAI_CREDENTIAL_ENV_VARS = frozenset({"OPENAI_API_KEY"})
_OPENSHELL_STATE_ENV = "AGENTIC_CI_OPENSHELL_STATE"
_DEFAULT_OPENSHELL_STATE = Path.home() / ".config" / "agentic-ci" / "openshell-sandbox.json"


def _openshell_state_path() -> Path:
    return Path(os.environ.get(_OPENSHELL_STATE_ENV, _DEFAULT_OPENSHELL_STATE))


def _load_sandbox_identity() -> dict | None:
    try:
        data = json.loads(_openshell_state_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_sandbox_identity(identity: dict) -> None:
    state_path = _openshell_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")


def _clear_sandbox_identity() -> None:
    try:
        _openshell_state_path().unlink()
    except FileNotFoundError:
        pass


def _sandbox_identity(
    harness_name: str,
    image: str | None,
    auth_mode: str,
    credential: str | None = None,
    profile: SandboxProfile | None = None,
) -> dict:
    """Describe a sandbox so ``setup()`` can tell whether an existing one fits.

    The key fingerprint and the profile hash are recorded only when set, so
    without them the identity (and the file it is saved to) is unchanged. A
    rotated key or a changed profile recreates the sandbox instead of reusing
    a stale placeholder or old egress.
    """
    identity = {"auth_mode": auth_mode, "harness": harness_name, "image": image}
    if credential is not None:
        # Fingerprint of a key the agent gets only through the provider. A
        # rotated key must recreate the sandbox: updating the provider alone
        # reaches a running sandbox only after a delay, and a placeholder
        # issued before the update keeps resolving to the old key.
        identity["credential"] = credential
    if profile is not None:
        identity["profile_hash"] = profile_hash(profile)
    return identity


# Key of the sandbox's main process (sandbox.MainProcess.to_record()) in the
# saved identity. Recorded only for a profile sandbox, which is the only kind
# whose phase switches kill leftover processes. It describes the sandbox
# rather than what it was asked for, so it is left out of identity matching.
_MAIN_PROCESS_KEY = "main_process"

# Key of the host's record of the toolchains provisioned in the sandbox
# (target directory to archive sha256) in the saved identity. Recorded only
# for a profile with toolchains. A reused sandbox skips a toolchain only when
# this record and the in-sandbox marker agree (see provision.py): the marker
# alone lives in a directory the agent can write. Like the main process, it
# describes the sandbox and is left out of identity matching.
_TOOLCHAINS_KEY = "toolchains"

# Key of the setup step records (StepRecord.to_dict()) in the saved identity.
# Recorded only for a profile with setup steps, which run when the sandbox is
# created; a reused sandbox reports them again (in ENVIRONMENT.md and
# _run/sandbox-setup.json) instead of running the steps a second time.
_SETUP_KEY = "setup"

_DESCRIPTIVE_KEYS = frozenset({_MAIN_PROCESS_KEY, _TOOLCHAINS_KEY, _SETUP_KEY})


def _identity_fields(identity: dict) -> dict:
    """*identity* without what it records about the sandbox, for matching."""
    return {k: v for k, v in identity.items() if k not in _DESCRIPTIVE_KEYS}


def _saved_setup_records(identity: dict | None) -> tuple[steps.StepRecord, ...]:
    """The setup step records of the saved identity; entries that do not parse are dropped."""
    records = (identity or {}).get(_SETUP_KEY)
    if not isinstance(records, list):
        return ()
    parsed = (steps.StepRecord.from_dict(record) for record in records)
    return tuple(record for record in parsed if record is not None)


# Run records the host writes into the run directory. run() writes them again
# after the workdir download, which brings the sandbox's copy of _run back.
_SETUP_RECORD = "sandbox-setup.json"
_VALIDATION_RECORD = "sandbox-validation.json"
_DISCARD_RECORD = "sandbox-discard.json"
_TOOLCHAIN_RECORD = "toolchains.json"
_RUN_RECORDS = (_SETUP_RECORD, _VALIDATION_RECORD, _DISCARD_RECORD, _TOOLCHAIN_RECORD)

# The variable an env script line exports.
_EXPORT_RE = re.compile(r"\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=")


def _json_writer(records: tuple) -> Callable[[Path], None]:
    """A writer of *records* (each with ``to_dict()``) as a JSON list, for a run record."""
    data = [record.to_dict() for record in records]
    return lambda path: steps.write_json(path, data)


def _toolchain_records(identity: dict | None) -> dict[str, str]:
    """The valid entries of the saved toolchain record (target to sha256)."""
    records = (identity or {}).get(_TOOLCHAINS_KEY)
    if not isinstance(records, dict):
        return {}
    prefix = toolchains.SANDBOX_TOOLCHAIN_ROOT + "/"
    return {
        target: sha256
        for target, sha256 in records.items()
        if isinstance(target, str)
        and target.startswith(prefix)
        and isinstance(sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", sha256)
    }


class OpenShellBackend(Backend):
    """Runs an AI agent inside an OpenShell sandbox.

    OpenShell provides security-focused sandboxing with network policy
    enforcement, filesystem isolation, and Landlock-based access control.
    Authentication is handled through an OpenShell provider created from
    one of agentic-ci's provider profiles (Vertex AI, Anthropic or OpenAI;
    none for a Claude subscription OAuth token). The gateway holds the real
    credential and injects a placeholder into the sandbox; the supervisor
    proxy swaps it for the real value on requests to the profile's
    endpoints from the agent binaries.

    Unlike PodmanBackend, which bind-mounts the workdir so changes are
    visible immediately on the host, OpenShellBackend copies the workdir
    into the sandbox on setup() and copies it back after run() completes.
    Only changes inside the workdir are reflected back to the host; files
    written elsewhere in the sandbox (e.g. /tmp) are not retrieved. The
    host's git control files (``.git/config``, hooks, ``info/``) are restored
    after the download, so the agent's git config never runs on the host.

    ``sandbox_profile`` (see :mod:`agentic_ci.sandbox_profile`) is stored on
    the backend. Its ``resources`` size the sandbox wherever the caller did
    not pass ``memory``, ``cpu`` or ``gpu`` explicitly, and its ``egress``
    presets open for the agent when the sandbox is created (the repo's
    ``.agentic-ci/openshell-policy.yml`` is then ignored).
    Its ``toolchains`` are provisioned after the workdir upload (see
    :meth:`_provision_toolchains`): the host downloads and verifies each
    archive and the sandbox extracts it under ``/sandbox/.local/toolchains``;
    :attr:`toolchain_env` then holds the variables the env script exports.
    :meth:`_set_egress_phase` switches the egress of the setup shim between
    the ``setup``, ``validate`` and ``agent`` phases. The shim is an extra
    layer, not isolation (see :data:`sandbox.SANDBOX_SETUP_SHIM`), so each
    switch also kills the processes earlier execs left running and, for the
    setup and validate phases, detaches the credential provider.

    Its ``setup`` steps run in the sandbox under the setup phase when the
    sandbox is created (:meth:`_run_setup_phase`), and
    ``/sandbox/.agentic-ci/ENVIRONMENT.md`` tells the agent what it has
    (:meth:`_write_environment`). After the agent, :meth:`run` deletes the
    harness credential files, runs the ``validate`` commands under the
    validate phase, moves the ``discard_before_download`` paths out of the
    workdir (the next run moves them back) and only then downloads the
    workdir. The host writes the results to
    ``<run_dir>/sandbox-setup.json``, ``sandbox-validation.json`` and
    ``sandbox-discard.json`` and writes them again after the download, so
    the agent cannot forge them.
    """

    collector_bind_address = "0.0.0.0"
    _ENV_SCRIPT = "/tmp/.agentic-ci-env.sh"

    def __init__(
        self,
        workdir=".",
        image=None,
        policy=None,
        extra_env=None,
        approval_mode=None,
        memory=None,
        cpu=None,
        gpu=None,
        *,
        harness: Harness,
        sandbox_profile: SandboxProfile | None = None,
        allow_host_setup: bool = False,
    ):
        super().__init__(
            workdir=workdir, image=image, harness=harness, allow_host_setup=allow_host_setup
        )
        self.policy_path = policy
        self._extra_env = extra_env or {}
        self.approval_mode = approval_mode
        self.memory = memory
        self.cpu = cpu
        self.gpu = gpu
        self.sandbox_profile = sandbox_profile
        # Set by _provision_toolchains(); empty until then and without toolchains.
        self.toolchain_env = toolchains.ToolchainEnv()
        self.toolchain_results: tuple[toolchains.ToolchainResult, ...] = ()
        self._toolchain_records: dict[str, str] = {}
        # Set by setup() for a profile with setup steps (run now, or saved
        # when a reused sandbox was created), and by each run() for a profile
        # with validate commands or skips and discard paths; run() starts
        # with none, so a run never reports an earlier run's results.
        self.setup_records: tuple[steps.StepRecord, ...] = ()
        self.validation_records: tuple[steps.StepRecord, ...] = ()
        self.discard_records: tuple[steps.DiscardRecord, ...] = ()
        # Whether validation ran in the current run() (not for a classifier
        # run); only then is sandbox-validation.json written.
        self._validation_ran = False
        # Set when the sandbox left the agent phase and has not been switched
        # back successfully; run() switches it before anything else then.
        self._agent_phase_pending = False
        # Loaded from the saved identity on first use when this backend did
        # not create the sandbox itself.
        self._main_process: sandbox.MainProcess | None = None
        self._explicit_resources = {
            name: value for name, value in (("memory", memory), ("cpu", cpu), ("gpu", gpu)) if value
        }
        self._apply_profile_resources()

    def _apply_profile_resources(self):
        """Fill ``memory``, ``cpu`` and ``gpu`` from the sandbox profile.

        Explicit constructor values win. Logs once where each value came from.
        A value is "set" by the same truthiness test ``sandbox.create`` uses,
        so an explicit ``""`` or ``0`` does not hide the profile's value and
        the log names only limits that are actually applied.
        """
        profile = self.sandbox_profile
        if profile is None or profile.resources is None:
            return
        sources = []
        for name in ("memory", "cpu", "gpu"):
            explicit = getattr(self, name)
            from_profile = getattr(profile.resources, name)
            if explicit:
                sources.append(f"{name}={explicit} (explicit)")
            elif from_profile:
                setattr(self, name, from_profile)
                sources.append(f"{name}={from_profile} (sandbox profile)")
        if sources:
            log.info(f"Sandbox resources: {', '.join(sources)}")

    def _merged_env(self):
        return {**os.environ, **self._extra_env}

    def setup(self, otel_port=None):
        env = self._merged_env()
        auth_mode = self.harness.auth_mode_for_env(env)
        provider.validate_credentials(auth_mode, env)
        credential = provider.credential_fingerprint(auth_mode, env)

        if gateway.is_running() and not gateway.config_is_current():
            # The gateway reads gateway.toml only at startup, so a running
            # gateway would keep creating sandboxes with the old supervisor
            # and sandbox runtime images. Tear down the sandbox and gateway
            # so the new config takes effect.
            log.section("OpenShell gateway config changed; restarting gateway")
            self.stop()

        if not gateway.is_running():
            log.section("Starting OpenShell gateway")
            gateway.start()
        else:
            log.section("OpenShell gateway already running")

        sandbox_exists = sandbox.exists()
        existing_provider = provider.provider_exists()
        existing_auth_mode = None
        if existing_provider:
            existing_auth_mode = provider.auth_mode()
            if existing_auth_mode is None:
                raise RuntimeError(
                    "Could not determine the existing OpenShell provider auth mode; "
                    "run agentic-ci stop before switching harnesses"
                )

        profile_updated = False
        if existing_provider and existing_auth_mode == auth_mode:
            # The reused provider composes its sandbox rule from the gateway's
            # copy of its profile, which an older agentic-ci or a hand edit may
            # have left binding other binaries. OpenShell v0.1.2 recomposes the
            # rule of a running sandbox when the profile changes, but a sandbox
            # that ran under the drifted rule is not reused anyway: recreating
            # it does not depend on that runtime behavior.
            profile_updated = provider.ensure_auth_mode_profile(auth_mode)

        if sandbox_exists:
            identity = _load_sandbox_identity()
            if identity is None:
                raise RuntimeError(
                    "Could not determine the existing OpenShell sandbox identity; "
                    "run agentic-ci stop before switching harnesses"
                )
            if not existing_provider:
                # Provider-less auth modes are only recorded in the sandbox identity.
                existing_auth_mode = identity.get("auth_mode")
                if not isinstance(existing_auth_mode, str) or provider.requires_provider(
                    existing_auth_mode
                ):
                    raise RuntimeError(
                        "The existing OpenShell sandbox has no identifiable provider; "
                        "run agentic-ci stop before switching harnesses"
                    )
            expected_identity = _sandbox_identity(
                self.harness.name,
                self.image,
                auth_mode,
                credential=credential,
                profile=self.sandbox_profile,
            )
            # A profile sandbox saved without its main process (by an earlier
            # agentic-ci) cannot have its leftover processes killed, so it is
            # recreated rather than reused.
            main_known = self.sandbox_profile is None or (
                sandbox.MainProcess.from_record(identity.get(_MAIN_PROCESS_KEY)) is not None
            )
            identity_matches = (
                existing_auth_mode == auth_mode and _identity_fields(identity) == expected_identity
            )
            if identity_matches and main_known and not profile_updated and self._reuse_sandbox():
                # Toolchains this sandbox already has (by the host's record and
                # the marker) are skipped.
                if self._provision_toolchains(_toolchain_records(identity)):
                    identity[_TOOLCHAINS_KEY] = self._toolchain_records
                    _save_sandbox_identity(identity)
                # Setup steps ran when this sandbox was created; their effects
                # are still in it, so they are reported, not run again.
                self.setup_records = _saved_setup_records(identity)
                self._write_setup_records()
                self._write_environment()
                return

            if existing_auth_mode != auth_mode:
                log.section("Auth mode changed; recreating OpenShell sandbox and provider")
            elif profile_updated:
                log.section("Provider profile updated; recreating OpenShell sandbox")
            elif identity_matches and not main_known:
                log.section("Sandbox has no recorded main process; recreating OpenShell sandbox")
            elif identity_matches:
                log.section("Sandbox could not be reused; recreating OpenShell sandbox")
            else:
                log.section("Sandbox identity changed; recreating OpenShell sandbox")
            sandbox.delete()

        if existing_provider and existing_auth_mode != auth_mode:
            provider.delete()

        log.section("Configuring provider")
        provider.setup(auth_mode=auth_mode, env=env)

        image_info = f", image: {self.image}" if self.image else ""
        log.section(f"Creating sandbox ({image_info.lstrip(', ') or 'default image'})")

        # Drop any identity left by an earlier sandbox before creating a new one.
        # It is only saved once setup succeeds, and for provider-less auth modes
        # it is the only record of the sandbox's auth mode.
        _clear_sandbox_identity()
        self._main_process = None
        # A new sandbox starts in the agent phase.
        self._agent_phase_pending = False
        sandbox.create(
            image=self.image,
            policy_path=self.policy_path,
            otel_port=otel_port,
            workdir=self.workdir,
            approval_mode=self.approval_mode,
            auth_mode=auth_mode,
            memory=self.memory,
            cpu=self.cpu,
            gpu=self.gpu,
            **self._profile_kwargs(),
        )
        if self.sandbox_profile is not None:
            # Nothing else runs yet, so this is the process every phase
            # switch spares when it kills leftovers.
            self._main_process = sandbox.find_main_process()

        self._run_setup_steps()

        log.section("Uploading workdir")
        sandbox.upload(self.workdir)

        provisioned = self._provision_toolchains({})
        self._run_setup_phase()
        self._write_environment()

        self._upload_sandbox_config(otel_enabled=otel_port is not None)
        identity = _sandbox_identity(
            self.harness.name,
            self.image,
            auth_mode,
            credential=credential,
            profile=self.sandbox_profile,
        )
        if self._main_process is not None:
            identity[_MAIN_PROCESS_KEY] = self._main_process.to_record()
        if provisioned:
            identity[_TOOLCHAINS_KEY] = self._toolchain_records
        if self.sandbox_profile is not None and self.sandbox_profile.setup:
            identity[_SETUP_KEY] = [record.to_dict() for record in self.setup_records]
        _save_sandbox_identity(identity)

    def _reuse_sandbox(self) -> bool:
        """Prepare the existing sandbox for this run; return False if it must be recreated.

        An earlier run may have stopped in the setup or validate phase, with
        shim rules live, the provider detached or processes still running;
        the agent must start with none of that. When the switch to the agent
        phase fails (a survivor, a stale main process record, an attach that
        is never confirmed), every later run would fail the same way on this
        sandbox, so it is recreated instead.
        """
        log.section("Sandbox already exists")
        self._warn_unapplied_resources()
        try:
            self._set_egress_phase("agent")
        except RuntimeError as exc:
            log.info(f"WARNING: could not prepare the existing sandbox: {exc}")
            return False
        return True

    def _provision_toolchains(self, recorded: dict[str, str]) -> bool:
        """Install the sandbox profile's toolchains in the sandbox; return whether it ran.

        No-op (False) without a profile or without toolchains. The host
        resolves each version (reading repo files for ``auto``), downloads the
        archive from the tool's official host, verifies it against the
        official checksum and caches it; the sandbox extracts it under
        ``/sandbox/.local/toolchains/<name>-<version>``. One that *recorded*
        (the host's record from the saved identity) and the in-sandbox marker
        both show as installed with the same sha256 is skipped. A toolchain
        that fails is recorded and skipped, and the run goes on. The results
        go to ``<run_dir>/toolchains.json`` (when the caller set
        :attr:`run_dir`) and the job log, the variables of the provisioned
        ones to :attr:`toolchain_env`, and the updated record to
        :attr:`_toolchain_records` for the saved identity.
        """
        profile = self.sandbox_profile
        if profile is None or not profile.toolchains:
            return False
        log.section("Provisioning toolchains")
        provisioned = toolchains.provision(
            profile.toolchains,
            workdir=Path(self.workdir),
            installer=OpenShellInstaller(recorded=recorded),
        )
        self.toolchain_results = provisioned.results
        self.toolchain_env = provisioned.env
        records = dict(recorded)
        for result in provisioned.results:
            if not result.resolved:
                continue
            target = toolchains.sandbox_dir(result.name, result.resolved)
            if result.status in ("installed", "present"):
                records[target] = result.sha256
            else:
                # A failed install may have replaced or removed the directory.
                records.pop(target, None)
        self._toolchain_records = records
        failed = sum(1 for r in provisioned.results if r.status == "failed")
        if failed:
            log.info(f"WARNING: {failed} toolchain(s) not provisioned; the run continues")
        self._write_toolchain_results()
        return True

    def _write_toolchain_results(self) -> None:
        """Write :attr:`toolchain_results` to ``<run_dir>/toolchains.json`` on the host.

        Only for a profile with toolchains (see :meth:`_write_run_record`).
        """
        profile = self.sandbox_profile
        if profile is None or not profile.toolchains:
            return
        results = self.toolchain_results
        self._write_run_record(
            _TOOLCHAIN_RECORD, lambda path: toolchains.write_results(path, results)
        )

    def _write_setup_records(self) -> None:
        """Write :attr:`setup_records` to ``<run_dir>/sandbox-setup.json`` (profile with setup)."""
        profile = self.sandbox_profile
        if profile is None or not profile.setup:
            return
        self._write_json_record(_SETUP_RECORD, [record.to_dict() for record in self.setup_records])

    def _write_run_records(self) -> None:
        """Write this run's records again after the workdir download; remove every other one.

        The download brings the sandbox's copy of ``_run`` back, so anything
        the agent wrote there arrives with it. First a symlink or other
        non-directory at the run directory is replaced by an empty directory
        (see :meth:`_reclaim_run_dir`). Then the records this run holds are
        written again, and every other host record name (:data:`_RUN_RECORDS`)
        is removed, whatever the profile: a validation record is kept only
        when validation ran in this run, and a record the host has nothing to
        write for (a forged ``sandbox-validation.json`` for a profile without
        validate, say) never survives. No-op without a run_dir.
        """
        if self.run_dir is None or not self._reclaim_run_dir():
            return
        profile = self.sandbox_profile
        records: dict[str, Callable[[Path], None]] = {}
        if profile is not None and profile.toolchains:
            results = self.toolchain_results
            records[_TOOLCHAIN_RECORD] = lambda path: toolchains.write_results(path, results)
        if profile is not None and profile.setup:
            records[_SETUP_RECORD] = _json_writer(self.setup_records)
        if self._validation_ran:
            records[_VALIDATION_RECORD] = _json_writer(self.validation_records)
        if profile is not None and profile.discard_before_download:
            records[_DISCARD_RECORD] = _json_writer(self.discard_records)
        for name in _RUN_RECORDS:
            if name in records:
                self._write_run_record(name, records[name])
            else:
                self._remove_run_record(name)

    def _reclaim_run_dir(self) -> bool:
        """Make the run directory a real directory again after the download; return success.

        The agent can replace the workdir's ``_run`` with a symlink to a
        directory it controls, or with a file; consumers would then read its
        records through it. The link or file is removed (never its target)
        and an empty directory created in its place. A missing run directory
        is left missing.
        """
        run_dir = Path(str(self.run_dir))
        try:
            if run_dir.is_symlink() or (run_dir.exists() and not run_dir.is_dir()):
                log.info("WARNING: the run directory was not a directory after the download")
                run_dir.unlink()
                run_dir.mkdir()
        except OSError as exc:
            log.detail("run directory error", f"{type(exc).__name__}: {exc}")
            log.info("WARNING: the run directory could not be restored; run records not written")
            return False
        return True

    def _write_json_record(self, name: str, data: object) -> None:
        self._write_run_record(name, lambda path: steps.write_json(path, data))

    def _clear_run_record_path(self, name: str) -> Path | None:
        """``<run_dir>/<name>`` with nothing left at it, or None when it cannot be written.

        None without a run_dir or when the run directory is a symlink
        (nothing is ever removed or written through it). A directory or any
        other entry at that path is removed, a symlink itself, never its
        target. Raises ``OSError`` when the removal fails.
        """
        if self.run_dir is None:
            return None
        run_dir = Path(self.run_dir)
        if run_dir.is_symlink():
            log.info(f"WARNING: the run directory is a symlink; {name} not written")
            return None
        path = run_dir / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_symlink() or path.exists():
            path.unlink()
        return path

    def _write_run_record(self, name: str, write: Callable[[Path], None]) -> None:
        """Write ``<run_dir>/<name>`` on the host with *write*; no-op without a run_dir.

        ``run()`` calls it again after the workdir download, which would
        otherwise replace the host's record with whatever the sandbox holds at
        that path (the workdir's ``_run`` is downloaded back too). Best
        effort: a directory or other non-file the agent left at that path is
        removed first, and a write that still fails is logged, never raised,
        so it cannot replace the run's exit code or hide a download error.
        """
        try:
            path = self._clear_run_record_path(name)
            if path is not None:
                write(path)
        except OSError as exc:
            log.detail(f"{name} write error", f"{type(exc).__name__}: {exc}")
            log.info(f"WARNING: {name} could not be written")

    def _remove_run_record(self, name: str) -> None:
        """Remove ``<run_dir>/<name>``, a record this run did not write (best effort)."""
        if self.run_dir is None:
            return
        path = Path(self.run_dir) / name
        try:
            if not (path.is_symlink() or path.exists()):
                return
            if self._clear_run_record_path(name) is not None:
                log.info(f"Removed {name} from the run directory: this run did not write it")
        except OSError as exc:
            log.detail(f"{name} removal error", f"{type(exc).__name__}: {exc}")
            log.info(f"WARNING: {name} could not be removed from the run directory")

    def _step_env(self) -> dict[str, str]:
        """The environment of setup steps and validate commands (see :func:`steps.build_env`)."""
        profile = self.sandbox_profile
        return steps.build_env(
            self.toolchain_env.path,
            self.toolchain_env.variables,
            profile.env if profile is not None else {},
        )

    def _sandbox_workdir(self) -> str:
        return f"/sandbox/{os.path.basename(self.workdir)}"

    def _run_setup_phase(self) -> None:
        """Run the profile's setup steps in the sandbox under the setup phase, then switch to agent.

        No-op without setup steps, so no phase is switched. Each step runs in
        order through the setup shim with its own timeout, in the workdir, with
        :meth:`_step_env` (no credential: the env script does not exist yet).
        A step that fails or times out is recorded and the next one still
        runs; the records go to :attr:`setup_records` and
        ``<run_dir>/sandbox-setup.json``. When the setup phase cannot be
        opened, no step runs and each is recorded as ``not_run``. The switch
        back to the agent phase must succeed: it raises, and setup fails,
        rather than let the agent start with setup egress open.
        """
        profile = self.sandbox_profile
        if profile is None or not profile.setup:
            return
        log.section(f"Running {len(profile.setup)} setup step(s) in the sandbox")
        records: list[steps.StepRecord] = []
        try:
            self._set_egress_phase("setup")
        except RuntimeError as exc:
            log.detail("setup phase error", str(exc))
            log.info("WARNING: the setup phase could not be opened; setup steps not run")
            records = [
                steps.StepRecord(step.name, "not_run", reason="the setup phase could not be opened")
                for step in profile.setup
            ]
        else:
            env = self._step_env()
            secrets = secret_values(self._merged_env())
            for step in profile.setup:
                records.append(
                    steps.run_step(
                        step.name,
                        step.run,
                        step.timeout,
                        env=env,
                        cwd=self._sandbox_workdir(),
                        secrets=secrets,
                    )
                )
        self.setup_records = tuple(records)
        failed = sum(1 for record in records if record.status != "passed")
        if failed:
            log.info(f"WARNING: {failed} setup step(s) did not pass; the run continues")
        self._write_setup_records()
        self._set_egress_phase("agent")

    def _write_environment(self) -> None:
        """Write ``ENVIRONMENT.md`` and ``environment.json`` to ``/sandbox/.agentic-ci``.

        For every sandbox profile, before the agent starts (see
        :mod:`agentic_ci.backends.openshell.environment`). Best effort: a
        failure is logged and the run goes on without the files.
        """
        profile = self.sandbox_profile
        if profile is None:
            return
        data = environment.build(
            profile,
            toolchain_results=self.toolchain_results,
            toolchain_env=self.toolchain_env,
            setup_records=self.setup_records,
            workdir=self._sandbox_workdir(),
        )
        local_dir = tempfile.mkdtemp(prefix=".agentic-ci-environment-")
        try:
            Path(local_dir, environment.ENVIRONMENT_MD).write_text(
                environment.render_markdown(data), encoding="utf-8"
            )
            steps.write_json(Path(local_dir, environment.ENVIRONMENT_JSON), data)
            sandbox.upload(local_dir, timeout=steps.ENVIRONMENT_TIMEOUT_SECONDS)
            written = steps.install_environment_files(
                f"/sandbox/{os.path.basename(local_dir)}",
                environment.SANDBOX_ENVIRONMENT_DIR,
                [environment.ENVIRONMENT_MD, environment.ENVIRONMENT_JSON],
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.detail("environment files error", type(exc).__name__)
            written = False
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)
        if written:
            log.info(f"Wrote {environment.SANDBOX_ENVIRONMENT_DIR}/{environment.ENVIRONMENT_MD}")
        else:
            log.info("WARNING: ENVIRONMENT.md could not be written; the run continues")

    def _credential_files(self) -> list[str]:
        """Harness credential files and the env script, which validate must not read."""
        files = getattr(self.harness, "sandbox_credential_files", ())
        if not isinstance(files, (tuple, list)):
            files = ()
        paths = [f for f in files if isinstance(f, str) and f.startswith("/")]
        return list(dict.fromkeys([*paths, self._ENV_SCRIPT]))

    def _wipe_credentials(self) -> None:
        """Delete the harness credential files in the sandbox (raises when not confirmed)."""
        steps.wipe_files(self._credential_files())

    def _run_validation(self) -> bool:
        """Run the profile's validate commands after the agent; return whether phases switched.

        Opens the validate phase, which kills every process left in the
        sandbox and then deletes the harness credential files (such as Codex's
        ``$CODEX_HOME/auth.json``) before it detaches the provider and applies
        the validate policy. Each command runs through the setup shim with
        :meth:`_step_env` and its timeout; then the sandbox returns to the
        agent phase so a reused sandbox starts clean. Declared ``skips`` are
        recorded as ``skipped``. When the validate phase cannot be opened
        (including a wipe that cannot be confirmed), no command runs and each
        is recorded as ``not_run``. Nothing here raises: validation never
        changes the run's exit code. Returns True when it switched phases
        (with validate commands), which kills every leftover process.
        """
        profile = self.sandbox_profile
        if profile is None or not (profile.validate or profile.skips):
            return False
        self._validation_ran = True
        records: list[steps.StepRecord] = []
        if profile.validate:
            log.section(f"Running {len(profile.validate)} validate command(s) in the sandbox")
            try:
                self._set_egress_phase("validate", before_open=self._wipe_credentials)
            except RuntimeError as exc:
                log.detail("validate phase error", str(exc))
                log.info("WARNING: the validate phase could not be opened; validation not run")
                records = [
                    steps.StepRecord(
                        step.name,
                        "not_run",
                        kind=step.kind,
                        reason="the validate phase could not be opened",
                    )
                    for step in profile.validate
                ]
            else:
                env = self._step_env()
                secrets = secret_values(self._merged_env())
                for step in profile.validate:
                    records.append(
                        steps.run_step(
                            step.name,
                            step.run,
                            step.timeout,
                            env=env,
                            cwd=self._sandbox_workdir(),
                            secrets=secrets,
                            kind=step.kind,
                        )
                    )
            try:
                self._set_egress_phase("agent")
            except RuntimeError as exc:
                # _agent_phase_pending stays set: the next run() or setup()
                # switches the sandbox to the agent phase first (setup()
                # recreates it when that fails) and never starts the agent
                # without it.
                log.detail("agent phase error", str(exc))
                log.info(
                    "WARNING: could not return the sandbox to the agent phase after validation"
                )
        records.extend(
            steps.StepRecord(skip.match, "skipped", kind="skip", reason=skip.reason)
            for skip in profile.skips
        )
        self.validation_records = tuple(records)
        return bool(profile.validate)

    def _stop_leftovers_before_discard(self) -> None:
        """Kill the processes the agent left before the discard, when no phase switch did.

        Only for a profile with discard paths. Best effort: a failure is
        logged and the discard still runs.
        """
        profile = self.sandbox_profile
        if profile is None or not profile.discard_before_download:
            return
        try:
            sandbox.stop_leftover_processes(self._sandbox_main_process(), "discard")
        except RuntimeError as exc:
            log.detail("leftover processes error", str(exc))
            log.info("WARNING: the processes left in the sandbox could not all be stopped")

    def _restore_discarded(self) -> None:
        """Put back what the previous run on this sandbox discarded, before the agent starts.

        :meth:`_discard_before_download` moves the paths out of the workdir
        instead of deleting them, so a later run (the skill after a classifier
        run, or a retry) still has what the setup steps installed. Best
        effort; no-op without discard paths.
        """
        profile = self.sandbox_profile
        if profile is None or not profile.discard_before_download:
            return
        if steps.restore_discarded(self._sandbox_workdir()) is None:
            log.info("WARNING: the paths discarded by the previous run could not be restored")

    def _discard_before_download(self) -> None:
        """Move the profile's ``discard_before_download`` paths out of the sandbox workdir."""
        profile = self.sandbox_profile
        if profile is None or not profile.discard_before_download:
            return
        log.section("Removing discard_before_download paths")
        self.discard_records = tuple(
            steps.discard_paths(self._sandbox_workdir(), profile.discard_before_download)
        )

    def _profile_kwargs(self) -> dict:
        """``profile=`` for ``sandbox.create``, passed only when a profile is set."""
        return {} if self.sandbox_profile is None else {"profile": self.sandbox_profile}

    def _sandbox_main_process(self) -> sandbox.MainProcess:
        """Return the main process recorded when the sandbox was created.

        Loaded from the saved identity when this backend did not create the
        sandbox. Raises ``RuntimeError`` when there is no usable record.
        """
        if self._main_process is None:
            identity = _load_sandbox_identity() or {}
            self._main_process = sandbox.MainProcess.from_record(identity.get(_MAIN_PROCESS_KEY))
        if self._main_process is None:
            raise RuntimeError(
                "The OpenShell sandbox's main process was not recorded; "
                "run agentic-ci stop to recreate the sandbox"
            )
        return self._main_process

    def _set_egress_phase(
        self, phase: str, *, before_open: Callable[[], None] | None = None
    ) -> None:
        """Open the sandbox profile's egress for *phase* to the setup shim.

        ``setup`` and ``validate`` bind the endpoints of the profile's presets
        open in that phase, plus its raw egress, to the setup shim only, and
        park the agent's rules so no process matches them; ``agent`` strips
        every shim-bound rule and restores the agent's rules. No-op without a
        sandbox profile.

        The shim lets setup and validate reach preset hosts without widening
        the agent's egress, but it is not a boundary against the agent, which
        could run it while its rules are live. So every switch, in order:

        - ``setup``/``validate``: kills every process an earlier exec left
          running (the agent, its daemons, earlier steps); for openai,
          api-key and vertex auth, makes sure a fresh exec gets the
          credential placeholder (attaching the provider first if not) and
          detaches the provider;
          applies the phase policy, which, issued after the detach, also
          confirms the provider's composed rule is gone when the policy
          changes; then waits until a fresh exec no longer gets the
          placeholder.
        - ``agent``: kills leftovers as well, applies the agent policy,
          attaches the provider again and waits until the placeholder is
          back, so the agent can authenticate.

        Killing on the way into the agent phase too means every phase starts
        with only the sandbox's main process running. A setup leftover has
        already lost the shim's egress by then, but it could still write the
        workdir while the agent works, and on a reused sandbox the previous
        run's processes would otherwise run next to this agent. Setup cannot
        start services for the agent or for validate; validate kills them
        anyway.

        *before_open* runs right after the kill, while nothing else runs in
        the sandbox and before the provider or policy changes; an exception
        it raises stops the switch. The validate phase deletes the harness
        credential files there, so no leftover can write them again.

        Every step is idempotent, so switching to the phase already in effect
        is safe, and a reused sandbox left mid-phase (provider detached) is
        repaired by the switch to ``agent``; :meth:`setup` recreates one it
        cannot repair. Raises ``RuntimeError`` when a step fails, and the
        caller must then not run the phase.

        A policy change closes every open proxied connection in the sandbox,
        so call this only while nothing runs there. The agent phase must be
        in effect before the agent starts, because the agent can run the shim.
        """
        profile = self.sandbox_profile
        if profile is None:
            return
        endpoints = [] if phase == "agent" else phase_endpoints(profile, phase)
        key_env_vars = provider.provider_env_vars(
            self.harness.auth_mode_for_env(self._merged_env())
        )
        log.section(f"Switching sandbox egress to the {phase} phase")
        if phase != "agent":
            # Cleared only by a switch back to agent that succeeds.
            self._agent_phase_pending = True
        sandbox.stop_leftover_processes(self._sandbox_main_process(), phase)
        if before_open is not None:
            before_open()
        if phase == "agent":
            sandbox.apply_phase_policy(phase, endpoints)
            if key_env_vars:
                sandbox.attach_provider(phase)
                sandbox.wait_for_provider_env(key_env_vars, attached=True, phase=phase)
            self._agent_phase_pending = False
            return
        if key_env_vars:
            # A DETACHED probe proves a detach only as a change from an
            # observed ATTACHED; otherwise a probe that cannot see the
            # placeholder would confirm it at once. A sandbox not seen
            # attached (setup straight to validate, or an odd probe) is
            # attached first; nothing runs in it now, so that is harmless.
            if sandbox.provider_env_state(key_env_vars) != "ATTACHED":
                sandbox.attach_provider(phase)
                sandbox.wait_for_provider_env(key_env_vars, attached=True, phase=phase)
            sandbox.detach_provider(phase)
        # Issued after the detach, so when the policy changes, "policy set
        # --wait" returns only once the supervisor has loaded a policy the
        # gateway composed without the provider's rule.
        sandbox.apply_phase_policy(phase, endpoints)
        if key_env_vars:
            sandbox.wait_for_provider_env(key_env_vars, attached=False, phase=phase)

    def _warn_unapplied_resources(self):
        """Say so when a reused sandbox keeps an allocation the caller did not ask for.

        Resource limits are fixed when the sandbox is created, so reuse silently
        discards whatever ``memory``, ``cpu`` or ``gpu`` this backend was given.
        Left unsaid, that is the same failure the limits themselves cause: work
        runs against a ceiling nobody chose and the symptom appears somewhere else
        entirely.

        A warning rather than an error, because reuse is a deliberate feature and
        the existing allocation may already be correct. Verifying that would mean
        parsing ``openshell sandbox get`` and comparing units -- ``1``, ``1000m``
        and ``1.0`` are the same CPU -- which is a contract worth adding only once
        something needs to depend on it.

        Values taken from the sandbox profile are not reported: the profile's
        hash is part of the sandbox identity, so a reused sandbox was created
        with them.
        """
        asked_for = self._explicit_resources
        if not asked_for:
            return
        values = ", ".join(f"{k}={v}" for k, v in asked_for.items())
        log.info(
            f"WARNING: {values} not applied -- resource limits are set at creation "
            f"and this sandbox already exists. Delete it to apply new values."
        )

    @classmethod
    def _agent_command(cls, sandbox_workdir, agent_args):
        """Build the in-sandbox command that sources the env script and execs the agent.

        The env script is removed as soon as it has been sourced so API keys
        it exports are not left readable on disk for the agent to find.
        """
        return [
            "bash",
            "-c",
            f"cd {shlex.quote(sandbox_workdir)} && . {cls._ENV_SCRIPT}"
            f' && rm -f {cls._ENV_SCRIPT} && exec "$@"',
            "--",
            *agent_args,
        ]

    def _upload_sandbox_config(self, otel_enabled=False):
        """Write harness-specific config and upload it to the sandbox."""
        config_dir = tempfile.mkdtemp(prefix="agentic-ci-config-")
        try:
            self.harness.write_sandbox_config(config_dir, otel_enabled=otel_enabled)
            for host_path, container_path in self.harness.sandbox_config_mounts(config_dir):
                sandbox.upload(host_path)
                fname = os.path.basename(host_path)
                target_dir = os.path.dirname(container_path)
                sandbox.exec_cmd(["mkdir", "-p", target_dir])
                sandbox.exec_cmd(["mv", fname, container_path])
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    def stop(self):
        try:
            if gateway.is_running() and sandbox.exists():
                sandbox.delete()
                _clear_sandbox_identity()
                log.section("Sandbox deleted")
            else:
                _clear_sandbox_identity()
                log.section("No sandbox to stop")
        finally:
            gateway.stop()
            log.section("Gateway stopped")

    def run(
        self,
        prompt,
        model,
        streaming=True,
        otel_port=None,
        otel_rate_file=None,
        extra_args=None,
        traceparent=None,
        effort=None,
    ):
        env = self._merged_env()
        auth_mode = self.harness.auth_mode_for_env(env)
        self.validation_records = ()
        self.discard_records = ()
        self._validation_ran = False
        if self._agent_phase_pending:
            # An earlier switch back to the agent phase failed (after
            # validation, say): no credential reaches the sandbox, and no
            # agent starts, until it succeeds. Raises otherwise.
            log.info("The sandbox is not in the agent phase; switching it before the agent")
            self._set_egress_phase("agent")
        self._write_env_script(
            model,
            otel_port,
            otel_rate_file,
            traceparent=traceparent,
            env=env,
            auth_mode=auth_mode,
            effort=effort,
        )
        otel_endpoint = f"http://{_OPENSHELL_HOST}:{otel_port}" if otel_port else None
        agent_args = self.harness.build_args(
            prompt,
            model,
            extra_args,
            otel_endpoint=otel_endpoint,
            externally_sandboxed=True,
        )

        workdir_name = os.path.basename(self.workdir)
        sandbox_workdir = f"/sandbox/{workdir_name}"
        cmd = self._agent_command(sandbox_workdir, agent_args)
        self._restore_discarded()

        # The download below copies the sandbox's .git over the host repo, and
        # the agent can write that .git. Only the host can write the workdir
        # until then, so snapshot it here and restore it after the download.
        self._snapshot_host_git()

        stop_keepalive = threading.Event()
        keepalive: threading.Thread | None = None

        # The token-lapse race only affects the OpenShell gateway's minted
        # Vertex credential; the API-key auth path is unaffected.
        if auth_mode == "vertex":
            log.section("Starting GCP token keepalive")
            keepalive = threading.Thread(
                target=_token_keepalive, args=(stop_keepalive,), daemon=True
            )
            keepalive.start()

        try:
            proc = sandbox.exec_cmd_streaming(cmd)

            rc, stream_complete = self._process_stream(proc, streaming)
            self._wait_for_otel_flush(otel_port)

            # Repo code runs again here, after the agent: only with the
            # credential files deleted, under the validate phase. Then the
            # discarded paths go, so they are not downloaded.
            switched = False
            if self.validate_after_run:
                switched = self._run_validation()
            if not switched:
                # No phase switch killed the agent's leftovers; one could
                # otherwise recreate a discarded path before the download.
                self._stop_leftovers_before_discard()
            self._discard_before_download()

            log.section("Downloading workdir")
            try:
                sandbox.download(sandbox_workdir, self.workdir)
            finally:
                # The download brings the sandbox's _run back too; the host's
                # records win over anything written there.
                self._write_run_records()

            rc = self._resolve_exit_code(rc, stream_complete)
            return rc
        finally:
            stop_keepalive.set()
            if keepalive:
                keepalive.join(timeout=5)
            self._restore_host_git(release=True)

    def _profile_script_lines(self, earlier: list[str]) -> list[str]:
        """The env script's toolchain and profile ``env`` exports, which follow *earlier*.

        A profile name that *earlier* exports, or that the caller passed in
        ``extra_env``, is left out, so agentic-ci's value is the one in
        effect; the profile's names are validated against the reserved
        names, so this only catches what that list misses.
        """
        lines = self.toolchain_env.script_lines()
        profile = self.sandbox_profile
        if profile is None or not profile.env:
            return lines
        taken = {m.group(1) for line in earlier if (m := _EXPORT_RE.match(line))}
        taken.update(self._extra_env)
        dropped = sorted(name for name in profile.env if name in taken)
        if dropped:
            log.detail("profile env not exported", ", ".join(dropped))
            log.info(
                f"WARNING: {len(dropped)} profile env variable(s) not exported to the agent: "
                "agentic-ci sets them"
            )
        lines.extend(
            f"export {key}={shlex.quote(value)}"
            for key, value in profile.env.items()
            if key not in taken
        )
        return lines

    def _write_env_script(
        self,
        model,
        otel_port=None,
        otel_rate_file=None,
        traceparent=None,
        env=None,
        auth_mode=None,
        effort=None,
    ):
        """Write env vars to a script inside the sandbox, sourced before the agent runs.

        Uses the harness's native env script (Vertex AI vars, API key, and
        OTEL vars). Provider credentials arrive as gateway-injected
        placeholders in the sandbox environment, so the script only wires
        the harness to them. The harness handles OTEL endpoint configuration
        using the gateway host address.
        """
        env = self._merged_env() if env is None else env
        auth_mode = self.harness.auth_mode_for_env(env) if auth_mode is None else auth_mode
        script_env = env
        if auth_mode == "openai":
            # The provider already sets OPENAI_API_KEY in the sandbox to an
            # OpenShell placeholder, and the proxy swaps it for the real key
            # only on requests to api.openai.com. Exporting the real key here
            # would also land in $CODEX_HOME/auth.json via codex login, where
            # any later sandbox process could read it.
            script_env = {k: v for k, v in env.items() if k not in _OPENAI_CREDENTIAL_ENV_VARS}
        lines = self.harness.build_env_script_lines(
            otel_port=otel_port,
            traceparent=traceparent,
            env=script_env,
        )
        if otel_port:
            # The harness sets the OTel endpoint to 10.200.0.1 (the gateway IP
            # used by the Podman backend). OpenShell sandboxes can't reach that
            # address — they resolve the host via host.openshell.internal.
            lines.append(f"export OTEL_EXPORTER_OTLP_ENDPOINT=http://{_OPENSHELL_HOST}:{otel_port}")
        if not otel_port and self.harness.name == "Claude Code":
            lines.append("export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")

        if auth_mode == "vertex":
            max_retries = env.get("CLAUDE_CODE_MAX_RETRIES", _DEFAULT_MAX_RETRIES)
            lines.append(f"export CLAUDE_CODE_MAX_RETRIES={shlex.quote(max_retries)}")

        for key, val in self._extra_env.items():
            if auth_mode == "openai" and key in _OPENAI_CREDENTIAL_ENV_VARS:
                continue
            # Exported below from *effort*, so the agent only sees the effort in effect.
            if key == AGENT_EFFORT_ENV_VAR:
                continue
            lines.append(f"export {key}={shlex.quote(val)}")

        lines.append(f"export AGENT_MODEL={shlex.quote(model)}")
        if effort is not None:
            lines.append(f"export {AGENT_EFFORT_ENV_VAR}={shlex.quote(effort)}")

        lines.extend(
            [
                "if command -v agentic-ci >/dev/null 2>&1; then",
                "    agentic-ci enable-plugins",
                "fi",
            ]
        )

        # The toolchain variables and the profile's env (which the setup steps
        # and validate commands get too) come last, after every command the
        # script runs, so they reach the agent and never agentic-ci's own
        # commands. Both are empty without a profile.
        lines.extend(self._profile_script_lines(lines))

        script = "\n".join(lines) + "\n"

        with tempfile.NamedTemporaryFile(
            mode="w", prefix="agentic-ci-env-", suffix=".sh", delete=False
        ) as f:
            f.write(script)
            local_path = f.name

        sandbox.upload(local_path)
        sandbox.exec_cmd(
            ["bash", "-c", f"mv {shlex.quote(os.path.basename(local_path))} {self._ENV_SCRIPT}"]
        )
        os.unlink(local_path)
