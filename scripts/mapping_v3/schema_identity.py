"""Schema-declared semantic identity strategies used by the mapper."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ImpliedStrategy(StrEnum):
    """Supported forms of identity implied by a FLYNC document location."""

    FOLDER_NAME = "folder-name"
    FILE_NAME = "file-name"


@dataclass(frozen=True)
class SchemaIdentity:
    """Local representation of an identity declaration from the FLYNC schema."""

    object_kind: str
    strategy: ImpliedStrategy


# The official ECU and Controller ``name`` fields both declare an implied
# FOLDER_NAME identity. Keeping the declaration as data avoids coupling the
# mapper to the complete FLYNC runtime while preserving that schema behavior.
ECU_IDENTITY = SchemaIdentity("ecu", ImpliedStrategy.FOLDER_NAME)
CONTROLLER_IDENTITY = SchemaIdentity("controller", ImpliedStrategy.FOLDER_NAME)


def implied_semantic_identity(identity: SchemaIdentity, document: Path) -> str | None:
    """Apply a schema identity declaration to a document path."""
    if identity.strategy == ImpliedStrategy.FOLDER_NAME:
        return document.parent.name
    if identity.strategy == ImpliedStrategy.FILE_NAME:
        name = document.name
        return name.removesuffix(".flync.yaml").removesuffix(".flync.yml")
    return None
