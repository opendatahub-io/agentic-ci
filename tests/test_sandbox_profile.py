"""Tests for sandbox profile parsing, merging and serialization."""

import dataclasses

import pytest
import yaml

from agentic_ci.sandbox_profile import (
    DEFAULT_OVERLAY_ALLOWED_PRESETS,
    KNOWN_EGRESS_PRESETS,
    KNOWN_TOOLCHAINS,
    MAX_ENTRIES,
    MAX_WARNINGS,
    FrozenMap,
    MergedProfile,
    ParsedProfile,
    Resources,
    SandboxProfile,
    SandboxProfileError,
    SetupStep,
    Skip,
    ValidateStep,
    merge_profiles,
    parse_profile,
    profile_hash,
    profile_to_dict,
)

SECRET_VALUE = "hunter2-do-not-leak"
SECRET_RUN = "curl -H 'Authorization: sekrit-run-string' https://example.invalid"

FULL_CENTRAL = {
    "toolchains": {"go": "auto", "node": "22", "shfmt": "3.12.0"},
    "egress": ["goproxy", "npm", "example.com:443:read-only"],
    "setup": [{"name": "modules", "run": "go mod download", "timeout": 900}],
    "validate": [
        {"name": "lint", "kind": "lint", "run": "make lint", "timeout": 900},
        {"name": "unit", "kind": "test", "run": "make test"},
    ],
    "skips": [{"match": "unshare --net", "reason": "network namespaces are blocked"}],
    "env": {"CGO_ENABLED": "0"},
    "resources": {"memory": "8Gi", "cpu": "4"},
    "discard_before_download": ["node_modules"],
    "overlay": "merge",
}


def central(data):
    return parse_profile(data, source="central").profile


def overlay(data):
    return parse_profile(data, source="overlay")


def central_error(data):
    with pytest.raises(SandboxProfileError) as excinfo:
        parse_profile(data, source="central")
    return excinfo.value


class TestParseBasics:
    def test_full_profile(self):
        parsed = parse_profile(FULL_CENTRAL, source="central")
        assert isinstance(parsed, ParsedProfile)
        assert parsed.warnings == ()
        profile = parsed.profile
        assert dict(profile.toolchains) == {"go": "auto", "node": "22", "shfmt": "3.12.0"}
        assert profile.egress == ("goproxy", "npm")
        assert profile.raw_egress == ("example.com:443:read-only",)
        assert profile.setup == (SetupStep("modules", "go mod download", 900),)
        assert profile.validate == (
            ValidateStep("lint", "lint", "make lint", 900),
            ValidateStep("unit", "test", "make test", 600),
        )
        assert profile.skips == (Skip("unshare --net", "network namespaces are blocked"),)
        assert dict(profile.env) == {"CGO_ENABLED": "0"}
        assert profile.resources == Resources(memory="8Gi", cpu="4")
        assert profile.discard_before_download == ("node_modules",)
        assert profile.overlay == "merge"

    def test_empty_profile_is_all_defaults(self):
        assert central({}) == SandboxProfile()

    def test_null_fields_mean_defaults(self):
        data = dict.fromkeys(FULL_CENTRAL)
        assert central(data) == SandboxProfile()

    def test_yaml_input(self):
        text = """
toolchains:
  go: auto
  node: 22
egress: [goproxy]
env:
  CGO_ENABLED: 0
"""
        profile = central(yaml.safe_load(text))
        assert dict(profile.toolchains) == {"go": "auto", "node": "22"}
        assert dict(profile.env) == {"CGO_ENABLED": "0"}

    @pytest.mark.parametrize("data", [None, [], "toolchains: {}", 3])
    def test_profile_must_be_a_mapping(self, data):
        err = central_error(data)
        assert "must be a mapping" in str(err)

    def test_unknown_source_is_a_programming_error(self):
        with pytest.raises(ValueError, match="source"):
            parse_profile({}, source="repo")  # type: ignore[arg-type]

    def test_profile_is_immutable_and_hashable(self):
        profile = central(FULL_CENTRAL)
        with pytest.raises(dataclasses.FrozenInstanceError):
            profile.egress = ()  # type: ignore[misc]
        with pytest.raises(TypeError):
            profile.env["NEW"] = "x"  # type: ignore[index]
        assert hash(profile) == hash(central(FULL_CENTRAL))

    def test_constructor_freezes_plain_containers(self):
        profile = SandboxProfile(toolchains={"go": "auto"}, egress=["npm"])  # type: ignore[arg-type]
        assert isinstance(profile.toolchains, FrozenMap)
        assert profile.egress == ("npm",)
        hash(profile)

    @pytest.mark.parametrize(
        "field", ["egress", "raw_egress", "setup", "validate", "skips", "discard_before_download"]
    )
    def test_constructor_rejects_a_string_for_a_list_field(self, field):
        with pytest.raises(TypeError, match=field):
            SandboxProfile(**{field: "pypi"})

    def test_constructor_turns_empty_resources_into_none(self):
        profile = SandboxProfile(resources=Resources())
        assert profile.resources is None
        assert profile == SandboxProfile()
        assert profile_hash(profile) == profile_hash(SandboxProfile())
        assert central(profile_to_dict(profile)) == profile

    @pytest.mark.parametrize(
        ("data", "path"),
        [
            ({"toolchains": {"go": "1.2\n"}}, "toolchains.go"),
            ({"setup": [{"name": "a\n", "run": "x"}]}, "setup[0].name"),
            ({"env": {"FOO\n": "x"}}, "env.'FOO\\n'"),
            ({"resources": {"memory": "8Gi\n"}}, "resources.memory"),
            ({"resources": {"cpu": "4\n"}}, "resources.cpu"),
            ({"egress": ["a.com:443\n:full"]}, "egress[0]"),
        ],
    )
    def test_trailing_newline_is_rejected(self, data, path):
        assert central_error(data).path == path

    def test_error_is_a_value_error_with_path(self):
        err = central_error({"validate": [{"name": "a", "kind": "lint", "run": "x"}, {}]})
        assert isinstance(err, ValueError)
        assert err.path == "validate[1].name"


