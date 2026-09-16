"""Project storage: the authoritative documents and their atomic writes.

``project.json`` is the editable truth; ``assets.json`` is where its asset ids
point (path + measurement).  Both are written through the same staged-then-replace
rule and read strictly.
"""
from .assets import (ASSETS_FILENAME, AssetIndex, AssetRef, assets_path,
                     referenced_ids, resolve)
from .project_store import (PROJECT_FILENAME, load_project, project_path,
                            read_document, save_project, write_document)

__all__ = ['ASSETS_FILENAME', 'AssetIndex', 'AssetRef', 'PROJECT_FILENAME',
           'assets_path', 'load_project', 'project_path', 'read_document',
           'referenced_ids', 'resolve', 'save_project', 'write_document']
