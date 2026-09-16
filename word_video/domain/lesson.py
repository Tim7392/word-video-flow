"""The declarative lesson template.

A template says *what one record produces* — which speech stages, which text
layers, which record field each of them carries — and nothing else: no code, no
expressions to evaluate (architecture §7 forbids executable templates).  The
concrete lesson rhythm lives in :mod:`word_video.domain.rhythm` and the expansion
itself in :mod:`word_video.application.instantiate`, which runs only when a
project is created or explicitly upgraded.  Opening or solving a project never
re-expands it, so the project keeps whatever the user edited.
"""
from dataclasses import dataclass

from ..contracts import DISPLAY_TRACKS, ROLES
from .errors import SchemaError
from .model import DISPLAY_LAYERS, TEACHING_STAGES

RECORD_FIELDS = ('word', 'phonetic', 'meaning', 'spoken_meaning')


@dataclass(frozen=True)
class StageNode:
    """One speech stage of a record: its role and the field it speaks."""

    role: str
    text_field: str

    def __post_init__(self):
        if self.role not in TEACHING_STAGES:
            raise SchemaError('unknown stage role %r' % (self.role,), path='template')
        if self.text_field not in RECORD_FIELDS:
            raise SchemaError('unknown record field %r' % (self.text_field,),
                              path='template')


@dataclass(frozen=True)
class DisplayNode:
    """One on-screen text layer; it starts with ``from_stage`` and ends with the word."""

    role: str
    text_field: str
    from_stage: str

    def __post_init__(self):
        if self.role not in DISPLAY_LAYERS:
            raise SchemaError('unknown display role %r' % (self.role,), path='template')
        if self.text_field not in RECORD_FIELDS:
            raise SchemaError('unknown record field %r' % (self.text_field,),
                              path='template')
        if self.from_stage not in TEACHING_STAGES:
            raise SchemaError('display layer must start with a teaching stage',
                              path='template')


@dataclass(frozen=True)
class LessonTemplate:
    """A versioned arrangement of stages and layers for one record."""

    template_id: str = 'lesson-default'
    version: int = 1
    stages: tuple = ()
    displays: tuple = ()

    def __post_init__(self):
        if not isinstance(self.template_id, str) or not self.template_id:
            raise SchemaError('template_id must be a non-empty string', path='template')
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise SchemaError('template version must be a positive integer',
                              path='template')
        object.__setattr__(self, 'stages', tuple(self.stages))
        object.__setattr__(self, 'displays', tuple(self.displays))
        for node in self.stages:
            if not isinstance(node, StageNode):
                raise SchemaError('stages must contain StageNode objects', path='template')
        for node in self.displays:
            if not isinstance(node, DisplayNode):
                raise SchemaError('displays must contain DisplayNode objects',
                                  path='template')
        # The teaching order is a business rule, not a template preference.
        if tuple(node.role for node in self.stages) != TEACHING_STAGES:
            raise SchemaError('stages must be the teaching order %s'
                              % (' → '.join(TEACHING_STAGES),), path='template',
                              hint='女声英文 → 男声英文 → 中文释义')
        if tuple(node.role for node in self.displays) != tuple(DISPLAY_TRACKS):
            raise SchemaError('displays must cover %s' % ', '.join(DISPLAY_TRACKS),
                              path='template')

    def stage(self, role):
        for node in self.stages:
            if node.role == role:
                return node
        return None


#: The reference lesson layout: two English readings then the Chinese meaning, with
#: the English text on screen for the whole word, the phonetic from the male
#: reading and the meaning from the Chinese reading — the same picture the
#: verified engine draws (see ``word_video/template.py::text_events``).
DEFAULT_LESSON_TEMPLATE = LessonTemplate(
    template_id='lesson-default',
    version=1,
    stages=(StageNode('female', 'word'),
            StageNode('male', 'word'),
            StageNode('chinese', 'spoken_meaning')),
    displays=(DisplayNode('english', 'word', 'female'),
              DisplayNode('phonetic', 'phonetic', 'male'),
              DisplayNode('meaning', 'meaning', 'chinese')),
)
