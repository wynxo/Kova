"""Credential detection and redaction: known shapes, false positives and bounds.

Every value here is a made-up fixture. None of them is a valid credential, and
the tests never call a provider.
"""

from __future__ import annotations

import re
import time

import pytest

from kova.secrets import (
    MAX_SCAN_CHARS,
    Redaction,
    Scan,
    contains_secret,
    detect,
    redact,
)

# Fake values built from repeated characters; no fixture is a usable key.
OPENAI = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
ANTHROPIC = "sk-ant-api03-" + "FakeAnthropicKeyMaterial12345"
GITHUB = "ghp_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Y"
GITHUB_PAT = "github_pat_" + "FakeFineGrainedToken1234567890"
SLACK = "xoxb-" + "1234567890-fakeSlackToken"
GOOGLE = "AIza" + "FakeGoogleApiKey1234567890123456789"
AWS = "AKIA" + "FAKEAWSACCESSKEY"
JWT = ("eyJhbGciOiJIUzI1NiJ9."
       "eyJzdWIiOiJmYWtlLXVzZXItaW4tYS10ZXN0In0."
       "FakeSignatureMaterial1234567890")
BEARER = "FakeBearerTokenMaterial1234567890"
STRIPE = "sk_live_" + "FakeStripeKeyMaterial1234"
SENDGRID = "SG." + "FakeSendGridKeyPart12" + "." + "FakeSendGridKeyPart34"
NPM = "npm_" + "FakeNpmTokenMaterial1234567890123XYZ"
GITLAB = "glpat-" + "FakeGitLabTokenMaterial123"
HUGGINGFACE = "hf_" + "FakeHuggingFaceTokenMaterial12345"
PEM_LABELS = ["RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY",
              "PGP PRIVATE KEY BLOCK"]

TOKENS = [
    ("openai_key", OPENAI),
    ("anthropic_key", ANTHROPIC),
    ("github_token", GITHUB),
    ("github_token", GITHUB_PAT),
    ("slack_token", SLACK),
    ("google_api_key", GOOGLE),
    ("aws_access_key", AWS),
    ("jwt", JWT),
    ("stripe_key", STRIPE),
    ("sendgrid_key", SENDGRID),
    ("npm_token", NPM),
    ("gitlab_token", GITLAB),
    ("huggingface_token", HUGGINGFACE),
]

URLS = [
    "postgres://fake_user:fake-pass-123@db.example.test:5432/app",
    "mongodb+srv://fake:fake-pass-123@cluster.example.test/app",
    "https://fake_user:fake-pass-123@example.test/resource",
]

ENV_LINES = [
    ("OPENAI_API_KEY=fake-openai-key-value", "OPENAI_API_KEY="),
    ("export DATABASE_PASSWORD='fake-db-password'", "export DATABASE_PASSWORD="),
    ("AWS_SECRET_ACCESS_KEY=fakeAwsSecretAccessKey", "AWS_SECRET_ACCESS_KEY="),
    ('GITHUB_TOKEN="fake-github-token"', "GITHUB_TOKEN="),
    ("MYSQL_ROOT_PASSWORD=fake-root-password", "MYSQL_ROOT_PASSWORD="),
    ("API_KEY=fake-api-key-value", "API_KEY="),
]


@pytest.mark.parametrize(("kind", "token"), TOKENS)
def test_known_provider_tokens_are_found(kind, token):
    scan = detect(f"the key is {token} in this sentence")
    assert scan.found
    assert scan.kinds[0] == kind


def test_bearer_token_redacts_only_the_token():
    scan = detect(f"Authorization: Bearer {BEARER}")
    assert scan.kinds == ("bearer_token",)
    finding = scan.findings[0]
    result = redact(f"Authorization: Bearer {BEARER}")
    assert result.text == f"Authorization: Bearer {finding.placeholder}"


@pytest.mark.parametrize("label", PEM_LABELS)
def test_private_key_block_is_redacted_as_one_span(label):
    pem = f"-----BEGIN {label}-----\nFAKEKEYMATERIAL\n-----END {label}-----"
    scan = detect(pem)
    assert scan.kinds == ("private_key",)
    finding = scan.findings[0]
    assert (finding.start, finding.end) == (0, len(pem))
    result = redact(pem)
    assert "FAKEKEYMATERIAL" not in result.text
    assert result.text == finding.placeholder


