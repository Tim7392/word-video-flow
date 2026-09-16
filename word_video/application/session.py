"""An editing session: revision, undo/redo and the view mode.

Undo and redo produce a *new* revision carrying old content, so version numbers
never go backwards (architecture §10) and a job that froze revision N is never
confused with the reverted document.  The template/advanced mode lives here and
not in the document: switching mode changes what the UI offers and nothing about
the project, which is what makes "模式切换不改业务数据" checkable.
"""
from dataclasses import dataclass, replace

from ..domain.errors import SchemaError
from ..domain.model import Project
from . import commands as command_module

MODE_TEMPLATE = 'template'
MODE_ADVANCED = 'advanced'
MODES = (MODE_TEMPLATE, MODE_ADVANCED)


@dataclass(frozen=True)
class Session:
    """The editable state around one project: history, redo stack and mode."""

    project: Project
    mode: str = MODE_TEMPLATE
    history: tuple = ()
    future: tuple = ()

    def __post_init__(self):
        if not isinstance(self.project, Project):
            raise SchemaError('a session needs a Project', path='project')
        if self.mode not in MODES:
            raise SchemaError('unknown mode %r' % (self.mode,), path='session',
                              hint='可用模式：%s' % '、'.join(MODES))
        object.__setattr__(self, 'history', tuple(self.history))
        object.__setattr__(self, 'future', tuple(self.future))

    # -- queries ---------------------------------------------------------
    @property
    def can_undo(self):
        return bool(self.history)

    @property
    def can_redo(self):
        return bool(self.future)

    @property
    def revision(self):
        return self.project.revision

    # -- editing ---------------------------------------------------------
    def apply(self, command, expected_revision=None):
        """Apply one command; the previous state becomes the undo entry."""
        result = command_module.apply(self.project, command, expected_revision)
        if not result.changed:
            return replace(self, project=result.project)
        return replace(self, project=result.project,
                       history=self.history + (self.project,), future=())

    def with_mode(self, mode):
        """Switch template/advanced view; the project is passed through untouched."""
        if mode not in MODES:
            raise SchemaError('unknown mode %r' % (mode,), path='session',
                              hint='可用模式：%s' % '、'.join(MODES))
        return replace(self, mode=mode)

    def undo(self):
        if not self.history:
            return self
        previous = self.history[-1]
        restored = replace(previous, revision=self.project.revision + 1)
        return replace(self, project=restored, history=self.history[:-1],
                       future=(self.project,) + self.future)

    def redo(self):
        if not self.future:
            return self
        following = self.future[0]
        restored = replace(following, revision=self.project.revision + 1)
        return replace(self, project=restored, history=self.history + (self.project,),
                       future=self.future[1:])

    # -- persistence -----------------------------------------------------
    def save(self, path):
        from ..storage.project_store import save_project
        return save_project(self.project, path)

    @classmethod
    def open(cls, path, mode=MODE_TEMPLATE):
        from ..storage.project_store import load_project
        return cls(project=load_project(path), mode=mode)