class TestUnknownKeys:
    def test_central_unknown_top_level_key_errors(self):
        err = central_error({"toolchain": {}})
        assert err.path == "toolchain"
        assert "unknown key" in str(err)

    def test_overlay_unknown_top_level_key_warns(self):
        parsed = overlay({"toolchain": {}, "egress": ["npm"]})
        assert parsed.profile.egress == ("npm",)
        assert len(parsed.warnings) == 1
        assert parsed.warnings[0].startswith("toolchain: unknown key")

    @pytest.mark.parametrize(
        ("data", "path"),
        [
            ({"setup": [{"name": "a", "run": "x", "shell": "sh"}]}, "setup[0].shell"),
            (
                {"validate": [{"name": "a", "kind": "lint", "run": "x", "cwd": "."}]},
                "validate[0].cwd",
            ),
            ({"skips": [{"match": "a", "reason": "b", "why": "c"}]}, "skips[0].why"),
            ({"resources": {"disk": "10Gi"}}, "resources.disk"),
        ],
    )
    def test_central_unknown_nested_keys_error(self, data, path):
        assert central_error(data).path == path

    def test_overlay_unknown_key_cannot_forge_a_warning_line(self):
        parsed = overlay({"foo\nWARN: fake": 1, "setup": [{"name": "a", "run": "x", "b\rc": 1}]})
        assert len(parsed.warnings) == 2
        assert all("\n" not in w and "\r" not in w for w in parsed.warnings)
        assert parsed.warnings[0].startswith("'foo\\nWARN: fake': unknown key")

    def test_non_string_key_is_quoted(self):
        assert central_error({3: 1}).path == "'3'"

    def test_overlay_unknown_nested_key_warns(self):
        parsed = overlay({"setup": [{"name": "a", "run": "x", "shell": "sh"}]})
        assert parsed.profile.setup == (SetupStep("a", "x"),)
        assert parsed.warnings[0].startswith("setup[0].shell: unknown key")


class TestToolchains:
    @pytest.mark.parametrize("version", ["auto", "1", "22", "1.26", "3.12.0", "0.0.1"])
    def test_valid_versions(self, version):
        assert central({"toolchains": {"go": version}}).toolchains["go"] == version

    @pytest.mark.parametrize(
        "version", ["", "latest", "v1.2", "1.2.3.4", "1.", ".1", "1.2-rc1", "AUTO", " 1", "1 "]
    )
    def test_invalid_versions(self, version):
        err = central_error({"toolchains": {"go": version}})
        assert err.path == "toolchains.go"
        assert "version" in err.rule

    def test_int_versions_are_converted(self):
        assert central({"toolchains": {"node": 22}}).toolchains["node"] == "22"

    @pytest.mark.parametrize("version", [1.2, 1.26, 22.0])
    def test_float_versions_are_rejected_with_quoting_advice(self, version):
        err = central_error({"toolchains": {"go": version}})
        assert err.path == "toolchains.go"
        assert "quoted" in err.rule

    @pytest.mark.parametrize("version", [True, None, -1, ["1"], {"v": "1"}])
    def test_non_version_types_rejected(self, version):
        assert central_error({"toolchains": {"go": version}}).path == "toolchains.go"

    def test_unknown_toolchain_rejected(self):
        err = central_error({"toolchains": {"rust": "1.80"}})
        assert err.path == "toolchains.rust"
        assert "unknown toolchain" in err.rule

    def test_unknown_toolchain_with_odd_name_is_quoted(self):
        assert central_error({"toolchains": {"ru st": "1"}}).path == "toolchains.'ru st'"

    def test_unknown_toolchain_warns_and_drops_for_overlay(self):
        parsed = overlay({"toolchains": {"rust": "1.80", "go": "auto"}})
        assert dict(parsed.profile.toolchains) == {"go": "auto"}
        assert len(parsed.warnings) == 1
        assert parsed.warnings[0].startswith("toolchains.rust: unknown toolchain")
        assert parsed.warnings[0].endswith("; ignored")

    def test_bad_version_still_raises_for_overlay(self):
        with pytest.raises(SandboxProfileError):
            overlay({"toolchains": {"go": "latest"}})

    def test_every_known_toolchain_accepted(self):
        profile = central({"toolchains": dict.fromkeys(KNOWN_TOOLCHAINS, "auto")})
        assert set(profile.toolchains) == KNOWN_TOOLCHAINS

    def test_toolchains_must_be_a_mapping(self):
        assert central_error({"toolchains": ["go"]}).path == "toolchains"


