"""Project storage: the authoritative documents and their atomic writes.

``project.json`` is the editable truth; ``assets.json`` is where its asset ids
point (path + measurement).  Both are written through the same staged-then-replace
rule and read strictly.
"""
from .assets import (ASSETS_FILENAME, AssetIndex, AssetRef, assets_path,
                     referenced_ids, resolve)
from .project_store import (PROJECT_FILENAME, load_project, project_path,
                            read_document, save_project, write_document)
from .templates import (BUILTIN_TEMPLATE, BUILTIN_TEMPLATE_ID, TEMPLATES_DIR,
                        TemplateStore)

__all__ = ['ASSETS_FILENAME', 'AssetIndex', 'AssetRef', 'BUILTIN_TEMPLATE',
           'BUILTIN_TEMPLATE_ID', 'PROJECT_FILENAME', 'TEMPLATES_DIR',
           'TemplateStore', 'assets_path', 'load_project', 'project_path',
           'read_document', 'referenced_ids', 'resolve', 'save_project',
           'write_document']
