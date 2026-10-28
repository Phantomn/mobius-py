import json

from mobius.codex_config import CodexConfigIO
from mobius.models import AccountProfile, CodexCredentialsSnapshot, CredentialsSnapshot
from mobius.store import AccountStore


def _claude(tag="A"):
    return CredentialsSnapshot(
        credentials_blob=json.dumps({"claudeAiOauth": {"accessToken": f"at-{tag}"}}).encode(),
        oauth_account={"emailAddress": f"{tag.lower()}@example.com"},
    )


def test_legacy_claude_secret_can_gain_codex_secret(env):
    store = AccountStore(env)
    profile = store.upsert_profile("unified", _claude())
    codex_blob = json.dumps({"tokens": {"access_token": "codex-at", "account_id": "acct-1"}}).encode()

    store.set_codex_secret(CodexCredentialsSnapshot(codex_blob, "acct-1", "pro"), profile.id)

    saved = AccountStore(env)
    assert saved.secret(profile.id).credentials_blob == _claude().credentials_blob
    codex = saved.codex_secret(profile.id)
    assert codex is not None
    assert codex.auth_blob == codex_blob
    assert codex.account_id == "acct-1"
    assert codex.plan_type == "pro"


def test_account_profile_roundtrips_hidden_codex_identity():
    profile = AccountProfile("id", "name", "user@example.com", codex_account_id="acct-1",
                             codex_plan_type="self_serve_business_prolite")
    restored = AccountProfile.from_dict(profile.to_dict())
    assert restored.codex_account_id == "acct-1"
    assert restored.codex_plan_type == "self_serve_business_prolite"


def test_codex_auth_snapshot_can_be_written_atomically(env):
    raw = json.dumps({"tokens": {"access_token": "at", "account_id": "acct"}}).encode()
    io = CodexConfigIO(env.home)
    io.write_auth(raw)
    assert io.read_auth().raw_blob == raw


def test_codex_capture_merges_into_existing_nickname(env):
    store = AccountStore(env)
    claude = store.upsert_profile("same-account", _claude())
    codex = CodexCredentialsSnapshot(b'{"tokens":{}}', "acct-merge", "pro")

    merged = store.upsert_codex_profile("same-account", codex)

    assert merged.id == claude.id
    assert len(store.file.accounts) == 1
    assert store.codex_secret(merged.id).account_id == "acct-merge"


def test_codex_only_account_is_not_a_claude_fallback(env):
    store = AccountStore(env)
    profile = store.upsert_codex_profile(
        "codex-only", CodexCredentialsSnapshot(b'{"tokens":{}}', "acct-only", "pro"))
    assert profile.has_claude_credentials is False
