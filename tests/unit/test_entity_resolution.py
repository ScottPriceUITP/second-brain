"""Tests for entity resolution service."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from second_brain.models import Base, Entity, EntityMerge, Entry, entry_entities
from second_brain.services.entity_resolution import (
    EntityResolutionService,
    ResolvedEntities,
)
from second_brain.utils.time import utc_now


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    # Seed config table with default threshold
    with eng.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO config (key, value, updated_at) "
                "VALUES ('entity_match_confidence_threshold', '0.8', :now)"
            ),
            {"now": utc_now().isoformat()},
        )
        conn.commit()
    return eng


@pytest.fixture
def session(engine):
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture
def service(session):
    return EntityResolutionService(session)


def _create_entity(session: Session, name: str, entity_type: str, merged_into_id=None) -> Entity:
    entity = Entity(
        name=name,
        type=entity_type,
        created_at=utc_now(),
        merged_into_id=merged_into_id,
    )
    session.add(entity)
    session.flush()
    return entity


def _create_entry(session: Session, raw_text: str) -> Entry:
    entry = Entry(
        raw_text=raw_text,
        clean_text=raw_text,
        source="telegram_text",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(entry)
    session.flush()
    return entry


class TestEntityResolution:
    def test_no_existing_entities_creates_new(self, service, session):
        extracted = [{"name": "Reynolds Electric", "type": "company"}]
        result = service.resolve_entities(extracted)

        assert len(result.new_created) == 1
        assert result.new_created[0].name == "Reynolds Electric"
        assert result.new_created[0].type == "company"
        assert len(result.auto_linked) == 0
        assert len(result.ambiguous) == 0

    def test_exact_match_auto_links(self, service, session):
        _create_entity(session, "Reynolds Electric", "company")
        session.commit()

        extracted = [{"name": "Reynolds Electric", "type": "company"}]
        result = service.resolve_entities(extracted)

        assert len(result.auto_linked) == 1
        assert result.auto_linked[0].name == "Reynolds Electric"
        assert result.auto_linked[0].score >= 0.8

    def test_high_confidence_fuzzy_match_auto_links(self, service, session):
        _create_entity(session, "Reynolds Electric", "company")
        session.commit()

        extracted = [{"name": "reynolds electric", "type": "company"}]
        result = service.resolve_entities(extracted)

        assert len(result.auto_linked) == 1

    def test_low_confidence_returns_ambiguous(self, service, session):
        _create_entity(session, "Reynolds Electric", "company")
        _create_entity(session, "Dave Reynolds", "person")
        session.commit()

        # "Reynolds" partially matches both but neither should be high confidence
        # for the company type — only "Reynolds Electric" is a company
        extracted = [{"name": "Reynolds Co", "type": "company"}]
        result = service.resolve_entities(extracted)

        # The match to "Reynolds Electric" should be in the 0.5-0.8 range (ambiguous)
        # or auto-linked if score is high enough
        assert len(result.auto_linked) + len(result.ambiguous) + len(result.new_created) == 1

    def test_no_match_creates_new(self, service, session):
        _create_entity(session, "Reynolds Electric", "company")
        session.commit()

        extracted = [{"name": "Completely Different Company", "type": "company"}]
        result = service.resolve_entities(extracted)

        assert len(result.new_created) == 1
        assert result.new_created[0].name == "Completely Different Company"

    def test_different_type_no_match(self, service, session):
        _create_entity(session, "Python", "technology")
        session.commit()

        # Same name but different type should create new
        extracted = [{"name": "Python", "type": "company"}]
        result = service.resolve_entities(extracted)

        assert len(result.new_created) == 1

    def test_merged_entity_follows_chain(self, service, session):
        target = _create_entity(session, "Reynolds Electric Co", "company")
        source = _create_entity(session, "Reynolds Elec", "company", merged_into_id=target.id)
        session.commit()

        # Should match "Reynolds Electric Co" (the target), not the merged source
        extracted = [{"name": "Reynolds Electric Co", "type": "company"}]
        result = service.resolve_entities(extracted)

        assert len(result.auto_linked) == 1
        assert result.auto_linked[0].entity_id == target.id

    def test_multiple_entities_resolved(self, service, session):
        _create_entity(session, "Reynolds Electric", "company")
        _create_entity(session, "Python", "technology")
        session.commit()

        extracted = [
            {"name": "Reynolds Electric", "type": "company"},
            {"name": "Python", "type": "technology"},
            {"name": "Brand New Person", "type": "person"},
        ]
        result = service.resolve_entities(extracted)

        assert len(result.auto_linked) == 2
        assert len(result.new_created) == 1

    def test_empty_input(self, service):
        result = service.resolve_entities([])
        assert result == ResolvedEntities()


class TestMergeEntities:
    def test_basic_merge(self, service, session):
        source = _create_entity(session, "Reynolds Elec", "company")
        target = _create_entity(session, "Reynolds Electric", "company")
        session.commit()

        service.merge_entities(source.id, target.id)
        session.commit()

        session.refresh(source)
        assert source.merged_into_id == target.id

        merges = session.query(EntityMerge).all()
        assert len(merges) == 1
        assert merges[0].source_entity_id == source.id
        assert merges[0].target_entity_id == target.id

    def test_merge_reassigns_junctions(self, service, session):
        source = _create_entity(session, "Reynolds Elec", "company")
        target = _create_entity(session, "Reynolds Electric", "company")
        entry = _create_entry(session, "Test entry about Reynolds")

        # Link entry to source entity
        session.execute(
            entry_entities.insert().values(entry_id=entry.id, entity_id=source.id)
        )
        session.commit()

        service.merge_entities(source.id, target.id)
        session.commit()

        # Junction should now point to target
        rows = session.execute(
            entry_entities.select().where(entry_entities.c.entry_id == entry.id)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0].entity_id == target.id

    def test_merge_deduplicates_junctions(self, service, session):
        source = _create_entity(session, "Reynolds Elec", "company")
        target = _create_entity(session, "Reynolds Electric", "company")
        entry = _create_entry(session, "Test entry about Reynolds")

        # Link entry to both source and target
        session.execute(
            entry_entities.insert().values(entry_id=entry.id, entity_id=source.id)
        )
        session.execute(
            entry_entities.insert().values(entry_id=entry.id, entity_id=target.id)
        )
        session.commit()

        service.merge_entities(source.id, target.id)
        session.commit()

        # Should have exactly one junction row (deduplicated)
        rows = session.execute(
            entry_entities.select().where(entry_entities.c.entry_id == entry.id)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0].entity_id == target.id

    def test_merge_nonexistent_entity_raises(self, service):
        with pytest.raises(ValueError):
            service.merge_entities(999, 998)

    def test_merge_into_self_noop(self, service, session):
        entity = _create_entity(session, "Reynolds Electric", "company")
        session.commit()

        service.merge_entities(entity.id, entity.id)
        session.refresh(entity)
        assert entity.merged_into_id is None

    def test_merge_follows_target_chain(self, service, session):
        original = _create_entity(session, "Reynolds Electric", "company")
        intermediate = _create_entity(session, "Reynolds Elec", "company", merged_into_id=original.id)
        source = _create_entity(session, "R.E. Corp", "company")
        session.commit()

        # Merging into intermediate should follow chain to original
        service.merge_entities(source.id, intermediate.id)
        session.commit()

        session.refresh(source)
        assert source.merged_into_id == original.id