def test_a_truncated_private_key_header_redacts_the_material_after_it():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEYMATERIAL"
    scan = detect(text)
    assert scan.kinds == ("private_key",)
    # There is no END line to prove the body is not key material, so the whole
    # remainder is treated as part of the key.
    assert scan.findings[0].end == len(text)
    result = redact(text)
    assert "BEGIN OPENSSH PRIVATE KEY" not in result.text
    assert "FAKEKEYMATERIAL" not in result.text


def test_a_truncated_header_after_a_complete_block_still_redacts_its_body():
    text = ("-----BEGIN PRIVATE KEY-----\nFIRSTKEY\n-----END PRIVATE KEY-----\n"
            "notes\n"
            "-----BEGIN EC PRIVATE KEY-----\nSECONDKEY")
    result = redact(text)
    assert len(result.findings) == 2
    assert "FIRSTKEY" not in result.text
    assert "SECONDKEY" not in result.text
    assert "notes" in result.text


@pytest.mark.parametrize("url", URLS)
def test_credential_urls_are_found(url):
    scan = detect(f"DATABASE_URL={url} is configured")
    assert scan.kinds == ("credential_url",)
    assert "fake-pass-123" not in redact(url).text


@pytest.mark.parametrize(("line", "prefix"), ENV_LINES)
def test_env_assignments_are_found(line, prefix):
    scan = detect(line)
    assert scan.kinds == ("env_assignment",)
    result = redact(line)
    assert result.text.startswith(prefix)
    assert "fake" not in result.text.lower()


def test_env_assignment_keeps_quotes_and_only_replaces_the_value():
    result = redact('DATABASE_PASSWORD="fake-db-password"')
    assert result.text.startswith('DATABASE_PASSWORD="[REDACTED:env_assignment:')
    assert result.text.endswith('"')
    assert "fake-db-password" not in result.text


def test_a_hash_inside_a_bare_value_is_part_of_the_secret():
    result = redact("API_KEY=fake#value")
    assert "fake#value" not in result.text
    assert result.text.startswith("API_KEY=[REDACTED:env_assignment:")


def test_an_inline_comment_is_not_part_of_the_redacted_value():
    result = redact("API_KEY=fake-value-123 # keep this note")
    assert "fake-value-123" not in result.text
    assert result.text.endswith(" # keep this note")


def test_a_specific_token_wins_inside_an_env_assignment():
    # The name stays readable; the specific shape is more useful than a generic
    # assignment finding for the same span.
    text = f"OPENAI_API_KEY={OPENAI}"
    scan = detect(text)
    assert scan.kinds == ("openai_key",)
    result = redact(text)
    assert result.text.startswith("OPENAI_API_KEY=[REDACTED:openai_key:")
    assert "A1b2C3d4" not in result.text


@pytest.mark.parametrize("text", [
    "The password is stored in the vault, never in the chat.",
    "Use the token budget to trim history.",
    "550e8400-e29b-41d4-a716-446655440000",
    "d41d8cd98f00b204e9800998ecf8427e" * 2,
    "9fceb02d0ae598e95dc970b74767f19372d61af8",
    "contact fake.user@example.test about it",
    "see https://example.test/docs?topic=secrets",
    "postgres://localhost:5432/app",
    "redis://localhost:6379/0",
    "git@github.com:owner/repo.git",
    "the sk- prefix marks an OpenAI key",
    "The marker -----BEGIN RSA PRIVATE KEY----- appears inline in prose.",
    "Authorization: Bearer",
    "Bearer short",
    "AKIA12345678",
    "AIza1234",
    "sk-short",
    "NO_SECRET=1",
])
def test_ordinary_text_is_not_flagged(text):
    assert detect(text).clean
    assert redact(text).text == text


@pytest.mark.parametrize("line", [
    "MONKEY=banana",
    "TOKENIZER=bert-base-uncased",
    "KEYBOARD=us",
    "MAX_TOKENS=4096",
    "TOKEN_BUDGET=131072",
    "PASSWORD_RESET_URL=https://example.test/reset",
    "PASSWORD_HASH=fake-hash-value",
    "SSH_KEY=~/.ssh/id_ed25519",
    "PUBLIC_KEY=fake-public-key",
    "PRIVATE_KEY_PATH=~/.ssh/id_ed25519",
    "API_KEY_FILE=~/.config/fake-key",
    "KEY_ROTATION_DAYS=30",
    "PWD=/home/you/projects/demo",
    "GOOGLE_APPLICATION_CREDENTIALS=~/.config/credentials.json",
    "DATABASE_URL=postgres://localhost:5432/app",
    "postgres://user:password@localhost/app",
    "token = secrets.token_urlsafe(32)",
    "api_key='private' if 'api_key' in spec else None",
    "secret = code.claim(code.secret, 'phone')",
    "API_KEY = 'fake-value'",
    "api-key: fake-value",
])
def test_nonsecret_assignments_and_paths_are_not_flagged(line):
    assert detect(line).clean