class TestEgress:
    def test_every_known_preset_accepted(self):
        profile = central({"egress": sorted(KNOWN_EGRESS_PRESETS)})
        assert set(profile.egress) == KNOWN_EGRESS_PRESETS

    def test_unknown_preset_rejected(self):
        err = central_error({"egress": ["pypi", "crates"]})
        assert err.path == "egress[1]"
        assert "unknown egress preset" in err.rule

    def test_unknown_preset_warns_and_drops_for_overlay(self):
        parsed = overlay({"egress": ["crates", "npm"]})
        assert parsed.profile.egress == ("npm",)
        assert len(parsed.warnings) == 1
        assert parsed.warnings[0].startswith("egress[0]: unknown egress preset 'crates'")

    def test_duplicates_are_dropped(self):
        profile = central({"egress": ["npm", "npm", "a.com:443:full", "a.com:443:full"]})
        assert profile.egress == ("npm",)
        assert profile.raw_egress == ("a.com:443:full",)

    @pytest.mark.parametrize(
        "endpoint",
        [
            "example.com:443:read-only",
            "*.example.com:443:read-write",
            "example.com:1:full",
            "example.com:65535:full",
            "example.com:443:read-write:rest",
            "example.com:443:read-write:rest:audit",
            "api.example.com:443:read-write:::allow-uninspected-credentials",
            "rt.example.com:443:read-write:websocket:enforce:"
            "websocket-credential-rewrite,allowed-ip=10.0.0.0/8",
            "10.0.0.1:8080:full",
            "my-host.example.com:443:full",
        ],
    )
    def test_valid_raw_endpoints(self, endpoint):
        assert central({"egress": [endpoint]}).raw_egress == (endpoint,)

    @pytest.mark.parametrize(
        "endpoint",
        [
            "example.com:443",
            ":443:full",
            "example.com:0:full",
            "example.com:65536:full",
            "example.com:https:full",
            "example.com:443:write",
            "example.com:443:full:rest:enforce:opt:extra",
            "example.com :443:full",
            "example.com:443:full\n",
            "example.com:\uff14\uff14\uff13:full",
            "example.com:\u00b2:full",
            "example.com:\u0664\u0664\u0663:full",
            "example.com:0443443:full",
            "-oProxy.example.com:443:full",
            "a/b:443:full",
            "user@example.com:443:full",
            "a.*.example.com:443:full",
            "*:443:full",
            "ex\u00e4mple.com:443:full",
            "example-.com:443:full",
            "example.com:443:full:http",
            "example.com:443:full:tcp",
            "example.com:443:full:rest:strict",
            "example.com:443:full::enforce",
            "example.com:443:full:rest:enforce:",
            "example.com:443:full:rest:enforce:bogus",
            "example.com:443:full:rest:enforce:allowed-ip=",
            "example.com:443:full:rest:enforce:allow-uninspected-credentials,",
        ],
    )
    def test_invalid_raw_endpoints(self, endpoint):
        err = central_error({"egress": [endpoint]})
        assert err.path == "egress[0]"
        assert "raw endpoint" in err.rule
        assert endpoint not in err.rule

    @pytest.mark.parametrize("entry", ["", None, 443, {"host": "x"}])
    def test_entries_must_be_non_empty_strings(self, entry):
        assert central_error({"egress": [entry]}).path == "egress[0]"

    def test_overlay_raw_endpoint_dropped_with_warning(self):
        parsed = overlay({"egress": ["npm", "evil.example.com:443:full"]})
        assert parsed.profile.egress == ("npm",)
        assert parsed.profile.raw_egress == ()
        assert parsed.warnings == (
            "egress[1]: raw endpoints are accepted only from the central profile; dropped",
        )


