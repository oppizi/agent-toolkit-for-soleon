"""Engine seam + converter behavior tests — every branch in the eng-review
coverage diagram. Uses the real vendored binary (these are the contract tests
that pin the CLI's JSON shapes)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import engine
from allium_to_json import (
    ConvertError,
    DecodeError,
    SpecError,
    ValidationError,
    convert,
    decode_default_expr,
    read_spec,
    validate_params,
)
from conftest import write_spec

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = BET_ROOT / "plugin"
REAL_IDENTITY = BET_ROOT.parents[1] / ".claude/agents/soleon-fde.md"


@pytest.fixture(scope="module")
def resolved():
    contract = engine.load_contract(PLUGIN)
    binary, version = engine.resolve(contract, PLUGIN)
    return contract, binary, version


# ---------- resolver ----------

def test_bundled_binary_resolves_first(resolved):
    contract, binary, version = resolved
    assert "plugin/bin/allium-" in binary.replace("\\", "/")
    assert version == contract["engine_version"]


def test_arch_normalization_map():
    assert engine._ARCH_NORMALIZE["aarch64"] == "arm64"
    assert engine._ARCH_NORMALIZE["amd64"] == "x86_64"
    name = engine._expected_binary_name()
    assert name.startswith("allium-") and name.count("-") == 2


def test_missing_binary_actionable_error(tmp_path, monkeypatch):
    # A plugin root with a contract but no bin/ and an empty PATH.
    shutil.copy(PLUGIN / "contract.json", tmp_path / "contract.json")
    monkeypatch.setenv("PATH", str(tmp_path))
    contract = engine.load_contract(tmp_path)
    with pytest.raises(engine.EngineNotFound) as exc:
        engine.resolve(contract, tmp_path)
    msg = str(exc.value)
    assert engine._expected_binary_name() in msg  # names the exact expected file
    assert "cargo install" in msg  # carries the copy-paste recovery command


def test_path_fallback_version_pin(tmp_path, monkeypatch):
    # Fake an `allium` on PATH reporting a different version.
    fake = tmp_path / "allium"
    fake.write_text("#!/bin/sh\necho 'allium 9.9.9 (language versions: 1)'\n")
    fake.chmod(0o755)
    shutil.copy(PLUGIN / "contract.json", tmp_path / "contract.json")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("ALLIUM_ENGINE_UNPINNED", raising=False)
    contract = engine.load_contract(tmp_path)
    with pytest.raises(engine.EngineVersionMismatch) as exc:
        engine.resolve(contract, tmp_path)
    assert "ALLIUM_ENGINE_UNPINNED" in str(exc.value)
    monkeypatch.setenv("ALLIUM_ENGINE_UNPINNED", "1")
    binary, version = engine.resolve(contract, tmp_path)
    assert version == "9.9.9"


# ---------- contract self-check ----------

def test_contract_missing_key_fails(tmp_path):
    doc = json.loads((PLUGIN / "contract.json").read_text())
    del doc["slug_pattern"]
    (tmp_path / "contract.json").write_text(json.dumps(doc))
    with pytest.raises(engine.ContractError) as exc:
        engine.load_contract(tmp_path)
    assert "slug_pattern" in str(exc.value)


def test_contract_corrupt_json_fails(tmp_path):
    (tmp_path / "contract.json").write_text("{not json")
    with pytest.raises(engine.ContractError) as exc:
        engine.load_contract(tmp_path)
    assert "reinstall" in str(exc.value)


def test_contract_wrong_version_fails(tmp_path):
    doc = json.loads((PLUGIN / "contract.json").read_text())
    doc["contract_version"] = 99
    (tmp_path / "contract.json").write_text(json.dumps(doc))
    with pytest.raises(engine.ContractError):
        engine.load_contract(tmp_path)


def test_selfcheck_green():
    summary = engine.selfcheck(PLUGIN)
    assert summary["engine_version"] == summary["engine_pinned"]


# ---------- check / model semantics (live-probed F1 class) ----------

def test_check_gates_on_errors_not_exit_code(resolved, tmp_path):
    _, binary, _ = resolved
    spec = write_spec(tmp_path)  # valid spec still exits 1 on warnings
    diagnostics = engine.check(binary, spec)
    assert all(d["severity"] != "error" for d in diagnostics)


def test_warnings_only_spec_converts(resolved, tmp_path, monkeypatch):
    """F1: missing version marker = warning; passes gate AND converts."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    spec = write_spec(tmp_path, version_marker=False)
    result = convert(spec, "dev", tmp_path / "out")
    assert result["body"]["slug"] == "campaign-sla-watcher"


