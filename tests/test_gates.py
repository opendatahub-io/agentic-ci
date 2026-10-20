"""Tests for pre/post agent gates."""

import logging
import re

from agentic_ci import gates
from agentic_ci.gates import (
    check_commit_identity,
    check_description_editors,
    check_external_reporter,
    check_label_author_email,
    check_sensitive_files,
    filter_bot_comments,
    filter_comments_by_domain,
    log_changed_files,
)

REDHAT_RE = re.compile(r"@redhat\.com$", re.IGNORECASE)


class TestCheckSensitiveFiles:
    def test_no_matches(self):
        assert check_sensitive_files(["src/main.py", "README.md"]) == []

    def test_env_file_blocked(self):
        result = check_sensitive_files(["src/.env", "src/main.py"])
        assert result == ["src/.env"]

    def test_pem_blocked(self):
        result = check_sensitive_files(["certs/server.pem"])
        assert result == ["certs/server.pem"]

    def test_custom_blocklist(self):
        result = check_sensitive_files(["config.yaml"], blocklist=["*.yaml"])
        assert result == ["config.yaml"]

    def test_secret_in_name(self):
        result = check_sensitive_files(["my-secret-file.txt"])
        assert result == ["my-secret-file.txt"]

    def test_secret_directory_not_blocked(self):
        files = [
            "src/app/secrets/SecretKeyValuePairInput.tsx",
            "src/app/secrets/SecretsAttachModal.tsx",
        ]
        assert check_sensitive_files(files) == []

    def test_secret_in_filename_still_blocked(self):
        result = check_sensitive_files(["config/secret.yaml"])
        assert result == ["config/secret.yaml"]

    def test_directory_pattern_matches_full_path(self):
        files = [".agentic-ci/config.yml", ".agentic-ci/sub/policy.yml", "src/.agentic-ci-x"]
        assert check_sensitive_files(files, [".agentic-ci/*"]) == [
            ".agentic-ci/config.yml",
            ".agentic-ci/sub/policy.yml",
        ]

    def test_directory_pattern_is_anchored_at_repo_root(self):
        assert check_sensitive_files(["src/.agentic-ci/config.yml"], [".agentic-ci/*"]) == []

    def test_quoted_path_is_unquoted_and_returned_as_given(self):
        quoted = '".agentic-ci/caf\\303\\251.yml"'
        assert check_sensitive_files([quoted], [".agentic-ci/*"]) == [quoted]
        assert check_sensitive_files(['"certs/caf\\303\\251.pem"']) == ['"certs/caf\\303\\251.pem"']

    def test_matching_is_case_sensitive(self):
        assert check_sensitive_files(["certs/server.PEM"]) == []

    def test_escape_cannot_hide_a_sensitive_name(self):
        """git never writes \\057 for "/", so the name is also matched as given."""
        name = '"d/secret\\057x"'
        assert check_sensitive_files([name], ["*secret*"]) == [name]

    def test_gate_message_escapes_control_characters(self, monkeypatch):
        monkeypatch.setattr(gates, "get_changed_files", lambda *a, **kw: ["certs/a\nb.pem"])

        (message,) = gates._run_sensitive_files("/repo")

        assert message == "Sensitive files modified: certs/a\\nb.pem"


class TestCheckCommitAuthor:
    def test_match(self):
        assert check_commit_identity({"email": "bot@ci.com"}, "bot@ci.com") is True

    def test_case_insensitive(self):
        assert check_commit_identity({"email": "Bot@CI.com"}, "bot@ci.com") is True

    def test_mismatch(self):
        assert check_commit_identity({"email": "human@ci.com"}, "bot@ci.com") is False


class TestFilterCommentsByDomain:
    def test_filters_non_matching(self):
        comments = [
            {"author_email": "alice@redhat.com", "body": "ok"},
            {"author_email": "bob@external.com", "body": "bad"},
        ]
        result = filter_comments_by_domain(comments, REDHAT_RE)
        assert len(result) == 1
        assert result[0]["body"] == "ok"

    def test_missing_or_non_string_email_is_not_in_domain(self):
        comments = [
            {"author_email": None, "body": "app"},
            {"body": "no email key"},
            {"author_email": 42, "body": "not a string"},
            {"author_email": "alice@redhat.com", "body": "ok"},
        ]
        result = filter_comments_by_domain(comments, REDHAT_RE)
        assert [c["body"] for c in result] == ["ok"]