class TestSteps:
    @pytest.mark.parametrize("field", ["setup", "validate"])
    def test_default_timeout(self, field):
        if field == "validate":
            step = {"name": "a", "run": "x", "kind": "lint"}
        else:
            step = {"name": "a", "run": "x"}
        assert getattr(central({field: [step]}), field)[0].timeout == 600

    @pytest.mark.parametrize("timeout", [1, 600, 3600])
    def test_valid_timeouts(self, timeout):
        profile = central({"setup": [{"name": "a", "run": "x", "timeout": timeout}]})
        assert profile.setup[0].timeout == timeout

    @pytest.mark.parametrize("timeout", [0, -5, 3601, "600", 1.5, True, None])
    def test_invalid_timeouts(self, timeout):
        err = central_error({"setup": [{"name": "a", "run": "x", "timeout": timeout}]})
        assert err.path == "setup[0].timeout"

    @pytest.mark.parametrize("name", ["", "has space", "a/b", "semi;colon", None, 3])
    def test_invalid_names(self, name):
        err = central_error({"setup": [{"name": name, "run": "x"}]})
        assert err.path == "setup[0].name"

    def test_name_charset(self):
        profile = central({"setup": [{"name": "Go_mod-1.x", "run": "x"}]})
        assert profile.setup[0].name == "Go_mod-1.x"

    def test_names_must_be_unique_within_a_list(self):
        err = central_error(
            {"validate": [{"name": "a", "kind": "lint", "run": "x"}] * 2},
        )
        assert err.path == "validate[1].name"
        assert "duplicate" in err.rule

    @pytest.mark.parametrize("field", ["setup", "validate"])
    def test_overlay_duplicate_name_warns_and_drops(self, field):
        first = {"name": "a", "run": "first", "kind": "lint"}
        second = {"name": "a", "run": "second", "kind": "test"}
        if field == "setup":
            first.pop("kind")
            second.pop("kind")
        parsed = overlay({field: [first, second]})
        steps = getattr(parsed.profile, field)
        assert [s.run for s in steps] == ["first"]
        assert parsed.warnings == (f"{field}[1].name: duplicate step name 'a'; ignored",)

    def test_same_name_allowed_across_setup_and_validate(self):
        profile = central(
            {
                "setup": [{"name": "a", "run": "x"}],
                "validate": [{"name": "a", "kind": "lint", "run": "y"}],
            }
        )
        assert profile.setup[0].name == profile.validate[0].name == "a"

    @pytest.mark.parametrize("run", ["", "   ", None, ["make"]])
    def test_run_must_be_non_empty_string(self, run):
        err = central_error({"setup": [{"name": "a", "run": run}]})
        assert err.path == "setup[0].run"

    @pytest.mark.parametrize("kind", ["lint", "build", "test", "generated"])
    def test_valid_kinds(self, kind):
        profile = central({"validate": [{"name": "a", "kind": kind, "run": "x"}]})
        assert profile.validate[0].kind == kind

    @pytest.mark.parametrize("kind", [None, "", "unit", "LINT", ["lint"]])
    def test_invalid_kinds(self, kind):
        data = {
            "validate": [
                {"name": "a", "kind": "lint", "run": "x"},
                {"name": "b", "kind": "test", "run": "x"},
                {"name": "c", "kind": kind, "run": "x"},
            ]
        }
        err = central_error(data)
        assert err.path == "validate[2].kind"
        assert "lint" in err.rule and "generated" in err.rule

    @pytest.mark.parametrize("field", ["setup", "validate"])
    def test_steps_must_be_mappings_in_a_list(self, field):
        assert central_error({field: "make lint"}).path == field
        assert central_error({field: ["make lint"]}).path == f"{field}[0]"


class TestSkips:
    def test_valid(self):
        assert central({"skips": [{"match": "a", "reason": "b"}]}).skips == (Skip("a", "b"),)

    @pytest.mark.parametrize("key", ["match", "reason"])
    @pytest.mark.parametrize("value", [None, "", "  ", 3])
    def test_fields_must_be_non_empty_strings(self, key, value):
        entry = {"match": "a", "reason": "b", key: value}
        assert central_error({"skips": [entry]}).path == f"skips[0].{key}"

    def test_exact_duplicates_dropped(self):
        profile = central({"skips": [{"match": "a", "reason": "b"}] * 2})
        assert profile.skips == (Skip("a", "b"),)


class TestEnv:
    def test_valid_names(self):
        profile = central({"env": {"CGO_ENABLED": "0", "_x": "y", "GOFLAGS": "-mod=mod"}})
        assert dict(profile.env) == {"CGO_ENABLED": "0", "_x": "y", "GOFLAGS": "-mod=mod"}

    @pytest.mark.parametrize(
        "name",
        [
            "ENVIRONMENT",
            "GITHUB_ORG",
            "LDFLAGS",
            "NODE_ENV",
            "GOPATH",
            "PYTHONPATH",
            "NODE_PATH",
            "CLASSPATH",
            "PATCH_LEVEL",
        ],
    )
    def test_names_near_the_denylist_are_allowed(self, name):
        assert central({"env": {name: "x"}}).env[name] == "x"

    @pytest.mark.parametrize(
        "name",
        [
            "GITHUB_TOKEN",
            "my_secret",
            "API_KEY",
            "DB_PASSWORD",
            "GITLAB_PAT",
            "PAT",
            "pat",
            "GH_PAT_RO",
            "PAT_FILE",
            "CREDENTIALS_FILE",
            "Token",
            "OPENAI_BASE_URL",
            "ANTHROPIC_MODEL",
            "AGENT_MODEL",
            "openai_x",
            "HOME",
            "PATH",
            "home",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_LOG_USER_PROMPTS",
            "otel_x",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GCP_PROJECT",
            "CLOUD_ML_REGION",
            "VERTEX_LOCATION",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "GIT_SSH_COMMAND",
            "GIT_DIR",
            "BASH_ENV",
            "ENV",
            "IFS",
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "NO_PROXY",
            "no_proxy",
            "ALL_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
            "NODE_OPTIONS",
            "PYTHONSTARTUP",
            "PYTHONHOME",
        ],
    )
    def test_denied_names_error_for_central(self, name):
        err = central_error({"env": {name: SECRET_VALUE}})
        assert err.path == f"env.{name}"
        assert SECRET_VALUE not in str(err)

    @pytest.mark.parametrize("name", ["GITHUB_TOKEN", "OPENAI_API_BASE", "HOME", "1BAD", "A-B"])
    def test_rejected_names_warn_and_drop_for_overlay(self, name):
        parsed = overlay({"env": {name: SECRET_VALUE, "GOOD": "1"}})
        assert dict(parsed.profile.env) == {"GOOD": "1"}
        assert len(parsed.warnings) == 1
        assert parsed.warnings[0].endswith("; ignored")
        assert SECRET_VALUE not in parsed.warnings[0]

    @pytest.mark.parametrize("name", ["1BAD", "A-B", "has space", "", "FOO\n", 3])
    def test_invalid_names_error_for_central(self, name):
        err = central_error({"env": {name: "x"}})
        assert "not a valid environment variable name" in err.rule
        assert "\n" not in str(err)

    def test_int_values_converted(self):
        assert central({"env": {"CGO_ENABLED": 0}}).env["CGO_ENABLED"] == "0"

    @pytest.mark.parametrize("value", [True, False, 1.5, None, ["a"], {"a": "b"}])
    def test_other_value_types_rejected_without_echo(self, value):
        err = central_error({"env": {"FLAG": value}})
        assert err.path == "env.FLAG"
        assert repr(value) not in err.rule

    def test_value_type_error_raises_for_overlay_too(self):
        with pytest.raises(SandboxProfileError):
            overlay({"env": {"FLAG": True}})

    def test_env_must_be_a_mapping(self):
        assert central_error({"env": ["A=1"]}).path == "env"


