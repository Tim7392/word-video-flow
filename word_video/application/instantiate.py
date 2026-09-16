"""Template expansion: the one place a template becomes concrete clips.

Expansion happens when a project is created (here) or explicitly upgraded, never
when a project is opened or solved.  Every expanded clip therefore gets an
absolute start on the project grid: moving one clip later cannot silently drag
its neighbours, and linking two clips is an explicit user command
(:class:`~word_video.application.commands.BindStart`).  A missing voice is a hard
error — an estimated duration never becomes a formal plan.

Clip ids are stable (``<record_id>.<role>``), which is what lets a later
template upgrade match the clips a user already edited.
"""
from ..domain.compile import media_table
from ..domain.errors import SchemaError, UnknownDurationError
from ..domain.lesson import LessonTemplate
from ..domain.model import Clip, MediaSlice, Project, check_duplicate_ids
from ..domain.rhythm import stage_duration_ticks
from ..domain.timebase import TimeExpr


def asset_id_for(record_id, role, asset_ids=None):
    """Stable media identity of one stage; W02's cache maps it to a real file."""
    if asset_ids:
        chosen = asset_ids.get((record_id, role))
        if chosen:
            return chosen
    return '%s:%s' % (record_id, role)


def expand(template, records, media, base, *, background=None, asset_ids=None):
    """Expand ``template`` for ``records`` into clips placed on ``base``'s grid.

    Split out from :func:`instantiate` so a later explicit template upgrade can
    reuse the same rhythm and the same ids instead of writing a second expander.
    """
    if not isinstance(template, LessonTemplate):
        raise SchemaError('instantiate needs a LessonTemplate', path='template')
    if not isinstance(base, Project):
        raise SchemaError('instantiate needs a Project as its settings carrier',
                          path='project')
    records = tuple(records)
    if not records:
        raise SchemaError('a lesson needs at least one record', path='project')
    check_duplicate_ids(records, ())
    table = media_table(media)
    base.frame_ticks  # refuse a frame rate the tick base cannot express
    cursor = base.intro_ticks
    clips = []
    for record in records:
        phone = 'record:%s' % record.id
        stages = {}
        for node in template.stages:
            asset_id = asset_id_for(record.id, node.role, asset_ids)
            info = table.get(asset_id)
            if info is None:
                raise UnknownDurationError(
                    'media %r for %s of %s has not been measured' % (asset_id,
                                                                     node.role, record.id),
                    path=phone,
                    hint='先准备并探测该配音，再实例化工程；估算时长不能生成计划')
            source = MediaSlice(asset_id=asset_id, source_start=0, source_end=info.units,
                                unit_num=info.unit_num, unit_den=info.unit_den,
                                speed=base.speed)
            duration = stage_duration_ticks(
                node.role, record, source.seconds, gap_s=base.gap_s, speed=base.speed,
                first_six=base.first_six, extra=base.extra,
                fps_num=base.fps_num, fps_den=base.fps_den)
            clips.append(Clip(id='%s.%s' % (record.id, node.role), role=node.role,
                              record_id=record.id, start=TimeExpr.at(cursor),
                              duration_ticks=duration,
                              text=record.field(node.text_field), source=source))
            stages[node.role] = (cursor, cursor + duration)
            cursor += duration
        block_end = cursor
        for node in template.displays:
            start = stages[node.from_stage][0]
            clips.append(Clip(id='%s.%s' % (record.id, node.role), role=node.role,
                              record_id=record.id, start=TimeExpr.at(start),
                              duration_ticks=block_end - start,
                              text=record.field(node.text_field)))
    total = cursor
    first, last = _numbering(records)
    clips.append(Clip(id='layer.title', role='title', start=TimeExpr.at(0),
                      duration_ticks=total, text=base.title))
    clips.append(Clip(id='layer.subtitle', role='subtitle', start=TimeExpr.at(0),
                      duration_ticks=total,
                      text='速通（%d–%d）' % (first, last)))
    clips.append(Clip(id='layer.footer', role='footer', start=TimeExpr.at(0),
                      duration_ticks=total, text=base.footer))
    if background is not None:
        if not isinstance(background, MediaSlice):
            raise SchemaError('background must be a MediaSlice', path='request')
        clips.append(Clip(id='layer.background', role='background',
                          start=TimeExpr.at(0), duration_ticks=total,
                          source=background))
    return base.with_records(records).with_clips(clips)


def _numbering(records):
    """First/last word number for the on-screen subtitle, from the source list."""
    if all(record.index > 0 for record in records):
        return records[0].index, records[-1].index
    return 1, len(records)


def instantiate(template, records, media, base, *, background=None, asset_ids=None):
    """Create a new project from a template; only valid on an empty document."""
    if not isinstance(base, Project):
        raise SchemaError('instantiate needs a Project as its settings carrier',
                          path='project')
    if base.clips or base.records:
        raise SchemaError('instantiate only expands into an empty project',
                          path='project',
                          hint='模板展开只发生在创建或显式升级时，不会覆盖已有片段')
    return expand(template, records, media, base, background=background,
                  asset_ids=asset_ids)
