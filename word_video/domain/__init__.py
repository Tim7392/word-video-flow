"""Project domain: one editable project, its time base and its derived plans.

Import surface for consumers (W02 media, W03 exporters, W04 preview, W05
storage/CLI, QA):
``Project``/``Record``/``Clip``/``MediaSlice``/``MediaInfo``/``TimeExpr`` are the
editable truth, ``RenderPlan``/``CuePlan`` the read-only derivations, and
``solve``/``render_plan``/``cue_plan`` produce them.
"""
from .compile import (IntroMeasurement, Solution, cue_plan, render_plan, resolve,
                      solve, sounding_items, speech_overlaps)
from .errors import (AmbiguousRoleError, ClipBoundError, CycleError,
                     DanglingRefError, DuplicateIdError, IntroMediaError,
                     InvalidTimeError, MissingRoleError, ProjectError, SchemaError,
                     SourceRangeError, StaleRevisionError, TeachingOrderError,
                     TeachingOverlapError, UnknownCommandError,
                     UnknownDurationError, UnsupportedRateError)
from .lesson import DEFAULT_LESSON_TEMPLATE, DisplayNode, LessonTemplate, StageNode
from .model import (AUDIO_ROLES, CLIP_ROLES, DISPLAY_LAYERS, INTRO_ROLE,
                    PROJECT_LAYERS, SCHEMA, SCHEMA_V1, SCHEMAS, TEACHING_STAGES,
                    VIDEO_ORDER, Clip, MediaInfo, MediaSlice, Project, Record,
                    check_duplicate_ids)
from .plan import (CUE_ROLES, CUE_TRACKS, Conflict, Cue, CuePlan, PlanItem,
                   PlanSound, RenderPlan)
from .timebase import (TICKS_PER_SECOND, TimeExpr, ticks_to_microseconds,
                       ticks_to_milliseconds)

__all__ = [
    'AUDIO_ROLES', 'AmbiguousRoleError', 'CLIP_ROLES', 'CUE_ROLES', 'CUE_TRACKS',
    'Clip', 'ClipBoundError', 'Conflict', 'Cue', 'CuePlan', 'CycleError',
    'DEFAULT_LESSON_TEMPLATE', 'DISPLAY_LAYERS', 'DanglingRefError', 'DisplayNode',
    'DuplicateIdError', 'INTRO_ROLE', 'IntroMeasurement', 'IntroMediaError',
    'InvalidTimeError', 'LessonTemplate', 'MediaInfo',
    'MediaSlice', 'MissingRoleError', 'PROJECT_LAYERS', 'PlanItem', 'PlanSound',
    'Project', 'ProjectError', 'Record', 'RenderPlan', 'SCHEMA', 'SCHEMA_V1',
    'SCHEMAS', 'SchemaError', 'Solution',
    'SourceRangeError', 'StageNode', 'StaleRevisionError', 'TICKS_PER_SECOND',
    'TEACHING_STAGES', 'TeachingOrderError', 'TeachingOverlapError', 'TimeExpr',
    'UnknownCommandError', 'UnknownDurationError', 'UnsupportedRateError',
    'VIDEO_ORDER', 'check_duplicate_ids', 'cue_plan', 'render_plan', 'resolve',
    'solve', 'sounding_items', 'speech_overlaps', 'ticks_to_microseconds',
    'ticks_to_milliseconds',
]