class TestFilterBotComments:
    def test_removes_sentinel(self):
        comments = [
            {"body": "Human wrote this"},
            {"body": "AUTO: jira-autofix-bot generated this"},
        ]
        result = filter_bot_comments(comments, ["jira-autofix-bot"])
        assert len(result) == 1
        assert result[0]["body"] == "Human wrote this"

    def test_missing_or_non_string_body_is_kept(self):
        comments = [
            {"body": None, "author_email": "a@redhat.com"},
            {"author_email": "b@redhat.com"},
            {"body": "AUTO: jira-autofix-bot generated this"},
        ]
        result = filter_bot_comments(comments, ["jira-autofix-bot"])
        assert [c["author_email"] for c in result] == ["a@redhat.com", "b@redhat.com"]


class TestCheckExternalReporter:
    def test_internal_reporter(self):
        ticket = {"reporter_email": "dev@redhat.com", "labels": []}
        assert check_external_reporter(ticket, REDHAT_RE, external_label="triage-external") is None

    def test_external_reporter(self):
        ticket = {"reporter_email": "user@gmail.com", "labels": []}
        result = check_external_reporter(ticket, REDHAT_RE, external_label="triage-external")
        assert result == "triage-external"

    def test_already_labeled(self):
        ticket = {"reporter_email": "user@gmail.com", "labels": ["triage-external"]}
        assert check_external_reporter(ticket, REDHAT_RE, external_label="triage-external") is None

    def test_none_reporter_email_is_external(self):
        ticket = {"reporter_email": None, "labels": []}
        assert check_external_reporter(ticket, REDHAT_RE, external_label="ext") == "ext"


class TestCheckDescriptionEditors:
    def test_all_internal(self):
        result = check_description_editors(["dev2@redhat.com"], "dev1@redhat.com", REDHAT_RE)
        assert result == []

    def test_external_editor(self):
        result = check_description_editors(["attacker@gmail.com"], "dev@redhat.com", REDHAT_RE)
        assert result == ["attacker@gmail.com"]

    def test_external_reporter(self):
        result = check_description_editors([], "user@external.com", REDHAT_RE)
        assert result == ["user@external.com"]

    def test_empty_reporter_email(self):
        result = check_description_editors([], "", REDHAT_RE)
        assert result == ["missing-email:reporter"]

    def test_missing_editor_email(self):
        result = check_description_editors(["missing-email:abc123"], "dev@redhat.com", REDHAT_RE)
        assert result == ["missing-email:abc123"]

    def test_mixed_editors(self):
        result = check_description_editors(
            ["dev2@redhat.com", "attacker@evil.com", "dev3@redhat.com"],
            "dev1@redhat.com",
            REDHAT_RE,
        )
        assert result == ["attacker@evil.com"]

    def test_no_editors_internal_reporter(self):
        result = check_description_editors([], "dev@redhat.com", REDHAT_RE)
        assert result == []

    def test_deduplicates(self):
        result = check_description_editors(
            ["bad@evil.com", "bad@evil.com"], "bad@evil.com", REDHAT_RE
        )
        assert result == ["bad@evil.com"]

    def test_empty_email_normalized_and_deduplicated(self):
        result = check_description_editors(["", ""], "dev@redhat.com", REDHAT_RE)
        assert result == ["missing-email:unknown"]


class TestLogChangedFiles:
    def test_logs_files(self, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            log_changed_files(["a.py", "b.py"], "TEST-1")
        assert "2 files" in caplog.text

    def test_logs_no_files(self, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            log_changed_files([], "TEST-1")
        assert "No files changed" in caplog.text

    def test_names_cannot_forge_log_lines(self, caplog):
        with caplog.at_level(logging.INFO):
            log_changed_files(["a.py\n[TEST-1] Agent changed 0 files"], "TEST-1")
        (record,) = caplog.records
        assert "\n" not in record.getMessage()
        assert "a.py\\n[TEST-1]" in record.getMessage()


class TestCheckLabelAuthorEmail:
    def test_author_found_email_matches(self):
        author_info = {"found": True, "email": "dev@redhat.com"}
        assert check_label_author_email(author_info, REDHAT_RE) is True

    def test_author_not_found(self):
        author_info = {"found": False, "email": ""}
        assert check_label_author_email(author_info, REDHAT_RE) is False

    def test_email_does_not_match(self):
        author_info = {"found": True, "email": "user@gmail.com"}
        assert check_label_author_email(author_info, REDHAT_RE) is False

    def test_empty_email(self):
        author_info = {"found": True, "email": ""}
        assert check_label_author_email(author_info, REDHAT_RE) is False

    def test_missing_found_key(self):
        author_info = {"email": "dev@redhat.com"}
        assert check_label_author_email(author_info, REDHAT_RE) is False

    def test_none_email(self):
        author_info = {"found": True, "email": None}
        assert check_label_author_email(author_info, REDHAT_RE) is False