@pytest.mark.parametrize("line", [
    "API_KEY=changeme",
    "TOKEN=${GITHUB_TOKEN}",
    "API_KEY=",
    "SECRET=xxxxx",
    "API_KEY=your-api-key-here",
    'PASSWORD="<your-password>"',
    "PASSWORD=changeme # TODO",
    "OPENAI_API_KEY=[REDACTED:openai_key:3f9a]",
])
def test_placeholders_and_variable_references_are_not_flagged(line):
    assert detect(line).clean


def test_findings_are_ordered_and_do_not_overlap():
    scan = detect(f"key {OPENAI}\nDATABASE_PASSWORD=fake-db-password")
    assert scan.kinds == ("openai_key", "env_assignment")
    first, second = scan.findings
    assert first.end <= second.start


def test_placeholders_are_deterministic_and_do_not_contain_the_secret():
    text = f"one {OPENAI} two {OPENAI}"
    first = redact(text)
    second = redact(text)
    assert first.text == second.text
    assert len(first.findings) == 2
    assert first.findings[0].placeholder == first.findings[1].placeholder
    assert OPENAI not in first.text
    assert re.fullmatch(r"\[REDACTED:[a-z_]+:[0-9a-f]{4}\]",
                        first.findings[0].placeholder)
    assert OPENAI not in repr(first.findings)


def test_different_secrets_get_different_placeholders():
    one = redact("sk-" + "A" * 40).findings[0].placeholder
    two = redact("sk-" + "B" * 40).findings[0].placeholder
    assert one != two


def test_redaction_is_idempotent():
    text = f"OPENAI_API_KEY={OPENAI}\nDEEPSEEK_URL={URLS[0]}"
    once = redact(text)
    assert once.changed
    assert detect(once.text).clean
    assert redact(once.text).text == once.text


def test_clean_text_reports_clean():
    scan = detect("nothing sensitive here")
    assert isinstance(scan, Scan)
    assert not scan.found
    assert scan.clean
    result = redact("nothing sensitive here")
    assert isinstance(result, Redaction)
    assert result.text == "nothing sensitive here"
    assert not result.changed


def test_empty_text_is_clean():
    assert detect("").clean
    assert redact("").text == ""
    assert not redact("").changed


def test_contains_secret_is_a_bounded_boolean():
    assert contains_secret(f"token {OPENAI}")
    assert not contains_secret("token " + "A" * 40)
    assert contains_secret("x" * 60 + " " + OPENAI, max_scan_chars=50)


def test_a_secret_beyond_the_scan_limit_is_removed_not_returned():
    token = "sk-proj-" + "Z" * 40
    text = "x" * 60 + " " + token
    scan = detect(text, max_scan_chars=50)
    assert scan.truncated
    assert not scan.findings
    result = redact(text, max_scan_chars=50)
    assert result.truncated
    assert result.changed
    assert token not in result.text
    assert "[REDACTED:unscanned:" in result.text


def test_the_findings_cap_removes_the_unaccepted_remainder():
    lines = [f"API_KEY=fake-value-{index:04d}" for index in range(5)]
    text = "\n".join(lines)
    scan = detect(text, max_findings=2)
    assert scan.overflowed
    assert len(scan.findings) == 2
    result = redact(text, max_findings=2)
    assert result.overflowed
    assert "fake-value-0000" not in result.text
    assert "fake-value-0001" not in result.text
    assert "fake-value-0003" not in result.text
    assert "fake-value-0004" not in result.text
    assert "[REDACTED:overflow:" in result.text


def test_zero_findings_still_removes_everything_from_the_first_match():
    text = "prefix\nAPI_KEY=fake-value-0000"
    result = redact(text, max_findings=0)
    assert result.overflowed
    assert "fake-value-0000" not in result.text
    assert result.text.startswith("prefix\nAPI_KEY=[REDACTED:overflow:")


def test_scan_limits_are_clamped_to_the_module_maximum():
    text = "API_KEY=fake-value-0000"
    assert detect(text, max_scan_chars=MAX_SCAN_CHARS * 10).found
    assert detect(text, max_scan_chars=0).truncated


def test_repetitive_long_input_is_handled_in_bounded_time():
    text = ("A" * 5000 + "\n") * 20
    started = time.monotonic()
    scan = detect(text)
    assert scan.clean
    assert time.monotonic() - started < 5.0