class TestResources:
    @pytest.mark.parametrize("memory", ["1", "512Mi", "8Gi", "1Ti", "64Ki"])
    def test_valid_memory(self, memory):
        assert central({"resources": {"memory": memory}}).resources == Resources(memory=memory)

    @pytest.mark.parametrize(
        "memory", ["0", "0Gi", "8G", "8gi", "-1Gi", "1.5Gi", "", 8, "8Gi\n", "\uff18Gi"]
    )
    def test_invalid_memory(self, memory):
        assert central_error({"resources": {"memory": memory}}).path == "resources.memory"

    def test_null_memory_means_unset(self):
        assert central({"resources": {"memory": None}}).resources is None

    @pytest.mark.parametrize(
        ("cpu", "expected"), [("4", "4"), ("2.5", "2.5"), ("500m", "500m"), (4, "4"), (2.5, "2.5")]
    )
    def test_valid_cpu(self, cpu, expected):
        assert central({"resources": {"cpu": cpu}}).resources == Resources(cpu=expected)

    @pytest.mark.parametrize(
        "cpu", ["0", "0m", 0, -1, 0.0, "1.5m", "four", True, "", [4], "4\n", "500m\n", "\u0664"]
    )
    def test_invalid_cpu(self, cpu):
        assert central_error({"resources": {"cpu": cpu}}).path == "resources.cpu"

    @pytest.mark.parametrize("gpu", [0, 1, 8])
    def test_valid_gpu(self, gpu):
        assert central({"resources": {"gpu": gpu}}).resources == Resources(gpu=gpu)

    @pytest.mark.parametrize("gpu", [-1, 1.0, "1", True])
    def test_invalid_gpu(self, gpu):
        assert central_error({"resources": {"gpu": gpu}}).path == "resources.gpu"

    def test_empty_resources_is_none(self):
        assert central({"resources": {}}).resources is None

    def test_overlay_resources_ignored_with_warning(self):
        parsed = overlay({"resources": {"memory": "64Gi", "gpu": 8}})
        assert parsed.profile.resources is None
        assert parsed.warnings[0].startswith("resources: set only by the central profile")

    def test_overlay_resources_ignored_even_when_invalid(self):
        parsed = overlay({"resources": "lots"})
        assert parsed.profile.resources is None
        assert len(parsed.warnings) == 1


class TestDiscardBeforeDownload:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("node_modules", "node_modules"),
            ("./node_modules/", "node_modules"),
            ("web//dist", "web/dist"),
            ("a/./b", "a/b"),
            (".cache", ".cache"),
            (".github/tmp", ".github/tmp"),
        ],
    )
    def test_normalized(self, path, expected):
        profile = central({"discard_before_download": [path]})
        assert profile.discard_before_download == (expected,)

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/etc",
            "/sandbox/work",
            "..",
            "../x",
            "a/../b",
            "a/..",
            ".",
            "./",
            ".git",
            ".git/hooks",
            "./.git/config",
            "sub/.git",
            None,
            3,
            "a\0b",
        ],
    )
    def test_rejected(self, path):
        err = central_error({"discard_before_download": ["ok", path]})
        assert err.path == "discard_before_download[1]"

    def test_duplicates_after_normalization_dropped(self):
        profile = central({"discard_before_download": ["dist", "./dist/", "dist"]})
        assert profile.discard_before_download == ("dist",)


