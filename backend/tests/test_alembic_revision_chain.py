from pathlib import Path
import re


def _extract(name: str, text: str) -> str | None:
    # Supports:
    #   key = "value"
    #   key = 'value'
    #   key: str = "value"
    #   key: Union[str, None] = 'value'
    m = re.search(
        rf"^{name}(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]",
        text,
        re.MULTILINE,
    )
    return m.group(1) if m else None


def test_alembic_down_revision_references_existing_revision_ids():
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    py_files = sorted(versions_dir.glob("*.py"))

    revisions: set[str] = set()
    down_refs: list[tuple[str, str, str]] = []

    for path in py_files:
        text = path.read_text(encoding="utf-8")
        revision = _extract("revision", text)
        if not revision:
            continue
        revisions.add(revision)

        down = _extract("down_revision", text)
        if down:
            down_refs.append((path.name, revision, down))

    invalid = [(f, r, d) for (f, r, d) in down_refs if d not in revisions]

    assert not invalid, (
        "Alembic chain has unknown down_revision reference(s): "
        + ", ".join([f"{f}:{r}->{d}" for (f, r, d) in invalid])
    )


def test_alembic_revision_chain_has_expected_single_head():
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    revisions: set[str] = set()
    down_revisions: set[str] = set()

    for path in versions_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = _extract("revision", text)
        if revision:
            revisions.add(revision)
        down_revision = _extract("down_revision", text)
        if down_revision:
            down_revisions.add(down_revision)

    assert revisions - down_revisions == {"029_reservation_sync_round"}


def test_patient_line_id_has_non_unique_lookup_index():
    from app.models.patient import Patient

    indexes = {index.name: index for index in Patient.__table__.indexes}
    assert "ix_patients_line_id" in indexes
    assert indexes["ix_patients_line_id"].unique is not True


def test_line_source_ref_has_partial_unique_index():
    from app.models.reservation import Reservation

    indexes = {index.name: index for index in Reservation.__table__.indexes}
    index = indexes["uq_reservations_line_source_ref"]

    assert index.unique is True
    assert "channel = 'LINE'" in str(index.dialect_options["postgresql"]["where"])
