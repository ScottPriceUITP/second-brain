"""Entity resolution service — fuzzy matching extracted entities against the database."""

import logging
from dataclasses import dataclass, field

from sqlalchemy import update
from sqlalchemy.orm import Session

from second_brain.config import get_config_float
from second_brain.models.entity import Entity, entry_entities
from second_brain.models.entity_merge import EntityMerge
from second_brain.utils.fuzzy_match import fuzzy_match
from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)


@dataclass
class LinkedEntity:
    """An extracted entity that was auto-linked to an existing entity."""

    entity_id: int
    name: str
    type: str
    score: float


@dataclass
class AmbiguousEntity:
    """An extracted entity with multiple possible matches below the auto-link threshold."""

    extracted_name: str
    extracted_type: str
    candidates: list[tuple[int, str, float]]  # (entity_id, name, score)


@dataclass
class NewEntity:
    """An extracted entity that was created as a new entity."""

    entity_id: int
    name: str
    type: str


@dataclass
class ResolvedEntities:
    """Result of entity resolution containing all three categories."""

    auto_linked: list[LinkedEntity] = field(default_factory=list)
    ambiguous: list[AmbiguousEntity] = field(default_factory=list)
    new_created: list[NewEntity] = field(default_factory=list)


class EntityResolutionService:
    """Resolves extracted entities against existing database entities."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _get_threshold(self) -> float:
        """Get the entity match confidence threshold from config."""
        return get_config_float(self.session, "entity_match_confidence_threshold") or 0.8

    def _follow_merge_chain(self, entity: Entity) -> Entity:
        """Follow the merged_into_id chain to find the canonical entity.

        If an entity has been merged into another, follow the chain until
        we find the final target entity.
        """
        visited: set[int] = set()
        current = entity

        while current.merged_into_id is not None:
            if current.merged_into_id in visited:
                logger.warning(
                    "Circular merge chain detected for entity %d", current.id
                )
                break
            visited.add(current.id)
            target = self.session.get(Entity, current.merged_into_id)
            if target is None:
                logger.warning(
                    "Merge target %d not found for entity %d",
                    current.merged_into_id,
                    current.id,
                )
                break
            current = target

        return current

    def resolve_entities(
        self,
        extracted_entities: list[dict],
    ) -> ResolvedEntities:
        """Resolve a list of extracted entities against the database.

        For each extracted entity {name, type}:
        1. Query existing entities of the same type
        2. Fuzzy match against existing entity names
        3. High confidence (>= threshold): auto-link to existing entity
        4. Low confidence (0.5 to threshold): return as ambiguous
        5. No match (< 0.5): create new entity

        Args:
            extracted_entities: List of dicts with 'name' and 'type' keys.

        Returns:
            ResolvedEntities with auto_linked, ambiguous, and new_created lists.
        """
        threshold = self._get_threshold()
        result = ResolvedEntities()

        for extracted in extracted_entities:
            name = extracted["name"]
            entity_type = extracted["type"]

            existing = (
                self.session.query(Entity)
                .filter(Entity.type == entity_type, Entity.merged_into_id.is_(None))
                .all()
            )

            if not existing:
                new_entity = self._create_entity(name, entity_type)
                result.new_created.append(
                    NewEntity(entity_id=new_entity.id, name=name, type=entity_type)
                )
                continue

            candidate_names = [e.name for e in existing]
            matches = fuzzy_match(name, candidate_names, threshold=0.5)

            if not matches:
                new_entity = self._create_entity(name, entity_type)
                result.new_created.append(
                    NewEntity(entity_id=new_entity.id, name=name, type=entity_type)
                )
                continue

            best_name, best_score = matches[0]

            if best_score >= threshold:
                # High confidence — auto-link
                matched_entity = next(e for e in existing if e.name == best_name)
                canonical = self._follow_merge_chain(matched_entity)
                result.auto_linked.append(
                    LinkedEntity(
                        entity_id=canonical.id,
                        name=canonical.name,
                        type=entity_type,
                        score=best_score,
                    )
                )
                logger.info(
                    "Auto-linked '%s' to existing entity '%s' (id=%d, score=%.2f)",
                    name,
                    canonical.name,
                    canonical.id,
                    best_score,
                )
            else:
                # Low confidence (0.5 to threshold) — ambiguous
                candidates = []
                for match_name, match_score in matches:
                    matched_entity = next(e for e in existing if e.name == match_name)
                    canonical = self._follow_merge_chain(matched_entity)
                    candidates.append((canonical.id, canonical.name, match_score))

                result.ambiguous.append(
                    AmbiguousEntity(
                        extracted_name=name,
                        extracted_type=entity_type,
                        candidates=candidates,
                    )
                )
                logger.info(
                    "Ambiguous match for '%s': %d candidates",
                    name,
                    len(candidates),
                )

        return result

    def _create_entity(self, name: str, entity_type: str) -> Entity:
        """Create a new entity in the database."""
        entity = Entity(
            name=name,
            type=entity_type,
            created_at=utc_now(),
        )
        self.session.add(entity)
        self.session.flush()
        logger.info("Created new entity '%s' (type=%s, id=%d)", name, entity_type, entity.id)
        return entity

    def merge_entities(self, source_id: int, target_id: int) -> None:
        """Merge source entity into target entity.

        Sets source's merged_into_id to target, creates EntityMerge record,
        and reassigns all entry-entity junction rows from source to target.

        Args:
            source_id: ID of the entity to merge away.
            target_id: ID of the entity to absorb the source.
        """
        source = self.session.get(Entity, source_id)
        target = self.session.get(Entity, target_id)

        if source is None or target is None:
            raise ValueError(f"Entity not found: source={source_id}, target={target_id}")

        # Follow merge chain on target to get canonical
        target = self._follow_merge_chain(target)

        if source.id == target.id:
            logger.warning("Cannot merge entity %d into itself", source.id)
            return

        # Set merged_into_id
        source.merged_into_id = target.id

        # Create EntityMerge record
        merge_record = EntityMerge(
            source_entity_id=source.id,
            target_entity_id=target.id,
            merged_at=utc_now(),
        )
        self.session.add(merge_record)

        # Reassign entry-entity junction rows from source to target.
        # First, delete source rows where the entry is already linked to target
        # (to avoid UNIQUE constraint violation), then update the rest.
        from sqlalchemy import and_, select

        already_linked = select(entry_entities.c.entry_id).where(
            entry_entities.c.entity_id == target.id
        )
        self.session.execute(
            entry_entities.delete().where(
                and_(
                    entry_entities.c.entity_id == source.id,
                    entry_entities.c.entry_id.in_(already_linked),
                )
            )
        )

        # Now update remaining source rows to point to target
        self.session.execute(
            update(entry_entities)
            .where(entry_entities.c.entity_id == source.id)
            .values(entity_id=target.id)
        )

        self.session.flush()
        logger.info(
            "Merged entity '%s' (id=%d) into '%s' (id=%d)",
            source.name,
            source.id,
            target.name,
            target.id,
        )
