"""Tests for the secret redactor used on sandbox step output."""

import time

import pytest

from agentic_ci.redact import REDACTED, is_secret_name, redact, secret_values


class TestRedact:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Authorization: Bearer abcdefghijkl", f"Authorization: {REDACTED}"),
            ("authorization=token-value here", f"authorization={REDACTED}"),
            ("Proxy-Authorization: Basic dXNlcjpwYXNz", f"Proxy-Authorization: {REDACTED}"),
            ("> Cookie: session=abc; other=def", f"> Cookie: {REDACTED}"),
            ("x-api-key: sk-short", f"x-api-key: {REDACTED}"),
            ("curl -H 'bearer abcdefghijklmnop'", f"curl -H 'bearer {REDACTED}'"),
        ],
    )
    def test_headers_and_bearer_credentials(self, text, expected):
        assert redact(text) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:pass@example.com/x",
            "https://x-access-token:ghs_aaaaaaaaaaaaaaaaaaaaaa@github.com/o/r.git",
            "https://oauth2:@gitlab.com/g/p.git",
            "git+https://abcdefghijklmnopqrstuvwx@github.com/o/r",
        ],
    )
    def test_url_user_information(self, url):
        out = redact(f"cloning {url} now")
        assert "@" in out and REDACTED in out
        assert "pass" not in out and "ghs_" not in out and "abcdefghijklmnop" not in out

    def test_a_url_without_credentials_is_kept(self):
        text = "GET https://registry.npmjs.org/left-pad 200 and git@github.com:o/r.git"
        assert redact(text) == text

    @pytest.mark.parametrize(
        "token",
        [
            "sk-proj-abcdefghijklmnopqrstuvwx",
            "sk-ant-api03-abcdefghijklmnopqrstuvwx",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "ghp_" + "a" * 36,
            "github_pat_" + "b" * 40,
            "glpat-" + "c" * 20,
            "npm_" + "d" * 36,
            "xoxb-1234567890-abcdef",
            "AKIAABCDEFGHIJKLMNOP",
            "AIza" + "e" * 35,
            "ya29." + "f" * 40,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
            "openshell:resolve:env:OPENAI_API_KEY",
        ],
    )
    def test_well_known_token_shapes(self, token):
        out = redact(f"value {token} end")
        assert token not in out
        assert out == f"value {REDACTED} end"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("BOT_PAT=abc", f"BOT_PAT={REDACTED}"),
            ("export JIRA_API_TOKEN='a b c'", f"export JIRA_API_TOKEN={REDACTED}"),
            ('DB_PASSWORD="hunter two"', f"DB_PASSWORD={REDACTED}"),
            ("--auth-token=xyz", f"--auth-token={REDACTED}"),
            ('{"access_token": "abc123"}', f'{{"access_token": "{REDACTED}"}}'),
            ("{'client_secret': 'abc'}", f"{{'client_secret': '{REDACTED}'}}"),
        ],
    )
    def test_values_of_credential_like_names(self, text, expected):
        assert redact(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "GOPATH=/sandbox/.local/gopath PATH=/usr/bin:/bin",
            "PYTHONPATH=/x NODE_PATH=/y",
            "ok  example.com/m  0.012s",
            "npm warn deprecated left-pad@1.3.0",
            "disk-usage-statistics-report task-abcdefghijklmnopqrstuv",
        ],
    )
    def test_ordinary_output_is_kept(self, text):
        assert redact(text) == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Dotted and dashed names, from where the run of name characters starts.
            ("a.b.c.token=xyz", f"a.b.c.token={REDACTED}"),
            ("spring.datasource.password = x", f"spring.datasource.password = {REDACTED}"),
            ("url?access_token=abc&x=1", f"url?access_token={REDACTED}"),
            ("$GH_TOKEN=abc", f"$GH_TOKEN={REDACTED}"),
        ],
    )
    def test_names_inside_longer_text(self, text, expected):
        assert redact(text) == expected

    @pytest.mark.parametrize(
        "unit",
        ["a.b.", "a-a-", "token.", "a_pat_", "eyJa-", "x://", "a://" + "b" * 300, '"a":', "sk-"],
    )
    def test_a_long_line_takes_linear_time(self, unit):
        # 256 KiB, the most a step's tail keeps: every pattern either starts
        # only where a run begins or scans a bounded length from each start.
        line = unit * ((256 << 10) // len(unit))
        start = time.monotonic()
        redact(line)
        assert time.monotonic() - start < 10

    def test_host_secrets_are_replaced_anywhere(self):
        secret = "s3cr3t-value-without-shape"
        assert redact(f"a{secret}b and {secret}", [secret]) == f"a{REDACTED}b and {REDACTED}"

    def test_the_longest_secret_goes_first(self):
        assert redact("abcdefghij-XYZ", ["abcdefghij", "abcdefghij-XYZ"]) == REDACTED

    def test_short_secrets_are_ignored(self):
        assert redact("value true", ["true", 1234]) == "value true"


class TestSecretValues:
    def test_only_credential_like_names_long_enough(self):
        env = {
            "BOT_PAT": "glpat-host-value-1",
            "JIRA_API_TOKEN": "  jira-token-value\n",
            "OPENAI_API_KEY": "sk-host-openai-key",
            "GH_PAT": "short",
            "HOME": "/home/runner-long-path",
            "GOPATH": "/home/runner/go-long",
            "SESSION_ID": "sess-12345678",
        }
        assert set(secret_values(env)) == {
            "glpat-host-value-1",
            "jira-token-value",
            "sk-host-openai-key",
            "sess-12345678",
        }

    def test_longest_first(self):
        values = secret_values({"A_TOKEN": "a" * 10, "B_TOKEN": "b" * 20})
        assert values == ("b" * 20, "a" * 10)

    @pytest.mark.parametrize(
        ("name", "secret"),
        [
            ("BOT_PAT", True),
            ("PAT", True),
            ("GH_PAT_2", True),
            ("GOPATH", False),
            ("PATH", False),
            ("NODE_PATH", False),
            ("ANTHROPIC_API_KEY", True),
            ("CLAUDE_CODE_OAUTH_TOKEN", True),
            ("GITHUB_APP_PRIVATE_KEY", True),
            ("AWS_ACCESS_KEY_ID", True),
            ("CI_JOB_ID", False),
        ],
    )
    def test_is_secret_name(self, name, secret):
        assert is_secret_name(name) is secret
