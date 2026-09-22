"""ORM models for knowledge-brain.

Table definitions follow spec §5 exactly. Key design points:

- `chunks.user_id` is denormalized on purpose (spec §5 ①): tenant filtering
  must live in the retrieval SQL itself, be index-backed, and be unit-testable.
- `documents` is unique on (user_id, source, source_path) (spec §5 ③): this is
  what keeps the git channel and the upload channel from fighting.
"""

from kb.models.api_token import ApiToken
from kb.models.base import Base
from kb.models.chunk import Chunk
from kb.models.conversion_cache import ConversionCache
from kb.models.document import Document
from kb.models.index_config import IndexConfig
from kb.models.repo import Repo
from kb.models.sync_job import SyncJob
from kb.models.user import User

__all__ = [
    "ApiToken",
    "Base",
    "Chunk",
    "ConversionCache",
    "Document",
    "IndexConfig",
    "Repo",
    "SyncJob",
    "User",
]
