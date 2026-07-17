"""CLI package for minio-aiops.

Re-exports ``app`` so the pyproject entry point
``minio-aiops = "minio_aiops.cli:app"`` works unchanged.
"""

from minio_aiops.cli._root import app

__all__ = ["app"]
