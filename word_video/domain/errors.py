"""Structured project errors: stable code, object path and a usable next step.

The CLI (W05) and the desktop (W06) both surface these, so every error carries a
machine-readable ``code``, the ``object_path`` it is about (``clip:w1.male``,
``record:w1``, ``project``) and a short ``hint`` that names a way forward.  No
error message is used as a control channel.
"""


class ProjectError(Exception):
    """Base class for every refused project operation."""

    code = 'PROJECT_ERROR'

    def __init__(self, message, *, path='', hint=''):
        super().__init__(message)
        self.message = message
        self.path = path
        self.hint = hint

    def to_dict(self):
        return {'code': self.code, 'message': self.message,
                'object_path': self.path, 'hint': self.hint}

    def __str__(self):
        text = self.message
        if self.path:
            text = '%s [%s]' % (text, self.path)
        if self.hint:
            text = '%s (%s)' % (text, self.hint)
        return text


class SchemaError(ProjectError):
    """Wrong document kind/version, unknown field or malformed value."""

    code = 'SCHEMA'


class DuplicateIdError(ProjectError):
    code = 'DUPLICATE_ID'


class DanglingRefError(ProjectError):
    code = 'DANGLING_REF'


class CycleError(ProjectError):
    code = 'CYCLE'


class InvalidTimeError(ProjectError):
    code = 'INVALID_TIME'


class UnsupportedRateError(ProjectError):
    """A frame rate that the 720000 tick/s base cannot express exactly."""

    code = 'UNSUPPORTED_RATE'


class UnknownDurationError(ProjectError):
    """A clip whose length depends on media that has not been measured."""

    code = 'UNKNOWN_DURATION'


class SourceRangeError(ProjectError):
    """A source window outside the measured media, or empty."""

    code = 'SOURCE_RANGE'


class IntroMediaError(ProjectError):
    """The intro layer's media is missing, unusable, or not the one measured."""

    code = 'INTRO_MEDIA'


class MissingAssetError(ProjectError):
    """No registered asset carries the id a clip names."""

    code = 'MISSING_ASSET'


class DuplicateAssetError(ProjectError):
    """Two registered assets carry the same id."""

    code = 'DUPLICATE_ASSET'


class AssetFileError(ProjectError):
    """A registered asset's file is not where the registry says it is."""

    code = 'ASSET_FILE_MISSING'


class MissingRoleError(ProjectError):
    code = 'MISSING_ROLE'


class AmbiguousRoleError(ProjectError):
    """More than one clip claims the same semantic role of one record."""

    code = 'ROLE_AMBIGUOUS'


class TeachingOrderError(ProjectError):
    code = 'TEACHING_ORDER'


class TeachingOverlapError(ProjectError):
    """Two speech clips of the teaching delivery would sound at the same time."""

    code = 'TEACHING_OVERLAP'


class ClipBoundError(ProjectError):
    """An operation that needs an absolute clip start, on a linked clip."""

    code = 'CLIP_BOUND'


class SplitNotAllowedError(ProjectError):
    """A clip whose role cannot be cut into two clips.

    Raised before anything changes, so a UI that asks
    :func:`word_video.application.commands.can_split` first never offers a button
    that can only fail.
    """

    code = 'SPLIT_NOT_APPLICABLE'


class StaleRevisionError(ProjectError):
    code = 'STALE_REVISION'


class UnknownCommandError(ProjectError):
    code = 'UNKNOWN_COMMAND'
