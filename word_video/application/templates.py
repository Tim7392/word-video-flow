"""The shareable template: one versioned document a batch can pin.

A template is *arrangement plus style*, and nothing else: which stages a record
speaks, which layers it shows, whether the lesson has an intro layer, and the style
overrides a member settled on.  It is data, never code (architecture §7), and it
carries no absolute working path, no font face and no voice — those keep coming from
the resolution chain — so the same document means the same lesson on another root.

Versions live in separate files (:mod:`word_video.storage.templates`): an upgrade
writes the next version instead of rewriting the one a batch already pinned, which
is what makes "this batch used template X@2" a fact rather than a guess, and what
makes an upgrade undoable by pointing back at the previous number.
"""
from dataclasses import dataclass, replace

from ..domain.errors import SchemaError
from ..domain.lesson import (DEFAULT_LESSON_TEMPLATE, DisplayNode, LessonTemplate,
                             StageNode)
from ..domain.model import Project, StyleOverride, style_overrides, style_table

SCHEMA = 'wv-template@1'
TEMPLATE_KEYS = ('schema', 'template_id', 'version', 'note', 'intro', 'stages',
                 'displays', 'styles')


@dataclass(frozen=True)
class TemplateDocument:
    """One template version: the arrangement, its styles, and a short note."""

    template_id: str
    version: int = 1
    lesson: LessonTemplate = DEFAULT_LESSON_TEMPLATE
    styles: tuple = ()
    note: str = ''

    def __post_init__(self):
        if not isinstance(self.template_id, str) or not self.template_id.strip():
            raise SchemaError('template_id must be a non-empty string', path='template')
        if isinstance(self.version, bool) or not isinstance(self.version, int) \
                or self.version <= 0:
            raise SchemaError('template version must be a positive integer',
                              path='template')
        if not isinstance(self.lesson, LessonTemplate):
            raise SchemaError('a template document needs a LessonTemplate',
                              path='template')
        # A document has exactly one identity, so the lesson it carries may not claim
        # another id or version: ``from_dict`` reads them from the document, and a
        # capture that was handed a reference lesson must still round-trip equal.
        object.__setattr__(self, 'lesson', replace(self.lesson,
                                                   template_id=self.template_id,
                                                   version=self.version))
        object.__setattr__(self, 'styles', style_overrides(self.styles))
        if not isinstance(self.note, str):
            raise SchemaError('template note must be a string', path='template')

    def style_table(self):
        """``{role: {field: value}}``: the overrides this version carries."""
        return style_table(self.styles)

    def to_dict(self):
        return {'schema': SCHEMA, 'template_id': self.template_id,
                'version': self.version, 'note': self.note,
                'intro': bool(self.lesson.intro),
                'stages': [{'role': node.role, 'text_field': node.text_field}
                           for node in self.lesson.stages],
                'displays': [{'role': node.role, 'text_field': node.text_field,
                              'from_stage': node.from_stage}
                             for node in self.lesson.displays],
                'styles': [override.to_dict() for override in self.styles]}

    @classmethod
    def from_dict(cls, document, path='template'):
        """Read one template document; unknown keys and versions are refused."""
        if not isinstance(document, dict):
            raise SchemaError('a template document must be a JSON object', path=path)
        unknown = sorted(set(document) - set(TEMPLATE_KEYS))
        if unknown:
            raise SchemaError('template document has unknown field(s): %s'
                              % ', '.join(unknown), path=path)
        for name in ('template_id', 'version'):
            if name not in document:
                raise SchemaError('template document needs %s' % name, path=path)
        schema = document.get('schema', '')
        if schema != SCHEMA:
            raise SchemaError('unknown template schema %r' % (schema,), path=path,
                              hint='本版本只读 %s' % SCHEMA)
        template_id, version = document['template_id'], document['version']
        stages = _stages(document.get('stages'), path)
        displays = _displays(document.get('displays'), path)
        lesson = LessonTemplate(template_id=template_id, version=version,
                                stages=stages, displays=displays,
                                intro=document.get('intro', False))
        styles = tuple(StyleOverride.from_dict(item, path)
                       for item in document.get('styles', []))
        return cls(template_id=template_id, version=version, lesson=lesson,
                   styles=styles, note=document.get('note', ''))

    @property
    def intro(self):
        return bool(self.lesson.intro)


def _stages(value, path):
    if value is None:
        return DEFAULT_LESSON_TEMPLATE.stages
    if not isinstance(value, list):
        raise SchemaError('template stages must be a list', path=path)
    nodes = []
    for item in value:
        if not isinstance(item, dict):
            raise SchemaError('every stage must be an object', path=path)
        unknown = sorted(set(item) - {'role', 'text_field'})
        if unknown:
            raise SchemaError('stage has unknown field(s): %s' % ', '.join(unknown),
                              path=path)
        nodes.append(StageNode(role=item.get('role', ''),
                               text_field=item.get('text_field', '')))
    return tuple(nodes)


def _displays(value, path):
    if value is None:
        return DEFAULT_LESSON_TEMPLATE.displays
    if not isinstance(value, list):
        raise SchemaError('template displays must be a list', path=path)
    nodes = []
    for item in value:
        if not isinstance(item, dict):
            raise SchemaError('every display must be an object', path=path)
        unknown = sorted(set(item) - {'role', 'text_field', 'from_stage'})
        if unknown:
            raise SchemaError('display has unknown field(s): %s' % ', '.join(unknown),
                              path=path)
        nodes.append(DisplayNode(role=item.get('role', ''),
                                 text_field=item.get('text_field', ''),
                                 from_stage=item.get('from_stage', '')))
    return tuple(nodes)


def document_from_project(project, *, template_id='', version=1, note=''):
    """Capture the parts of a project a template can carry, without guessing.

    The intro layer and the style overrides are what a member can change today, so
    those are what is captured.  The stage/display *wiring* has no edit command, so
    the reference wiring is kept rather than recovered from clip text — reading a
    field back out of a rendered string would be a guess, and a guess here would
    silently rewire a lesson.
    """
    if not isinstance(project, Project):
        raise SchemaError('a template needs a Project to capture', path='project')
    lesson = replace(DEFAULT_LESSON_TEMPLATE, intro=project.intro_clip() is not None)
    return TemplateDocument(template_id=template_id or DEFAULT_LESSON_TEMPLATE.template_id,
                            version=version, lesson=lesson, styles=project.styles,
                            note=note)
