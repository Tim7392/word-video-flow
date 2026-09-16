"""Exporters: the three deliverables, from one solved plan.

``plan``
    projects a :class:`~word_video.domain.plan.RenderPlan` onto the verified
    manifest the MP4 renderer and the draft exporter consume, and refuses a plan
    the manifest cannot express exactly.
``ass``
    burn-in captions positioned by :class:`word_video.layout.LayoutSurface`, so
    the picture and the preview share the geometry.
``srt``
    the five teaching subtitle tracks straight from a ``CuePlan``.
``catalog``
    where a project's assets live and what they measure (the exact
    ``MediaInfo`` grid).
``render``
    the one command: ``project.json`` + catalogue -> MP4 + five SRT + editable
    draft, over a single timeline.
"""
from .ass import build_layout_ass, layout_for, write_ass
from .catalog import (CATALOG_FILENAME, Asset, CatalogError, load_catalog, measure,
                      measure_all, save_catalog)
from .plan import (ManifestAudio, ProjectionError, SourceMedia, TimelineView,
                   build_manifest, frame_at, verify_manifest, word_blocks)
from .render import ExportError, ExportResult, export_run
from .srt import SRT_SUFFIXES, build_tracks, export_srts, verify_against_manifest

__all__ = ['Asset', 'CATALOG_FILENAME', 'CatalogError', 'ExportError', 'ExportResult',
           'ManifestAudio', 'ProjectionError', 'SRT_SUFFIXES', 'SourceMedia',
           'TimelineView', 'build_layout_ass', 'build_manifest', 'build_tracks',
           'export_run', 'export_srts', 'frame_at', 'layout_for', 'load_catalog',
           'measure', 'measure_all', 'save_catalog', 'verify_against_manifest',
           'verify_manifest', 'word_blocks', 'write_ass']