class TestOverlayKey:
    @pytest.mark.parametrize("mode", ["merge", "ignore"])
    def test_central_values(self, mode):
        assert central({"overlay": mode}).overlay == mode

    @pytest.mark.parametrize("mode", ["off", "", True, "IGNORE"])
    def test_central_invalid(self, mode):
        assert central_error({"overlay": mode}).path == "overlay"

    def test_overlay_setting_it_is_ignored_with_warning(self):
        parsed = overlay({"overlay": "ignore"})
        assert parsed.profile.overlay == "merge"
        assert parsed.warnings == ("overlay: set only by the central profile; ignored",)


class TestLimits:
    def test_long_lists_are_rejected(self):
        err = central_error({"discard_before_download": ["a"] * (MAX_ENTRIES + 1)})
        assert err.path == "discard_before_download"
        with pytest.raises(SandboxProfileError):
            overlay({"skips": [{"match": "a", "reason": "b"}] * (MAX_ENTRIES + 1)})

    def test_large_mappings_are_rejected(self):
        env = {f"V{i}": "x" for i in range(MAX_ENTRIES + 1)}
        assert central_error({"env": env}).path == "env"
        top = {f"k{i}": 1 for i in range(MAX_ENTRIES + 1)}
        with pytest.raises(SandboxProfileError):
            overlay(top)

    def test_de_duplication_keeps_order(self):
        paths = [f"d{i % 500}" for i in range(MAX_ENTRIES)]
        profile = _overlay_profile({"discard_before_download": paths})
        assert profile.discard_before_download == tuple(f"d{i}" for i in range(500))

    def test_warnings_are_capped(self):
        parsed = overlay({f"k{i}": 1 for i in range(MAX_WARNINGS + 30)})
        assert len(parsed.warnings) == MAX_WARNINGS + 1
        assert parsed.warnings[-1] == "30 more warnings not shown"


class TestErrorMessagesDoNotLeak:
    def test_run_string_never_in_errors(self):
        data = {"setup": [{"name": "a", "run": SECRET_RUN, "timeout": 0}]}
        err = central_error(data)
        assert SECRET_RUN not in str(err)
        assert "sekrit" not in str(err)

    def test_run_string_never_in_validate_kind_error(self):
        err = central_error({"validate": [{"name": "a", "kind": "x", "run": SECRET_RUN}]})
        assert "sekrit" not in str(err)

    def test_env_values_never_in_errors_or_warnings(self):
        err = central_error({"env": {"OK": "fine", "MY_TOKEN": SECRET_VALUE}})
        assert SECRET_VALUE not in str(err)
        parsed = overlay({"env": {"MY_TOKEN": SECRET_VALUE, "HOME": SECRET_VALUE}})
        assert all(SECRET_VALUE not in w for w in parsed.warnings)

    def test_long_names_are_truncated(self):
        err = central_error({"toolchains": {"x" * 500: "1"}})
        assert len(str(err)) < 300


def _overlay_profile(data):
    parsed = overlay(data)
    return parsed.profile


