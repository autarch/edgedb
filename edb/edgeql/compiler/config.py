#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""CONFIGURE statement compilation functions."""


from __future__ import annotations
from typing import *

import json

from edb import errors

from edb.edgeql import qltypes

from edb.ir import ast as irast
from edb.ir import staeval as ireval
from edb.ir import typeutils as irtyputils

from edb.schema import links as s_links
from edb.schema import name as sn
from edb.schema import objtypes as s_objtypes
from edb.schema import pointers as s_pointers
from edb.schema import types as s_types
from edb.schema import utils as s_utils

from edb.edgeql import ast as qlast

from . import context
from . import dispatch
from . import setgen


class SettingInfo(NamedTuple):
    param_name: str
    param_type: s_types.Type
    cardinality: qltypes.SchemaCardinality
    requires_restart: bool
    backend_setting: str
    affects_compilation: bool


@dispatch.compile.register
def compile_ConfigSet(
    expr: qlast.ConfigSet, *,
    ctx: context.ContextLevel,
) -> irast.Set:

    info = _validate_op(expr, ctx=ctx)
    param_val = setgen.ensure_set(
        dispatch.compile(expr.expr, ctx=ctx), ctx=ctx)

    try:
        ireval.evaluate(param_val, schema=ctx.env.schema)
    except ireval.UnsupportedExpressionError as e:
        raise errors.QueryError(
            f'non-constant expression in CONFIGURE {expr.scope} SET',
            context=expr.expr.context
        ) from e

    config_set = irast.ConfigSet(
        name=info.param_name,
        cardinality=info.cardinality,
        scope=expr.scope,
        requires_restart=info.requires_restart,
        backend_setting=info.backend_setting,
        context=expr.context,
        expr=param_val,
    )
    return setgen.ensure_set(config_set, ctx=ctx)


@dispatch.compile.register
def compile_ConfigReset(
    expr: qlast.ConfigReset, *,
    ctx: context.ContextLevel,
) -> irast.Set:

    info = _validate_op(expr, ctx=ctx)
    filter_expr = expr.where
    select_ir = None

    if not info.param_type.is_object_type() and filter_expr is not None:
        raise errors.QueryError(
            'RESET of a primitive configuration parameter '
            'must not have a FILTER clause',
            context=expr.context,
        )

    elif isinstance(info.param_type, s_objtypes.ObjectType):
        param_type_name = info.param_type.get_name(ctx.env.schema)
        param_type_ref = qlast.ObjectRef(
            name=param_type_name.name,
            module=param_type_name.module,
        )
        select = qlast.SelectQuery(
            result=qlast.Shape(
                expr=qlast.Path(steps=[param_type_ref]),
                elements=s_utils.get_config_type_shape(
                    ctx.env.schema, info.param_type, path=[param_type_ref]),
            ),
            where=filter_expr,
        )

        ctx.modaliases[None] = 'cfg'
        select_ir = setgen.ensure_set(
            dispatch.compile(select, ctx=ctx), ctx=ctx)

    config_reset = irast.ConfigReset(
        name=info.param_name,
        cardinality=info.cardinality,
        scope=expr.scope,
        requires_restart=info.requires_restart,
        backend_setting=info.backend_setting,
        context=expr.context,
        selector=select_ir,
    )
    return setgen.ensure_set(config_reset, ctx=ctx)


@dispatch.compile.register
def compile_ConfigInsert(
        expr: qlast.ConfigInsert, *, ctx: context.ContextLevel) -> irast.Set:

    info = _validate_op(expr, ctx=ctx)

    if expr.scope is not qltypes.ConfigScope.INSTANCE:
        raise errors.UnsupportedFeatureError(
            f'CONFIGURE {expr.scope} INSERT is not supported'
        )

    subject = ctx.env.get_track_schema_object(
        sn.QualName('cfg', expr.name.name), expr.name, default=None)
    if subject is None:
        raise errors.ConfigurationError(
            f'{expr.name.name!r} is not a valid configuration item',
            context=expr.context,
        )

    insert_stmt = qlast.InsertQuery(
        subject=qlast.Path(
            steps=[
                qlast.ObjectRef(
                    name=expr.name.name,
                    module='cfg',
                )
            ]
        ),
        shape=expr.shape,
    )

    for el in expr.shape:
        if isinstance(el.compexpr, qlast.InsertQuery):
            _inject_tname(el.compexpr, ctx=ctx)

    with ctx.newscope(fenced=False) as subctx:
        subctx.expr_exposed = True
        subctx.modaliases = ctx.modaliases.copy()
        subctx.modaliases[None] = 'cfg'
        subctx.special_computables_in_mutation_shape |= {'_tname'}
        insert_ir = dispatch.compile(insert_stmt, ctx=subctx)
        insert_ir_set = setgen.ensure_set(insert_ir, ctx=subctx)
        assert isinstance(insert_ir_set.expr, irast.InsertStmt)
        insert_subject = insert_ir_set.expr.subject

        _validate_config_object(insert_subject, scope=expr.scope, ctx=subctx)

    return setgen.ensure_set(
        irast.ConfigInsert(
            name=info.param_name,
            cardinality=info.cardinality,
            scope=expr.scope,
            requires_restart=info.requires_restart,
            backend_setting=info.backend_setting,
            expr=insert_subject,
            context=expr.context,
        ),
        ctx=ctx,
    )


