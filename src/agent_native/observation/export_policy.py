"""C4 consumer of the ONE borrowed C1 projector and owned paint provenance.

No Registry/schema policy is duplicated here. The finite first subset refuses
an entire surface when any dynamic dependency is denied or unproven. It does
not pretend that masking text removes word-wrap/layout/membership leakage.
"""
from dataclasses import dataclass
from queue import SimpleQueue

from ._export_types import ExportError, _owner


@dataclass(frozen=True, slots=True)
class _PaintDecision:
    traces: tuple


@dataclass(frozen=True, slots=True)
class _CpuCompletion:
    ok: bool
    encoded: bytes = b""


def _authorize_pixels(index, cut, request, resource):
    """Borrow one guarded snapshot; return detached semantic claims only."""
    _owner()
    if (resource.stamp.cut != cut.identity or resource.stamp.derived_revision != cut.derived_revision
            or resource.stamp.capture_target != "managed-content"
            or resource.stamp.view != cut.view.field(request.focus,"view")):
        raise ExportError()
    from agent_native.context._projection import Selector
    trace = None
    with index.projection(cut,request) as projector:
        for captured in resource.mappings:
            node = captured.node
            if not node.rendered:
                continue
            if not node.provenance:
                raise ExportError()
            for source in node.provenance:
                if source.kind == "literal":
                    if type(source.author_literal) is not str:
                        raise ExportError()
                    # Bound command UI metadata may not launder a hidden command
                    # into a supposedly public author literal.
                    if node.binding is not None:
                        command = cut.view.field(node.binding,"command_id")
                        if command is not None:
                            plan = projector.inspect(Selector("command",command))
                            if not any(row.get("name")==command for row in plan.payload["schemas"]):
                                raise ExportError()
                            trace = plan.projection_trace
                    continue
                if source.kind == "extension":
                    decision=projector.extension(source.source)
                    if not decision.allowed or decision.trace is None:raise ExportError()
                    trace=decision.trace
                    continue
                route = projector.route_for(source.source)
                if route is None:
                    raise ExportError()
                if source.kind == "membership":
                    decision = projector.membership(source.source,via=route)
                elif source.kind == "draft":
                    decision = projector.draft(source.semantic_ref,resource.stamp.view,via=route)
                elif source.kind == "field" and source.fields:
                    decision = None
                    for name in source.fields:
                        decision = projector.field(source.source,name,via=route,purpose="provenance")
                        if not decision.allowed:
                            raise ExportError()
                else:
                    raise ExportError()
                if decision is None or not decision.allowed or decision.trace is None:
                    raise ExportError()
                trace = decision.trace
        # Projector methods accumulate claims. The final issued trace includes
        # every checked field/member/draft, without retaining N cumulative copies.
        return _PaintDecision(() if trace is None else (trace,))


def _png_bound(width,height):
    # Filter bytes + conservative zlib/chunk/Qt metadata overhead. The image is
    # an SDK-created RGBA surface without caller text/ICC/image attachments.
    n = height * (4*width+1)
    return n + (n>>12) + (n>>14) + (n>>25) + 1024*1024


def _encode_approved(image, bound, mailbox):
    """CPU worker. Enqueue ONE immutable completion; Future retains no pixels.

    All native input/output reservations were pinned by the owner BEFORE submit.
    The owner closes them only after this function actually returns, including
    cancelled requests. No logger, callbacks, file, cache or transport is used.
    """
    from PySide6.QtCore import QByteArray,QBuffer,QIODevice
    copied = array = buffer = encoded = completion = None
    try:
        # Explicit approved variant. No implicit-shared raw owner enters cache.
        copied = image.copy()
        if copied.isNull():
            raise ValueError()
        array = QByteArray()
        buffer = QBuffer(array)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not copied.save(buffer,"PNG"):
            raise ValueError()
        buffer.close()
        if array.size() > bound:
            raise ValueError()
        encoded = bytes(array)
        completion = _CpuCompletion(True,encoded)
    except BaseException as error:
        _scrub_failure(error)
        completion = _CpuCompletion(False)
    finally:
        copied = array = buffer = encoded = image = None
    mailbox.put(completion)
    completion = None
    return None


def _cpu_entry(input_mailbox):
    # Executor arguments retain only an emptied mailbox, never raw QImage.
    payload = input_mailbox.get()
    try:
        _encode_approved(payload[0],payload[1],payload[2])
    except BaseException as error:
        _scrub_failure(error)
        payload[2].put(_CpuCompletion(False))
    finally:
        payload = None
    return None


def _crop_entry(input_mailbox):
    """Decode/crop ONLY an already-approved retained variant, never raw/current UI."""
    from PySide6.QtGui import QImage
    payload=input_mailbox.get()
    image=cropped=None
    try:
        encoded,region,shape,bound,mailbox=payload
        image=QImage.fromData(encoded,"PNG")
        if image.isNull() or (image.width(),image.height())!=shape:
            raise ValueError()
        cropped=image.copy(*region)
        if cropped.isNull() or (cropped.width(),cropped.height())!=region[2:]:
            raise ValueError()
        _encode_approved(cropped,bound,mailbox)
    except BaseException as error:
        _scrub_failure(error)
        payload[-1].put(_CpuCompletion(False))
    finally:
        image=cropped=payload=None
        encoded=region=shape=bound=mailbox=None
    return None


def _scrub_failure(error):
    """Discard private worker failures without retaining native aliases in traces."""
    import traceback
    pending=[error];seen=set()
    while pending:
        item=pending.pop()
        if id(item)in seen:
            continue
        seen.add(id(item))
        if item.__cause__ is not None:pending.append(item.__cause__)
        if item.__context__ is not None:pending.append(item.__context__)
        if isinstance(item,BaseExceptionGroup):pending.extend(item.exceptions)
        traceback.clear_frames(item.__traceback__)
        item.__traceback__=item.__cause__=item.__context__=None
        item.__dict__.clear()
