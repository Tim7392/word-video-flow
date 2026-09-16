"""Project storage: the authoritative documents and their atomic writes.

``project.json`` is the editable truth; ``assets.json`` is where its asset ids
point (path + measurement).  Both are written through the same staged-then-replace
rule and read strictly.
"""
from .assets import (ASSETS_FILENAME, AssetIndex, AssetRef, assets_path,
                     referenced_ids, resolve)
from .batches import BATCHES_DIR, BatchStore
from .delivery import DELIVERY_FILENAME, delivery_settings
from .project_store import (PROJECT_FILENAME, load_project, project_path,
                            read_document, save_project, write_document)
from .template_package import (MANIFEST_FILENAME as TEMPLATE_PACKAGE_FILENAME,
                               PACKAGE_SCHEMA as TEMPLATE_PACKAGE_SCHEMA,
                               import_package, read_package, write_package)
from .templates import (BUILTIN_TEMPLATE, BUILTIN_TEMPLATE_ID, TEMPLATES_DIR,
                        TemplateStore)

__all__ = ['ASSETS_FILENAME', 'AssetIndex', 'AssetRef', 'BATCHES_DIR', 'BatchStore',
           'BUILTIN_TEMPLATE', 'BUILTIN_TEMPLATE_ID', 'DELIVERY_FILENAME',
           'PROJECT_FILENAME', 'TEMPLATES_DIR', 'TEMPLATE_PACKAGE_FILENAME',
           'TEMPLATE_PACKAGE_SCHEMA', 'TemplateStore', 'assets_path',
           'delivery_settings', 'import_package', 'load_project', 'project_path',
           'read_document', 'read_package', 'referenced_ids', 'resolve', 'save_project',
           'write_document', 'write_package']