def test_model_on_spec_without_config_fails(resolved, tmp_path):
    _, binary, _ = resolved
    spec = tmp_path / "noconfig.allium"
    spec.write_text('-- allium: 3\n\nentity Foo {\n    a: String\n}\n')
    with pytest.raises(engine.EngineProtocol):
        engine.model(binary, spec)


def test_garbage_spec_fails_at_check(resolved, tmp_path):
    _, binary, _ = resolved
    spec = tmp_path / "garbage.allium"
    spec.write_text("total garbage {{{")
    diagnostics = engine.check(binary, spec)
    assert any(d["severity"] == "error" for d in diagnostics)


def test_spec_with_errors_raises_spec_error(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    spec = tmp_path / "bad.allium"
    spec.write_text("total garbage {{{")
    with pytest.raises(SpecError):
        convert(spec, "dev", tmp_path / "out")


# ---------- decode rules (F5) ----------

def test_decode_json_string():
    assert decode_default_expr("soul", '"hello\\nworld"') == "hello\nworld"


def test_decode_enum_bare_identifier():
    assert decode_default_expr("framework", "maverick") == "maverick"


def test_decode_quote_led_malformed_raises_never_falls_back():
    with pytest.raises(DecodeError):
        decode_default_expr("soul", '"unterminated')


def test_decode_non_identifier_non_json_raises():
    with pytest.raises(DecodeError):
        decode_default_expr("x", "{1, 2}")


def test_duplicate_config_params_rejected(resolved, tmp_path):
    _, binary, _ = resolved
    spec = write_spec(tmp_path, config_extra='    slug: String = "other-slug"')
    with pytest.raises(SpecError) as exc:
        read_spec(binary, spec)
    assert "duplicate" in str(exc.value)


# ---------- soul roundtrip (byte-exact, real 29KB identity) ----------

def test_soul_roundtrip_byte_exact_real_identity(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    if REAL_IDENTITY.is_file():
        soul = REAL_IDENTITY.read_text(encoding="utf-8")
    else:
        # Standalone (public-repo) clone: the real internal identity file is
        # not distributed — synthesize an equivalent-size adversarial soul so
        # the large-payload byte-exact roundtrip is still exercised.
        block = (
            "## Section\n\nYou are a \"careful\" agent. Never invent data.\n\n"
            "```python\nx = a % b  # 100%\n```\n- bullet with \\n literal\n\n"
        )
        soul = "# Synthetic Large Identity\n\n" + block * 400 + "🌱 end\n"
    assert len(soul) > 20_000
    spec = write_spec(tmp_path, soul=soul)
    result = convert(spec, "dev", tmp_path / "out")
    assert result["body"]["configFields"]["soul"] == soul  # byte-exact


def test_soul_roundtrip_adversarial_escapes(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    soul = 'literal \\n not newline, "quotes", back\\slash, emoji 🌱, 100%\n```py\nx = a % b\n```\n'
    spec = write_spec(tmp_path, soul=soul)
    result = convert(spec, "dev", tmp_path / "out")
    assert result["body"]["configFields"]["soul"] == soul


# ---------- local validation (F6/F9 + enums) ----------

def _params(**over):
    base = {
        "slug": "campaign-sla-watcher",
        "display_name": "Campaign SLA Watcher",
        "framework": "maverick",
        "visibility": "private",
        "soul": "watch carefully",
    }
    base.update(over)
    return base


@pytest.fixture(scope="module")
def contract_doc():
    return engine.load_contract(PLUGIN)


def test_display_name_emitted_stripped(contract_doc):
    v = validate_params(_params(display_name="  Padded Name  "), "dev", contract_doc)
    assert v["display_name"] == "Padded Name"


def test_display_name_padded_over_raw_cap_passes_after_strip(contract_doc):
    # raw 257 / stripped 250: the server caps what is SENT; we send stripped.
    v = validate_params(_params(display_name=" " * 4 + "x" * 250 + " " * 3), "dev", contract_doc)
    assert len(v["display_name"]) == 250


def test_display_name_stripped_over_cap_fails(contract_doc):
    with pytest.raises(ValidationError):
        validate_params(_params(display_name="x" * 257), "dev", contract_doc)


def test_slug_pattern_enforced(contract_doc):
    for bad in ("a", "-bad", "bad-", "Bad", "x" * 65):
        with pytest.raises(ValidationError):
            validate_params(_params(slug=bad), "dev", contract_doc)


def test_framework_enum_enforced(contract_doc):
    with pytest.raises(ValidationError):
        validate_params(_params(framework="custom"), "dev", contract_doc)


def test_app_env_fixture_set_enforced(contract_doc):
    with pytest.raises(ValidationError):
        validate_params(_params(), "qa", contract_doc)


def test_model_alias_rejected(contract_doc):
    with pytest.raises(ValidationError) as exc:
        validate_params(_params(model="fable"), "dev", contract_doc)
    assert "alias" in str(exc.value)


def test_model_catalog_id_accepted(contract_doc):
    v = validate_params(_params(model="us.anthropic.claude-opus-4-6-v1"), "dev", contract_doc)
    assert v["config"]["model"] == "us.anthropic.claude-opus-4-6-v1"


def test_soul_cap_boundary(contract_doc):
    cap = contract_doc["max_soul_bytes"]
    validate_params(_params(soul="x" * cap), "dev", contract_doc)  # exactly cap: OK
    with pytest.raises(ValidationError):
        validate_params(_params(soul="x" * (cap + 1)), "dev", contract_doc)


def test_unknown_config_param_rejected(contract_doc):
    with pytest.raises(ValidationError):
        validate_params(_params(channels="oops"), "dev", contract_doc)


# ---------- full pipeline + invariant/guidance extraction ----------

def test_full_pipeline_schema_match_and_report(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    extra = (
        "entity AgentIdentity {\n    purpose: String\n\n    @guidance\n"
        "        -- NeverInventData: never invent data.\n}\n\n"
        'invariant NeverInventData {\n    for a in AgentIdentities:\n        not (a.purpose = "")\n}\n'
    )
    spec = write_spec(tmp_path, extra=extra)
    result = convert(spec, "dev", tmp_path / "out")
    from validate_offline import validate
    verdict = validate(result["body"], result["projection"])
    assert verdict["schema_match"], verdict
    assert result["report"]["invariants"] == ["NeverInventData"]
    assert any("NeverInventData" in line for block in result["report"]["guidance_blocks"] for line in block)


# ---------- config distillation (config_proposal) ----------

def _proposal_spec(tmp_path, proposal: dict, soul="watch carefully\n") -> Path:
    return write_spec(
        tmp_path, soul=soul,
        config_extra="    config_proposal: String = " + json.dumps(json.dumps(proposal)),
    )


def test_config_proposal_model_evals_guardrails(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    proposal = {
        "model": "us.anthropic.claude-opus-4-8",
        "promptCaching": True, "promptCacheTtl": "1h",
        "guardrails": {"piiEnabled": True, "piiEntities": {"EMAIL": "ANONYMIZE"}},
        "evals": {"standardEvals": [{
            "id": "never-invent", "name": "Never Invent Data",
            "inputs": [{"role": "user", "content": "Make up the number."}],
            "expectedOutput": "Says the metric is unavailable.",
            "scoringMode": "pass_fail", "judgeModel": "claude-opus-4-7",
        }]},
    }
    result = convert(_proposal_spec(tmp_path, proposal), "dev", tmp_path / "out")
    from validate_offline import validate
    assert validate(result["body"], result["projection"])["schema_match"]
    cfg = result["body"]["configFields"]["config"]
    assert cfg["model"] == "us.anthropic.claude-opus-4-8"
    assert cfg["promptCacheTtl"] == "1h"
    assert cfg["evals"]["standardEvals"][0]["id"] == "never-invent"
    assert set(result["report"]["config_keys"]) == set(cfg)


def test_config_proposal_alias_model_rejected(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    with pytest.raises(ValidationError):
        convert(_proposal_spec(tmp_path, {"model": "fable"}), "dev", tmp_path / "out")


def test_config_proposal_schedules_rejected(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    with pytest.raises(ConvertError) as exc:
        convert(_proposal_spec(tmp_path, {"schedules": []}), "dev", tmp_path / "out")
    assert "schedules" in str(exc.value)


def test_config_proposal_bad_eval_judge_rejected(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    proposal = {"evals": {"standardEvals": [{
        "id": "x", "name": "X", "inputs": [{"role": "user", "content": "hi"}],
        "expectedOutput": "ok", "scoringMode": "pass_fail", "judgeModel": "gpt-4",
    }]}}
    with pytest.raises(ValidationError):
        convert(_proposal_spec(tmp_path, proposal), "dev", tmp_path / "out")


def test_model_param_conflicts_with_proposal(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    spec = write_spec(
        tmp_path, soul="x\n",
        config_extra=(
            '    model: String = "us.anthropic.claude-opus-4-8"\n'
            "    config_proposal: String = "
            + json.dumps(json.dumps({"model": "us.anthropic.claude-sonnet-4-6"}))
        ),
    )
    with pytest.raises(ValidationError):
        convert(spec, "dev", tmp_path / "out")


# ---------- skills bundling ----------

def _write_skill_dir(tmp_path, slug, description, body):
    d = tmp_path / "skills" / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\ndescription: {description}\nargument-hint: \"<x>\"\n---\n{body}",
        encoding="utf-8",
    )
    return d


def test_skill_dir_bundled_content_byte_exact(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    body = "When reporting a metric, append [source: <system>, <ts>].\n"
    d = _write_skill_dir(tmp_path, "cite-sources", "Always cite the source.", body)
    spec = write_spec(tmp_path, soul="x\n")
    result = convert(spec, "dev", tmp_path / "out", skill_inputs=[str(d)])
    skills = result["body"]["configFields"]["skills"]
    assert len(skills) == 1
    s = skills[0]
    assert s["id"] == "cite-sources"
    assert s["name"] == "Cite Sources"          # Title-Cased slug default
    assert s["content"] == body                  # byte-exact body
    assert s["description"] == "Always cite the source."
    assert set(s) == {"id", "name", "description", "content", "enabled"}  # argument-hint dropped
    assert result["report"]["skills_included"][0]["dropped_fields"] == ["argument-hint"]


def test_skill_json_input_with_name(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    obj = {"id": "escalate", "name": "Escalation Policy", "description": "",
           "content": "Escalate after 2 failed retries.\n", "enabled": False}
    p = tmp_path / "escalate.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    spec = write_spec(tmp_path, soul="x\n")
    result = convert(spec, "dev", tmp_path / "out", skill_inputs=[str(p)])
    s = result["body"]["configFields"]["skills"][0]
    assert s["name"] == "Escalation Policy" and s["enabled"] is False


def test_skills_collision_after_normalization_rejected(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    d1 = _write_skill_dir(tmp_path, "cite-sources", "a", "x\n")
    d2 = _write_skill_dir(tmp_path, "Cite_Sources", "b", "y\n")  # normalizes to same id
    spec = write_spec(tmp_path, soul="x\n")
    with pytest.raises(ValidationError) as exc:
        convert(spec, "dev", tmp_path / "out", skill_inputs=[str(d1), str(d2)])
    assert "duplicate skill id" in str(exc.value)


def test_skill_rendered_cap_raw_under_rendered_over(resolved, tmp_path, monkeypatch):
    """The cap is on the RENDERED markdown — a long display name pushes a
    near-cap body over even when raw content is under."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    cap = engine.load_contract(PLUGIN)["max_skill_bytes"]
    obj = {"id": "big", "name": "N", "description": "", "content": "x" * (cap - 20)}
    p = tmp_path / "big.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    spec = write_spec(tmp_path, soul="x\n")
    # frontmatter overhead (name/description/enabled fences) pushes it over cap
    with pytest.raises(ValidationError) as exc:
        convert(spec, "dev", tmp_path / "out", skill_inputs=[str(p)])
    assert "renders to" in str(exc.value)


# ---------- projection unchanged + soul-only byte-identical regressions ----------

def test_projection_identical_with_and_without_config_skills(resolved, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    soul = "watch carefully\n"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    plain = convert(write_spec(tmp_path / "a", soul=soul), "dev", tmp_path / "a/out")
    d = _write_skill_dir(tmp_path, "cite-sources", "cite", "body\n")
    rich = convert(
        _proposal_spec(tmp_path / "b", {"promptCaching": True}, soul=soul),
        "dev", tmp_path / "b/out", skill_inputs=[str(d)],
    )
    a, b = dict(plain["projection"]), dict(rich["projection"])
    a.pop("createdAt"); b.pop("createdAt")
    assert a == b, "projection must be independent of config/skills (S3-only)"
    # and the rich body genuinely carried config + skills
    assert "config" in rich["body"]["configFields"]
    assert "skills" in rich["body"]["configFields"]
    assert "config" not in plain["body"]["configFields"]
    assert "skills" not in plain["body"]["configFields"]
