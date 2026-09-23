"""Unit tests: no database required.

Chunking, RRF fusion, converter behaviour and configuration validation all live
here. Anything that needs a real PostgreSQL — vector search, RLS, the queue —
belongs in ``tests/integration``.
"""
