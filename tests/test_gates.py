"""Tests for pre/post agent gates."""

import re

from agentic_ci.gates import (
    check_commit_author,
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


class TestCheckCommitAuthor:
    def test_match(self):
        assert check_commit_author({"email": "bot@ci.com"}, "bot@ci.com") is True

    def test_case_insensitive(self):
        assert check_commit_author({"email": "Bot@CI.com"}, "bot@ci.com") is True

    def test_mismatch(self):
        assert check_commit_author({"email": "human@ci.com"}, "bot@ci.com") is False


class TestFilterCommentsByDomain:
    def test_filters_non_matching(self):
        comments = [
            {"author_email": "alice@redhat.com", "body": "ok"},
            {"author_email": "bob@external.com", "body": "bad"},
        ]
        result = filter_comments_by_domain(comments, REDHAT_RE)
        assert len(result) == 1
        assert result[0]["body"] == "ok"


class TestFilterBotComments:
    def test_removes_sentinel(self):
        comments = [
            {"body": "Human wrote this"},
            {"body": "AUTO: jira-autofix-bot generated this"},
        ]
        result = filter_bot_comments(comments, ["jira-autofix-bot"])
        assert len(result) == 1
        assert result[0]["body"] == "Human wrote this"


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