class TestMerge:
    def test_both_none(self):
        assert merge_profiles(None, None) is None

    def test_central_only(self):
        profile = central(FULL_CENTRAL)
        assert merge_profiles(profile, None) == MergedProfile(profile=profile, warnings=())

    def test_overlay_ignore_drops_overlay(self):
        base = central({**FULL_CENTRAL, "overlay": "ignore"})
        extra = _overlay_profile({"validate": [{"name": "x", "kind": "test", "run": "y"}]})
        merged = merge_profiles(base, extra)
        assert merged.profile == base
        assert len(merged.warnings) == 1
        assert "overlay: ignore" in merged.warnings[0]

    def test_overlay_ignore_without_overlay_has_no_warning(self):
        base = central({"overlay": "ignore"})
        assert merge_profiles(base, None).warnings == ()

    def test_central_map_keys_win(self):
        base = central({"toolchains": {"go": "1.22"}, "env": {"A": "central"}})
        extra = _overlay_profile(
            {"toolchains": {"go": "auto", "node": "22"}, "env": {"A": "repo", "B": "repo"}}
        )
        merged = merge_profiles(base, extra)
        assert dict(merged.profile.toolchains) == {"go": "1.22", "node": "22"}
        assert dict(merged.profile.env) == {"A": "central", "B": "repo"}
        assert "toolchains.go: the central profile's value wins" in merged.warnings
        assert "env.A: the central profile's value wins" in merged.warnings
        assert all("repo" not in w for w in merged.warnings)

    def test_same_map_value_is_silent(self):
        base = central({"toolchains": {"go": "auto"}})
        merged = merge_profiles(base, _overlay_profile({"toolchains": {"go": "auto"}}))
        assert merged.warnings == ()

    def test_lists_concatenate_central_first_and_dedup(self):
        base = central(
            {
                "egress": ["goproxy", "npm"],
                "skips": [{"match": "unshare", "reason": "central"}],
                "discard_before_download": ["node_modules"],
            }
        )
        extra = _overlay_profile(
            {
                "egress": ["pypi", "npm"],
                "skips": [
                    {"match": "unshare", "reason": "repo"},
                    {"match": "kind", "reason": "no docker"},
                ],
                "discard_before_download": ["./node_modules", "dist"],
            }
        )
        merged = merge_profiles(base, extra)
        assert merged.profile.egress == ("goproxy", "npm", "pypi")
        assert merged.profile.skips == (Skip("unshare", "central"), Skip("kind", "no docker"))
        assert merged.profile.discard_before_download == ("node_modules", "dist")
        assert merged.warnings == (
            "skips.unshare: the central profile defines this match; overlay skip dropped",
        )

    def test_identical_overlay_skip_is_silent(self):
        base = central({"skips": [{"match": "unshare --net", "reason": "r"}]})
        extra = _overlay_profile({"skips": [{"match": "unshare --net", "reason": "r"}]})
        merged = merge_profiles(base, extra)
        assert merged.profile.skips == base.skips
        assert merged.warnings == ()

    def test_colliding_skip_match_is_quoted(self):
        base = central({"skips": [{"match": "unshare --net", "reason": "central"}]})
        extra = _overlay_profile({"skips": [{"match": "unshare --net", "reason": "repo"}]})
        assert merge_profiles(base, extra).warnings == (
            "skips.'unshare --net': the central profile defines this match; overlay skip dropped",
        )

    @pytest.mark.parametrize(
        "match",
        ["unit", "make test", "test", "TEST", "Make Test", "unit tests", "UNIT", "make test -v"],
    )
    def test_overlay_skip_cannot_hide_central_validation(self, match):
        base = central({"validate": [{"name": "unit", "kind": "test", "run": "make test"}]})
        extra = _overlay_profile(
            {"skips": [{"match": match, "reason": "r"}, {"match": "cypress", "reason": "r"}]}
        )
        merged = merge_profiles(base, extra)
        assert merged.profile.skips == (Skip("cypress", "r"),)
        assert len(merged.warnings) == 1
        assert "matches a validate step the central profile requires" in merged.warnings[0]

    @pytest.mark.parametrize("match", ["lint", "e2e", "make e2e"])
    def test_unrelated_overlay_skip_is_kept(self, match):
        base = central({"validate": [{"name": "unit", "kind": "test", "run": "make test"}]})
        merged = merge_profiles(
            base, _overlay_profile({"skips": [{"match": match, "reason": "r"}]})
        )
        assert merged.profile.skips == (Skip(match, "r"),)
        assert merged.warnings == ()

    def test_overlay_skip_may_cover_its_own_validation(self):
        extra = _overlay_profile(
            {
                "validate": [{"name": "e2e", "kind": "test", "run": "make e2e"}],
                "skips": [{"match": "e2e", "reason": "needs a cluster"}],
            }
        )
        merged = merge_profiles(central({}), extra)
        assert merged.profile.skips == (Skip("e2e", "needs a cluster"),)
        assert merged.warnings == ()

    @pytest.mark.parametrize("field", ["setup", "validate"])
    def test_steps_dedup_by_name_central_wins(self, field):
        def step(name, run):
            data = {"name": name, "run": run}
            if field == "validate":
                data["kind"] = "test"
            return data

        base = central({field: [step("unit", "make test")]})
        extra = _overlay_profile({field: [step("unit", SECRET_RUN), step("e2e", "make e2e")]})
        merged = merge_profiles(base, extra)
        steps = getattr(merged.profile, field)
        assert [s.name for s in steps] == ["unit", "e2e"]
        assert steps[0].run == "make test"
        assert merged.warnings == (
            f"{field}.unit: the central profile defines a step with this name; "
            "overlay step dropped",
        )
        assert all("sekrit" not in w for w in merged.warnings)

    def test_overlay_disallowed_preset_dropped(self):
        base = central({"egress": ["github-release-assets"]})
        extra = _overlay_profile({"egress": ["github-release-assets", "npm"]})
        merged = merge_profiles(base, extra)
        # Central already has it, but the overlay still may not request it.
        assert merged.profile.egress == ("github-release-assets", "npm")
        assert merged.warnings == (
            "egress: preset 'github-release-assets' is not allowed in a repo overlay; dropped",
        )

    def test_custom_allowed_presets(self):
        extra = _overlay_profile({"egress": ["github-release-assets", "npm"]})
        merged = merge_profiles(
            None, extra, overlay_allowed_presets={"github-release-assets", "npm"}
        )
        assert merged.profile.egress == ("github-release-assets", "npm")
        assert merged.warnings == ()

    def test_default_allowed_presets(self):
        assert DEFAULT_OVERLAY_ALLOWED_PRESETS == {"pypi", "npm", "goproxy"}

    def test_central_resources_and_raw_egress_kept(self):
        base = central(FULL_CENTRAL)
        merged = merge_profiles(base, _overlay_profile({"egress": ["pypi"]})).profile
        assert merged.resources == Resources(memory="8Gi", cpu="4")
        assert merged.raw_egress == ("example.com:443:read-only",)
        assert merged.overlay == "merge"

    def test_overlay_without_central_gets_default_envelope(self):
        parsed = overlay(
            {
                "toolchains": {"go": "auto"},
                "egress": ["goproxy", "github-release-assets", "repo.example.com:443:full"],
                "validate": [{"name": "lint", "kind": "lint", "run": "make lint"}],
                "resources": {"memory": "64Gi"},
                "overlay": "ignore",
            }
        )
        assert len(parsed.warnings) == 3  # raw host, resources, overlay key
        merged = merge_profiles(None, parsed.profile)
        profile = merged.profile
        assert profile.overlay == "merge"
        assert profile.egress == ("goproxy",)
        assert profile.raw_egress == ()
        assert profile.resources is None
        assert dict(profile.toolchains) == {"go": "auto"}
        assert [v.name for v in profile.validate] == ["lint"]
        assert len(merged.warnings) == 1
        assert "github-release-assets" in merged.warnings[0]

    def test_constructed_overlay_cannot_carry_central_only_fields(self):
        """merge_profiles enforces the envelope even for an overlay not built by parse_profile."""
        rogue = SandboxProfile(
            raw_egress=("evil.example.com:443:full",),
            resources=Resources(memory="64Gi"),
            overlay="ignore",
        )
        merged = merge_profiles(None, rogue)
        assert merged.profile == SandboxProfile()
        assert len(merged.warnings) == 2

    def test_constructed_overlay_env_and_toolchains_are_checked(self):
        rogue = SandboxProfile(
            env={
                "OPENAI_API_KEY": SECRET_VALUE,
                "GH_TOKEN": SECRET_VALUE,
                "LD_PRELOAD": "/tmp/x.so",
                "BAD\nNAME": "x",
                "GOOD": "1",
            },
            toolchains={"evil": "1", "go": "1\n", "node": "22"},
        )
        merged = merge_profiles(None, rogue)
        assert dict(merged.profile.env) == {"GOOD": "1"}
        assert dict(merged.profile.toolchains) == {"node": "22"}
        assert len(merged.warnings) == 6
        assert all(w.endswith("; ignored") for w in merged.warnings)
        assert all(SECRET_VALUE not in w and "\n" not in w for w in merged.warnings)
        assert "toolchains.evil: unknown toolchain" in " ".join(merged.warnings)

    def test_constructed_overlay_discard_paths_are_checked(self):
        rogue = SandboxProfile(
            discard_before_download=("../..", ".git", "/etc", "sub/.git/hooks", ".", "a/./b")
        )
        merged = merge_profiles(None, rogue)
        assert merged.profile.discard_before_download == ("a/b",)
        assert len(merged.warnings) == 5
        assert all(w.endswith("; ignored") for w in merged.warnings)
        assert merged.warnings[0].startswith("discard_before_download[0]: ")

    def test_merge_accepts_every_parsed_overlay(self):
        extra = _overlay_profile(
            {
                "toolchains": dict.fromkeys(KNOWN_TOOLCHAINS, "auto"),
                "egress": sorted(KNOWN_EGRESS_PRESETS),
                "setup": [{"name": "a", "run": "x"}],
                "validate": [{"name": "a", "kind": "lint", "run": "x"}],
                "skips": [{"match": "a", "reason": "b"}],
                "env": {"X": "1", "SOME_TOKEN": "t"},
                "discard_before_download": ["dist"],
                "overlay": "ignore",
                "resources": {"cpu": 1},
                "bogus": 1,
            }
        )
        merged = merge_profiles(central(FULL_CENTRAL), extra)
        assert isinstance(merged, MergedProfile)