def _inject_tname(
        insert_stmt: qlast.InsertQuery, *,
        ctx: context.ContextLevel) -> None:

    for el in insert_stmt.shape:
        if isinstance(el.compexpr, qlast.InsertQuery):
            _inject_tname(el.compexpr, ctx=ctx)

    assert isinstance(insert_stmt.subject.steps[0], qlast.BaseObjectRef)
    insert_stmt.shape.append(
        qlast.ShapeElement(
            expr=qlast.Path(
                steps=[qlast.Ptr(ptr=qlast.ObjectRef(name='_tname'))],
            ),
            compexpr=qlast.Path(
                steps=[
                    qlast.Introspect(
                        type=qlast.TypeName(
                            maintype=insert_stmt.subject.steps[0],
                        ),
                    ),
                    qlast.Ptr(ptr=qlast.ObjectRef(name='name')),
                ],
            ),
        ),
    )


def _validate_config_object(
        expr: irast.Set, *,
        scope: str,
        ctx: context.ContextLevel) -> None:

    for element, _ in expr.shape:
        assert element.rptr is not None
        if element.rptr.ptrref.shortname.name == 'id':
            continue

        if (irtyputils.is_object(element.typeref)
                and isinstance(element.expr, irast.InsertStmt)):
            _validate_config_object(element, scope=scope, ctx=ctx)


def _validate_op(
        expr: qlast.ConfigOp, *,
        ctx: context.ContextLevel) -> SettingInfo:

    if expr.name.module and expr.name.module != 'cfg':
        raise errors.QueryError(
            'invalid configuration parameter name: module must be either '
            '\'cfg\' or empty', context=expr.name.context,
        )

    name = expr.name.name
    cfg_host_type = ctx.env.get_track_schema_type(
        sn.QualName('cfg', 'AbstractConfig'))
    assert isinstance(cfg_host_type, s_objtypes.ObjectType)
    cfg_type = None

    if isinstance(expr, (qlast.ConfigSet, qlast.ConfigReset)):
        # expr.name is the actual name of the property.
        ptr = cfg_host_type.maybe_get_ptr(ctx.env.schema, sn.UnqualName(name))
        if ptr is not None:
            cfg_type = ptr.get_target(ctx.env.schema)

    if cfg_type is None:
        if isinstance(expr, qlast.ConfigSet):
            raise errors.ConfigurationError(
                f'unrecognized configuration parameter {name!r}',
                context=expr.context
            )

        # expr.name is the name of the configuration type
        cfg_type = ctx.env.get_track_schema_type(
            sn.QualName('cfg', name), default=None)
        if cfg_type is None:
            raise errors.ConfigurationError(
                f'unrecognized configuration object {name!r}',
                context=expr.context
            )

        assert isinstance(cfg_type, s_objtypes.ObjectType)
        ptr_candidate: Optional[s_pointers.Pointer] = None

        mro = [cfg_type] + list(
            cfg_type.get_ancestors(ctx.env.schema).objects(ctx.env.schema))
        for ct in mro:
            ptrs = ctx.env.schema.get_referrers(
                ct, scls_type=s_links.Link, field_name='target')

            if ptrs:
                pointer_link = next(iter(ptrs))
                assert isinstance(pointer_link, s_links.Link)
                ptr_candidate = pointer_link
                break

        if (
            ptr_candidate is None
            or (ptr_source := ptr_candidate.get_source(ctx.env.schema)) is None
            or not ptr_source.issubclass(ctx.env.schema, cfg_host_type)
        ):
            raise errors.ConfigurationError(
                f'{name!r} cannot be configured directly'
            )

        ptr = ptr_candidate

        name = ptr.get_shortname(ctx.env.schema).name

    assert isinstance(ptr, s_pointers.Pointer)

    sys_attr = ptr.get_annotations(ctx.env.schema).get(
        ctx.env.schema, sn.QualName('cfg', 'system'), None)

    system = (
        sys_attr is not None
        and sys_attr.get_value(ctx.env.schema) == 'true'
    )

    cardinality = ptr.get_cardinality(ctx.env.schema)
    assert cardinality is not None

    restart_attr = ptr.get_annotations(ctx.env.schema).get(
        ctx.env.schema, sn.QualName('cfg', 'requires_restart'), None)

    requires_restart = (
        restart_attr is not None
        and restart_attr.get_value(ctx.env.schema) == 'true'
    )

    backend_attr = ptr.get_annotations(ctx.env.schema).get(
        ctx.env.schema, sn.QualName('cfg', 'backend_setting'), None)

    if backend_attr is not None:
        backend_setting = json.loads(backend_attr.get_value(ctx.env.schema))
    else:
        backend_setting = None

    compilation_attr = ptr.get_annotations(ctx.env.schema).get(
        ctx.env.schema, sn.QualName('cfg', 'affects_compilation'), None)

    if compilation_attr is not None:
        affects_compilation = (
            json.loads(compilation_attr.get_value(ctx.env.schema)))
    else:
        affects_compilation = False

    if system and expr.scope is not qltypes.ConfigScope.INSTANCE:
        raise errors.ConfigurationError(
            f'{name!r} is a system-level configuration parameter; '
            f'use "CONFIGURE INSTANCE"')

    return SettingInfo(param_name=name,
                       param_type=cfg_type,
                       cardinality=cardinality,
                       requires_restart=requires_restart,
                       backend_setting=backend_setting,
                       affects_compilation=affects_compilation)
