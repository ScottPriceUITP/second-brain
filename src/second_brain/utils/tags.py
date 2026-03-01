"""Tag utilities — shared helpers for creating/linking tags to entries."""

from sqlalchemy.orm import Session

from second_brain.models.tag import Tag


def store_tags(session: Session, entry, tag_names: list[str]) -> None:
    """Create or get-existing tags and link them to the entry."""
    if not tag_names:
        return

    for tag_name in tag_names:
        tag_name = tag_name.strip().lower()
        if not tag_name:
            continue

        tag = session.query(Tag).filter(Tag.name == tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            session.add(tag)
            session.flush()

        if tag not in entry.tags:
            entry.tags.append(tag)
