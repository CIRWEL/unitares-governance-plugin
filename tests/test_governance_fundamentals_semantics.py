"""Pin the producer-aware coherence contract in the bundled skill."""

from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "governance-fundamentals"
    / "SKILL.md"
)


def _parts() -> tuple[str, str]:
    text = SKILL.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    return frontmatter, body


def test_coherence_sources_and_roles_are_explicit() -> None:
    frontmatter, body = _parts()

    assert 'last_verified: "2026-08-11"' in frontmatter
    assert "unitares/src/behavioral_sensor.py" in frontmatter
    assert "unitares/src/coherence_provenance.py" in frontmatter
    assert "`legacy_tanh_v`" in body
    assert "`ode_control_feedback`" in body
    assert "`manifold`" in body
    assert "`eis_structural_measurement`" in body


def test_legacy_controller_is_not_taught_as_health_or_balance() -> None:
    _, body = _parts()
    plain_body = body.replace("**", "")

    assert "not a symmetric health/balance score" in plain_body
    assert "Existing critical thresholds are compatibility gates" in body
    assert "Think of it as structural health" not in body
    assert "Coherence reflects balance" not in body


def test_hidden_e_and_i_dependency_is_disclosed() -> None:
    _, body = _parts()

    assert "legacy coherence-level term" in body
    assert "trend of that same legacy controller scalar" in body