class TestSerialization:
    def test_round_trip_full(self):
        profile = central(FULL_CENTRAL)
        assert central(profile_to_dict(profile)) == profile

    def test_round_trip_defaults(self):
        assert central(profile_to_dict(SandboxProfile())) == SandboxProfile()

    def test_round_trip_through_json_and_yaml(self):
        profile = central(
            {**FULL_CENTRAL, "resources": {"cpu": 2.5, "gpu": 1}, "overlay": "ignore"}
        )
        assert central(yaml.safe_load(yaml.safe_dump(profile_to_dict(profile)))) == profile

    def test_to_dict_shape(self):
        data = profile_to_dict(central(FULL_CENTRAL))
        assert data["egress"] == ["goproxy", "npm", "example.com:443:read-only"]
        assert data["resources"] == {"memory": "8Gi", "cpu": "4"}
        assert data["validate"][1] == {
            "name": "unit",
            "kind": "test",
            "run": "make test",
            "timeout": 600,
        }
        assert "resources" not in profile_to_dict(SandboxProfile())

    def test_hash_is_stable(self):
        # A changed value means every existing sandbox identity changes too.
        assert (
            profile_hash(central(FULL_CENTRAL))
            == "ef94f29e4b8b504d91774b77c83caba6c1150e652ca3a8c2882222f0a46b51cb"
        )

    def test_hash_ignores_map_insertion_order(self):
        a = central({"env": {"A": "1", "B": "2"}, "toolchains": {"go": "auto", "node": "22"}})
        b = central({"env": {"B": "2", "A": "1"}, "toolchains": {"node": "22", "go": "auto"}})
        assert profile_hash(a) == profile_hash(b)

    def test_hash_changes_with_content(self):
        a = central(FULL_CENTRAL)
        b = central({**FULL_CENTRAL, "env": {"CGO_ENABLED": "1"}})
        assert profile_hash(a) != profile_hash(b)
        assert len(profile_hash(a)) == 64
