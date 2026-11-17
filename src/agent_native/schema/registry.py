"""One trusted author definition, compiled into validation and shared projections."""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import inspect
import math
import re
import types
import annotated_types as _metadata
from typing import Annotated, Literal, Union, get_args, get_origin, get_type_hints
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model
from pydantic.fields import FieldInfo
from pydantic_core import SchemaError, SchemaValidator, ValidationError as _CoreValidationError, core_schema

from .descriptors import (DefinitionDescriptor, FieldDescriptor, RelationDescriptor,
                          UiBindingDescriptor, canonical, freeze, thaw)
from .permissions import CommandPermissionProfile
from .types import CollectionRef, EntityKey, Owned, Ref, Resource, ResourceCollection, valid_type_id

_RELATIONS = {Ref: "ref", Owned: "owned", Resource: "resource",
              ResourceCollection: "resource_collection", CollectionRef: "resource_collection"}


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a strict positive integer")
    return value


def _base(annotation):
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _snapshot_annotation(annotation):
    """Detach Annotated/Field metadata before Pydantic retains it in core schemas."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return Annotated[_snapshot_annotation(args[0]), *deepcopy(args[1:])]
    if origin in (list, tuple, dict):
        return origin[tuple(_snapshot_annotation(item) if item is not Ellipsis else item for item in args)]
    if origin in (Union, types.UnionType):
        return Union[tuple(_snapshot_annotation(item) for item in args)]
    return annotation



def _wire_form(annotation):
    """Only representation topology, not arbitrary schema/validator equivalence."""
    annotation = _base(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Ref, Resource):
        return ("object", "ref", None)
    if origin in (CollectionRef, ResourceCollection):
        return ("object", "collection_ref", None)
    if origin is Owned:
        return ("array", "tuple", (Ref[args[0]],), True)
    if origin is list:
        return ("array", "list", args, True)
    if origin is tuple:
        variadic = len(args) == 2 and args[1] is Ellipsis
        return ("array", "tuple", args[:1] if variadic else args, variadic)
    if origin is dict:
        return ("object", "map", args[1])
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return ("object", "record", annotation)
    return ("scalar",)


def _record_keys(model):
    fields = dict(model.__schema_annotations__)
    required = {name for name in fields if model.model_fields[name].is_required()}
    return fields, required


def _scalar_wire_conflict(left, right, *, nested):
    kinds_a = {type(value) for value in get_args(left)} if get_origin(left) is Literal else {left}
    kinds_b = {type(value) for value in get_args(right)} if get_origin(right) is Literal else {right}
    if nested and ((int in kinds_a and float in kinds_b) or (float in kinds_a and int in kinds_b)):
        # Pydantic's JSON tuple/model/map union selection may choose a float branch for an int.
        return True
    if get_origin(left) is Literal or get_origin(right) is Literal:
        numeric = {bool, int, float}
        # Literal equality can accept True as 1 (or 1.0), then return the literal's native type.
        return any(a is not b and a in numeric and b in numeric for a in kinds_a for b in kinds_b)
    return False


def _wire_native_conflict(left, right, *, nested=False):
    """Conservative: reject potentially overlapping wire shapes with different native layouts.

    Direct ordinary scalar unions keep exact types; nested numeric alternatives and
    cross-type numeric Literals conservatively refuse Pydantic branch coercion.
    Ref and CollectionRef have distinct
    exact object keys; two Ref targets share one native leaf representation. Containers
    and normalized model dictionaries are checked recursively, without running validators.
    """
    left, right = _base(left), _base(right)
    if get_origin(left) in (Union, types.UnionType):
        return any(_wire_native_conflict(member, right, nested=nested) for member in get_args(left))
    if get_origin(right) in (Union, types.UnionType):
        return any(_wire_native_conflict(left, member, nested=nested) for member in get_args(right))
    a, b = _wire_form(left), _wire_form(right)
    if a[0] != b[0]:
        return False
    if a[0] == "scalar":
        return _scalar_wire_conflict(left, right, nested=nested)
    if a[0] == "array":
        if a[1] != b[1]:
            return True  # list and tuple/Owned both serialize as JSON arrays, including empty.
        if not a[3] and not b[3]:
            if len(a[2]) != len(b[2]):
                return False
            pairs = zip(a[2], b[2])
        else:
            pairs = ((x, y) for x in a[2] for y in b[2])
        return any(_wire_native_conflict(x, y, nested=True) for x, y in pairs)
    leaves = {"ref", "collection_ref"}
    if a[1] in leaves and b[1] in leaves:
        return False  # Same leaf representation, or disjoint exact object-key sets.
    if a[1] in leaves or b[1] in leaves:
        leaf, other = (a, b) if a[1] in leaves else (b, a)
        if other[1] == "map":
            return True
        fields, required = _record_keys(other[2])
        keys = ({"entity_type", "entity_id", "generation"} if leaf[1] == "ref" else {"owner", "field"})
        return required <= keys and keys <= fields.keys()
    if a[1] == b[1] == "map":
        return _wire_native_conflict(a[2], b[2], nested=True)
    if a[1] == b[1] == "record":
        fields_a, required_a = _record_keys(a[2])
        fields_b, required_b = _record_keys(b[2])
        if not required_a <= fields_b.keys() or not required_b <= fields_a.keys():
            return False  # Exact known object fields discriminate these record alternatives.
        if fields_a.keys() != fields_b.keys():
            return True  # Optional/defaulted fields could change the normalized output shape.
        return any(_wire_native_conflict(fields_a[key], fields_b[key], nested=True) for key in fields_a)
    return True  # Generic map versus record: unsupported overlap, no speculative inference.


def _check_wire_union(members):
    for index, left in enumerate(members):
        for right in members[index + 1:]:
            if _wire_native_conflict(left, right):
                raise TypeError(
                    f"Unsupported ambiguous wire union: {left!r} | {right!r}; "
                    "JSON wire shapes may overlap and lose exact native container/identity type. "
                    "Use separate typed fields or one unambiguous representation.")

def _supported(annotation):
    annotation = _base(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation in (str, bool, int, float, type(None)):
        return
    if origin in _RELATIONS:
        if len(args) != 1 or not hasattr(args[0], "__entity_type__"):
            raise TypeError("Relations require one registered entity target")
        return
    if origin is Literal:
        if not args or any(type(value) not in (str, bool, int, float, type(None)) for value in args):
            raise TypeError("Only finite JSON scalar literals are supported")
        if any(type(value) is float and not math.isfinite(value) for value in args):
            raise TypeError("Non-finite literals are unsupported")
        numeric_kinds = {type(value) for value in args} & {bool, int, float}
        if len(numeric_kinds) > 1:
            raise TypeError("Unsupported ambiguous wire union in Literal: cross-type numeric/bool variants "
                            "can change exact native type during Pydantic literal selection")
        return
    if origin in (Union, types.UnionType, list, tuple, dict):
        if origin is dict and (len(args) != 2 or args[0] is not str):
            raise TypeError("Dictionary keys must be strings")
        if not args:
            raise TypeError("Containers require explicit type arguments")
        for item in (args[1:] if origin is dict else args):
            if item is not Ellipsis:
                _supported(item)
        if origin in (Union, types.UnionType):
            _check_wire_union(args)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and hasattr(annotation, "__record_type__"):
        return
    raise TypeError(f"Unsupported semantic annotation: {annotation!r}")


def _shape(annotation, value, *, wire=False, collection_check=None):
    """Exact scalar/container checks before and after trusted Pydantic validators."""
    annotation = _base(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation in (str, bool, int, float, type(None)):
        if type(value) is not annotation:
            raise TypeError(f"Expected exact {annotation.__name__}, got {type(value).__name__}")
        if annotation is float and not math.isfinite(value):
            raise ValueError("Non-finite floats are unsupported")
        return
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in args):
            raise ValueError("Literal value/type does not match")
        return
    if origin in (Union, types.UnionType):
        for item in args:
            try:
                _shape(item, value, wire=wire, collection_check=collection_check)
                return
            except (TypeError, ValueError):
                pass
        raise TypeError("Value does not match any declared union member")
    if origin in (Ref, Resource):
        if type(value) is Ref:
            key = value.key
        elif type(value) is dict and set(value) == {"entity_type", "entity_id", "generation"}:
            key = EntityKey(**value)
        else:
            raise TypeError("Expected a typed Ref or exact reference wire object")
        if key.entity_type != args[0].__entity_type__:
            raise ValueError("Reference target type mismatch")
        return
    if origin in (ResourceCollection, CollectionRef):
        if type(value) is CollectionRef:
            identity = value
        elif (type(value) is dict and set(value) == {"owner", "field"}
              and type(value["owner"]) is dict
              and set(value["owner"]) == {"entity_type", "entity_id", "generation"}):
            identity = CollectionRef(EntityKey(**value["owner"]), value["field"])
        else:
            raise TypeError("Expected a CollectionRef or exact owner/field wire object, never members")
        if collection_check is not None:
            collection_check(identity, target_type=args[0].__entity_type__)
        return
    if origin is Owned:
        _shape(tuple[Ref[args[0]], ...], value, wire=wire, collection_check=collection_check)
        return
    if origin in (list, tuple):
        expected = list if origin is list or wire else tuple
        if type(value) is not expected:
            raise TypeError(f"Expected exact {expected.__name__}")
        if origin is list or (len(args) == 2 and args[1] is Ellipsis):
            for item in value:
                _shape(args[0], item, wire=wire, collection_check=collection_check)
        else:
            if len(value) != len(args):
                raise ValueError("Tuple arity mismatch")
            for item_type, item in zip(args, value):
                _shape(item_type, item, wire=wire, collection_check=collection_check)
        return
    if origin is dict:
        if type(value) is not dict or any(type(key) is not str for key in value):
            raise TypeError("Expected a dictionary with exact string keys")
        for item in value.values():
            _shape(args[1], item, wire=wire, collection_check=collection_check)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if type(value) is annotation:
            value = {name: getattr(value, name) for name, _ in annotation.__schema_annotations__}
        if type(value) is not dict:
            raise TypeError("Expected a nested record object")
        _fields_shape(annotation.__schema_annotations__, value, wire=wire, collection_check=collection_check)
        return
    raise TypeError("Unsupported semantic value")


def _fields_shape(fields, value, *, wire=False, collection_check=None):
    if type(value) is not dict:
        raise TypeError("Expected an exact object dictionary")
    names = {name for name, _ in fields}
    if set(value) - names:
        raise ValueError(f"Unknown fields: {sorted(set(value) - names)!r}")
    for name, annotation in fields:
        if name in value:
            _shape(annotation, value[name], wire=wire, collection_check=collection_check)


def _detach(value):
    if type(value) in (Ref, CollectionRef):
        return value
    if isinstance(value, BaseModel) and hasattr(type(value), "__schema_annotations__"):
        return {name: _detach(getattr(value, name)) for name, _ in type(value).__schema_annotations__}
    if type(value) is dict:
        return {key: _detach(item) for key, item in value.items()}
    if type(value) is list:
        return [_detach(item) for item in value]
    if type(value) is tuple:
        return tuple(_detach(item) for item in value)
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError("Validator produced an unsupported semantic value")


def _wire(value):
    if type(value) is CollectionRef:
        return value.as_dict()
    if type(value) is Ref:
        return value.key.as_dict()
    if type(value) is dict:
        return {key: _wire(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [_wire(item) for item in value]
    return value


def _relation(name, annotation):
    annotation = _base(annotation)
    nullable = False
    if get_origin(annotation) in (Union, types.UnionType):
        members = get_args(annotation)
        if type(None) in members and len(members) == 2:
            nullable = True
            annotation = _base(next(member for member in members if member is not type(None)))
    origin = get_origin(annotation)
    if origin not in _RELATIONS:
        return None
    return RelationDescriptor(name, get_args(annotation)[0].__entity_type__, _RELATIONS[origin],
                              origin in (Owned, ResourceCollection, CollectionRef), nullable)


_GENERAL_METADATA = type(Field(pattern="accepted-metadata-probe").metadata[0])
_NUMERIC_METADATA = {_metadata.Gt: "gt", _metadata.Ge: "ge", _metadata.Lt: "lt",
                     _metadata.Le: "le", _metadata.MultipleOf: "multiple_of"}
_LENGTH_METADATA = {_metadata.MinLen: "min_length", _metadata.MaxLen: "max_length"}


def _accepted_metadata(annotation):
    items = []
    while get_origin(annotation) is Annotated:
        args = get_args(annotation)
        items.extend(args[1:])
        annotation = args[0]
    return tuple(items)


def _check_static_constraint(validator, value, kind):
    try:
        validator.validate_python(value, strict=True)
    except _CoreValidationError as error:
        codes = sorted({item["type"] for item in error.errors(
            include_url=False, include_context=False, include_input=False)})
        raise ValueError(f"Accepted value violates declared {kind} constraint ({', '.join(codes)})") from error


@dataclass(frozen=True, slots=True)
class _AcceptedConstraints:
    numeric: tuple
    lengths: tuple
    string: object
    unsupported: tuple

    def check(self, value):
        if self.unsupported:
            raise ValueError("Unsupported accepted-value validation metadata: " + ", ".join(self.unsupported))
        if self.numeric:
            if type(value) not in (int, float):
                raise TypeError("Declared numeric constraints require an exact numeric accepted value")
            validator = self.numeric[0 if type(value) is int else 1]
            if validator is None:
                raise ValueError("Unsupported accepted-value numeric constraints for this exact native type")
            _check_static_constraint(validator, value, "numeric")
        if self.lengths:
            if type(value) not in (str, dict, list, tuple):
                raise TypeError("Declared length constraints require a native string/container")
            minimum, maximum = self.lengths
            if minimum is not None and len(value) < minimum:
                raise ValueError("Accepted value violates min_length")
            if maximum is not None and len(value) > maximum:
                raise ValueError("Accepted value violates max_length")
        if self.string is not None:
            if type(value) is not str:
                raise TypeError("Declared string constraints require an exact string accepted value")
            _check_static_constraint(self.string, value, "string")


def _compile_accepted_constraints(items):
    numeric, lengths, string, unsupported = {}, {}, {}, []
    pending = list(items)
    while pending:
        item = pending.pop(0)
        kind = type(item)
        if kind is FieldInfo:
            pending[:0] = item.metadata
        elif kind in _NUMERIC_METADATA:
            name = _NUMERIC_METADATA[kind]
            numeric[name] = getattr(item, name)
        elif kind in _LENGTH_METADATA:
            name = _LENGTH_METADATA[kind]
            lengths[name] = getattr(item, name)
        elif kind is _metadata.Interval:
            numeric.update({name: getattr(item, name) for name in ("gt", "ge", "lt", "le")
                            if getattr(item, name) is not None})
        elif kind is _metadata.Len:
            lengths.update({name: getattr(item, name) for name in ("min_length", "max_length")
                            if getattr(item, name) is not None})
        elif kind is StringConstraints:
            lengths.update({name: getattr(item, name) for name in ("min_length", "max_length")
                            if getattr(item, name) is not None})
            string.update({name: getattr(item, name) for name in ("pattern", "ascii_only")
                           if getattr(item, name) is not None})
            # strip/case settings normalize inputs. Accepted values are never transformed here.
        elif kind is _GENERAL_METADATA:
            for name, argument in vars(item).items():
                if name in ("pattern", "ascii_only"):
                    string[name] = argument
                elif name in ("strict", "allow_inf_nan", "union_mode", "strip_whitespace",
                              "to_lower", "to_upper", "coerce_numbers_to_str"):
                    pass  # Input normalization/mode metadata; exact accepted types are checked separately.
                else:
                    unsupported.append(name)
        elif kind is _metadata.Unit:
            pass  # Inert units/labels are not validation rules.
        elif kind is _metadata.Predicate:
            pass  # Arbitrary author predicate: accepted once, never replayed on recovery.
        elif kind.__module__ in ("pydantic.functional_validators", "pydantic.functional_serializers",
                                  "pydantic.json_schema"):
            pass  # Explicit author callbacks/serialization/schema views, never called here.
        elif kind.__module__ == "pydantic.types" and kind.__name__ in ("Strict", "AllowInfNan", "FailFast", "OnErrorOmit"):
            pass
        elif (hasattr(kind, "__get_pydantic_core_schema__")
              or _metadata.BaseMetadata in kind.__mro__ or _metadata.GroupedMetadata in kind.__mro__
              or (isinstance(item, type) and (_metadata.BaseMetadata in item.__mro__
                                            or _metadata.GroupedMetadata in item.__mro__))):
            unsupported.append(f"{kind.__module__}.{kind.__qualname__}")
        # Other metadata (labels, docs, JSON-like UI values, Unit) is inert to Pydantic validation.
    for name, argument in numeric.items():
        if type(argument) not in (int, float) or (type(argument) is float and not math.isfinite(argument)):
            unsupported.append(name)
    if numeric.get("multiple_of") == 0:
        unsupported.append("multiple_of=0")
    for name, argument in lengths.items():
        if type(argument) is not int or argument < 0:
            unsupported.append(name)
    if "pattern" in string and type(string["pattern"]) not in (str, re.Pattern):
        unsupported.append("pattern")
    if "ascii_only" in string and type(string["ascii_only"]) is not bool:
        unsupported.append("ascii_only")
    if unsupported:
        return _AcceptedConstraints((), (), None, tuple(unsupported))
    number_checks = []
    if numeric:
        for factory, settings in ((core_schema.int_schema, {}), (core_schema.float_schema, {"allow_inf_nan": False})):
            try:
                number_checks.append(SchemaValidator(factory(strict=True, **settings, **numeric)))
            except (SchemaError, TypeError, ValueError, OverflowError):
                # Do not change public normal-validation registration semantics for a recovery subset.
                number_checks.append(None)
    size_checks = (() if not lengths else (lengths.get("min_length"), lengths.get("max_length")))
    text_check = None if not string else SchemaValidator(core_schema.str_schema(strict=True, **string))
    return _AcceptedConstraints(tuple(number_checks), size_checks, text_check, ())


class SchemaContractViolation(ValueError):
    """Engine-private structural refusal; never evidence of arbitrary author semantics."""
    def __init__(self, path, detail, *, definition_id=None):
        self.path = tuple(path)
        self.definition_id = definition_id
        self.detail = detail
        location = definition_id or "accepted value"
        for part in self.path:
            location += f"[{part}]" if type(part) is int else f"[{part!r}]"
        super().__init__(f"{location}: {detail}")


@dataclass(frozen=True, slots=True)
class _AcceptedRule:
    kind: object
    arguments: tuple
    constraints: _AcceptedConstraints

    def check(self, value, registry, *, path=()):
        try:
            self._inspect(value, registry, path)
        except SchemaContractViolation:
            raise
        except (TypeError, ValueError) as error:
            raise SchemaContractViolation(path, str(error)) from error

    def _inspect(self, value, registry, path):
        kind, args = self.kind, self.arguments
        if kind in (str, bool, int, float, type(None)):
            if type(value) is not kind or (kind is float and not math.isfinite(value)):
                raise TypeError("Accepted scalar must keep its exact finite native type")
        elif kind is Literal:
            if not any(type(value) is type(item) and value == item for item in args):
                raise ValueError("Accepted literal value/type mismatch")
        elif kind == "union":
            for rule in args:
                try:
                    rule.check(value, registry, path=path)
                    break
                except (TypeError, ValueError):
                    pass
            else:
                raise ValueError("Accepted value matches no declared union member")
        elif kind in (Ref, CollectionRef):
            if type(value) is not kind:
                raise TypeError("Accepted identity must be an immutable native identity leaf")
            key = value.key if kind is Ref else value.owner
            if type(key) is not EntityKey:
                raise TypeError("Accepted identity owner/key must be EntityKey")
            EntityKey(key.entity_type, key.entity_id, key.generation)  # Recheck data, never graph liveness.
            if kind is Ref and key.entity_type != args[0]:
                raise ValueError("Accepted Ref target type mismatch")
            if kind is CollectionRef:
                CollectionRef(key, value.field)
                registry.validate_collection_ref(value, target_type=args[0])
        elif kind in (list, tuple):
            if type(value) is not kind:
                raise TypeError("Accepted container must preserve exact list/tuple identity")
            elements, variadic = args
            if not variadic and len(value) != len(elements):
                raise ValueError("Accepted tuple arity mismatch")
            for index, item in enumerate(value):
                elements[0 if variadic else index].check(item, registry, path=path + (index,))
        elif kind is dict:
            if type(value) is not dict or any(type(key) is not str for key in value):
                raise TypeError("Accepted map requires exact native dict/string keys")
            for key, item in value.items():
                args[0].check(item, registry, path=path + ("*",))
        elif kind == "record":
            if (type(value) is not dict or any(type(key) is not str for key in value)
                    or value.keys() != {name for name, _ in args}):
                raise ValueError("Accepted record requires the complete exact declared field set")
            for name, rule in args:
                rule.check(value[name], registry, path=path + (name,))
        else:
            raise TypeError("Unsupported accepted-value annotation")
        self.constraints.check(value)

    def from_wire(self, value, registry, *, path=()):
        """Reconstruct only declared native topology, then reuse this static rule.

        Callers preflight the bounded parsed tree first. No Pydantic validation,
        author callbacks, default filling, migration or graph access occurs here.
        """
        try:
            kind, args = self.kind, self.arguments
            if kind == "union":
                for rule in args:
                    try:
                        result = rule.from_wire(value, registry, path=path)
                        self.constraints.check(result)
                        return result
                    except (TypeError, ValueError):
                        pass
                raise ValueError("Accepted wire value matches no declared union member")
            if kind is Ref:
                if type(value) is not dict or set(value) != {"entity_type", "entity_id", "generation"}:
                    raise TypeError("Accepted wire Ref requires exact identity fields")
                result = Ref(EntityKey(**value))
            elif kind is CollectionRef:
                if (type(value) is not dict or set(value) != {"owner", "field"}
                        or type(value["owner"]) is not dict
                        or set(value["owner"]) != {"entity_type", "entity_id", "generation"}):
                    raise TypeError("Accepted wire CollectionRef requires exact owner/field objects")
                result = CollectionRef(EntityKey(**value["owner"]), value["field"])
            elif kind in (list, tuple):
                if type(value) is not list:
                    raise TypeError("Accepted wire sequence requires an exact JSON array")
                elements, variadic = args
                if not variadic and len(value) != len(elements):
                    raise ValueError("Accepted wire tuple arity mismatch")
                items = [elements[0 if variadic else index].from_wire(item, registry, path=path + (index,))
                         for index, item in enumerate(value)]
                result = items if kind is list else tuple(items)
            elif kind is dict:
                if type(value) is not dict or any(type(key) is not str for key in value):
                    raise TypeError("Accepted wire map requires exact string keys")
                result = {key: args[0].from_wire(item, registry, path=path + ("*",))
                          for key, item in value.items()}
            elif kind == "record":
                if type(value) is not dict or value.keys() != {name for name, _ in args}:
                    raise ValueError("Accepted record requires the complete exact declared field set")
                result = {name: rule.from_wire(value[name], registry, path=path + (name,))
                          for name, rule in args}
            else:
                result = value
            self.check(result, registry, path=path)
            return result
        except SchemaContractViolation:
            raise
        except (TypeError, ValueError) as error:
            raise SchemaContractViolation(path, str(error)) from error


def _compile_accepted(annotation, *, metadata=None):
    constraints = _compile_accepted_constraints(_accepted_metadata(annotation) if metadata is None else metadata)
    annotation = _base(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation in (str, bool, int, float, type(None)):
        kind, arguments = annotation, ()
    elif origin is Literal:
        kind, arguments = Literal, args
    elif origin in (Union, types.UnionType):
        kind, arguments = "union", tuple(_compile_accepted(item) for item in args)
    elif origin in (Ref, Resource, CollectionRef, ResourceCollection):
        kind = Ref if origin in (Ref, Resource) else CollectionRef
        arguments = (args[0].__entity_type__,)
    elif origin in (list, tuple, Owned):
        kind = list if origin is list else tuple
        variadic = origin in (list, Owned) or (len(args) == 2 and args[1] is Ellipsis)
        items = (Ref[args[0]],) if origin is Owned else (args[:1] if variadic else args)
        arguments = (tuple(_compile_accepted(item) for item in items), variadic)
    elif origin is dict:
        kind, arguments = dict, (_compile_accepted(args[1]),)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        kind, arguments = "record", annotation.__accepted_fields__
    else:
        raise TypeError("Unsupported accepted-value annotation")
    return _AcceptedRule(kind, arguments, constraints)


@dataclass(frozen=True, slots=True)
class _Definition:
    descriptor: DefinitionDescriptor
    annotations: tuple
    validator: SchemaValidator
    handler: object = None
    collection_check: object = None
    accepted: object = None

    def validate(self, value, *, wire=False):
        _fields_shape(self.annotations, value, wire=wire, collection_check=self.collection_check)
        # JSON mode deliberately accepts JSON arrays for tuple/relation wire values.
        model = (self.validator.validate_json(canonical(value), strict=True) if wire else
                 self.validator.validate_python(value, strict=True))
        output = {name: getattr(model, name) for name, _ in self.annotations}
        _fields_shape(self.annotations, output, collection_check=self.collection_check)
        return _detach(model)


class Registry:
    """Trusted declarations only. This class never executes command handlers.

    Validation returns detached intermediate Python values, NOT published snapshots.
    C1 owns final deep-freeze/managed references; C2 owns command authority/admission.
    """
    def __init__(self, namespace: str, revision: int):
        valid_type_id(namespace + ".registry")
        self._namespace = namespace
        self._revision = _positive(revision, "Registry revision")
        self._definitions = {}
        self._symbols = {}
        self._migrations = {}

    @property
    def namespace(self):
        return self._namespace

    @property
    def revision(self):
        return self._revision

    def _id(self, name):
        if type(name) is not str:
            raise ValueError("Expected a stable definition name")
        if name.startswith(self.namespace + "."):
            valid_type_id(name)
            return name
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("Definition names must be explicit lowercase stable identifiers")
        return self.namespace + "." + name

    def _definition(self, kind, name):
        return self._definitions[(kind, self._id(name))]

    def _build(self, kind, name, revision, metadata, fields, *, doc=None, validators=None, handler=None,
               permissions=None):
        definition_id = self._id(name)
        if any(key[1] == definition_id for key in self._definitions):
            raise ValueError(f"Duplicate definition ID: {definition_id}")
        revision = _positive(revision, "Definition revision")
        meta = freeze({} if metadata is None else metadata)
        if isinstance(meta, Mapping) and "command_permissions" in meta:
            raise ValueError("command_permissions is reserved Registry metadata")
        if kind == "command":
            if permissions is not None and type(permissions) is not CommandPermissionProfile:
                raise TypeError("Use an exact CommandPermissionProfile")
            # Recapture/revalidate even an exact frozen value; never retain author state.
            profile = CommandPermissionProfile() if permissions is None else CommandPermissionProfile(
                permissions.effect_actions, permissions.create_types, permissions.allow_activity_transfer)
            commands = tuple(d.descriptor for (k, _), d in self._definitions.items() if k == "command")
            command_ids = {d.definition_id for d in commands} | {definition_id}
            declared_effects = {a for d in commands for a in d.command_permissions.effect_actions}
            if definition_id in declared_effects or command_ids.intersection(profile.effect_actions):
                raise ValueError("Command IDs and effect action IDs must be disjoint in this Registry")
            if not isinstance(meta, Mapping):
                raise TypeError("Command metadata must be a mapping")
            meta = freeze({**meta, "command_permissions": profile._metadata()})
        fields = {field: (_snapshot_annotation(annotation), deepcopy(default))
                  for field, (annotation, default) in fields.items()}
        annotations = tuple((field, annotation) for field, (annotation, _) in fields.items())
        for field, annotation in annotations:
            try:
                _supported(annotation)
            except TypeError as error:
                raise TypeError(f"{definition_id}.{field}: {error}") from error
        relations = tuple(relation for field, annotation in annotations
                          if (relation := _relation(field, annotation)) is not None)
        config = ConfigDict(strict=True, extra="forbid", validate_default=True,
                            revalidate_instances="always", frozen=True, allow_inf_nan=False,
                            json_schema_extra={"x-agent-native": {"definition_id": definition_id,
                                                "kind": kind, "revision": revision,
                                                "relations": [{"field": relation.field, "target_type": relation.target_type,
                                                               "kind": relation.kind, "many": relation.many,
                                                               "nullable": relation.nullable} for relation in relations]}})
        model = create_model(definition_id.replace(".", "_"), __config__=config,
                             __doc__=doc, __validators__=validators or {}, **fields)
        model.__record_type__ = definition_id
        model.__schema_annotations__ = annotations
        if kind == "entity":
            model.__entity_type__ = definition_id
        for info in model.model_fields.values():
            if info.alias or info.validation_alias or info.serialization_alias:
                raise TypeError("Field aliases are unsupported: one field name is shared by all projections")
        accepted_fields = tuple((field, _compile_accepted(annotation, metadata=model.model_fields[field].metadata))
                                for field, annotation in annotations)
        model.__accepted_fields__ = accepted_fields
        accepted = _AcceptedRule("record", accepted_fields, _compile_accepted_constraints(()))
        schema = freeze(model.model_json_schema())
        field_descriptors = []
        for field, annotation in annotations:
            info = model.model_fields[field]
            field_meta = {key: value for key, value in {
                "title": info.title, "description": info.description,
                "extra": info.json_schema_extra,
            }.items() if value is not None}
            field_descriptors.append(FieldDescriptor(field, info.is_required(), freeze(field_meta),
                                                     _relation(field, annotation)))
        identity = {"id": definition_id, "kind": kind, "revision": revision,
                    "schema": thaw(schema), "metadata": thaw(meta),
                    "relations": [{"field": f.relation.field, "target": f.relation.target_type,
                                   "kind": f.relation.kind, "many": f.relation.many,
                                   "nullable": f.relation.nullable}
                                  for f in field_descriptors if f.relation]}
        descriptor = DefinitionDescriptor(definition_id, kind, revision,
                                          sha256(canonical(identity)).hexdigest(),
                                          tuple(field_descriptors), meta, schema)
        # Capture the validator and immutable descriptor now; views never use caller-owned dictionaries.
        self._definitions[(kind, definition_id)] = _Definition(
            descriptor, annotations, SchemaValidator(model.__pydantic_core_schema__), handler, self.validate_collection_ref, accepted)
        return model

    def _class_decorator(self, kind, name, revision, metadata):
        def decorate(author):
            if not isinstance(author, type) or author.__bases__ != (object,):
                raise TypeError("Author one plain annotated class; inheritance is unsupported")
            hints = get_type_hints(author, localns=self._symbols, include_extras=True)
            fields = {field: (annotation, deepcopy(vars(author).get(field, ...)))
                      for field, annotation in hints.items()}
            validators = {key: value for key, value in vars(author).items()
                          if hasattr(value, "decorator_info") and hasattr(value, "wrapped")}
            model = self._build(kind, name, revision, metadata, fields,
                                doc=author.__doc__, validators=validators)
            self._symbols[author.__name__] = model
            return model
        return decorate

    def entity(self, name: str, *, revision: int = 1, metadata=None):
        return self._class_decorator("entity", name, revision, metadata)

    def record(self, name: str, *, revision: int = 1, metadata=None):
        return self._class_decorator("record", name, revision, metadata)

    def command(self, name: str, *, revision: int = 1, metadata=None,
                permissions: CommandPermissionProfile | None = None):
        """One command/arguments/profile declaration; C2 enforces the declared ceiling."""
        def decorate(handler):
            if inspect.iscoroutinefunction(handler) or inspect.isgeneratorfunction(handler) or inspect.isasyncgenfunction(handler):
                raise TypeError("Command prepare handlers must be synchronous trusted functions")
            parameters = tuple(inspect.signature(handler).parameters.values())
            if (not parameters or parameters[0].name != "ctx"
                    or parameters[0].kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    or parameters[0].default is not inspect.Parameter.empty):
                raise TypeError("Command requires the first trusted context parameter named ctx")
            hints = get_type_hints(handler, localns=self._symbols, include_extras=True)
            fields = {}
            for parameter in parameters[1:]:
                if parameter.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                    raise TypeError("Command arguments must be named; variadic/positional-only args are unsupported")
                if parameter.name not in hints:
                    raise TypeError("Every command argument needs a semantic annotation")
                fields[parameter.name] = (hints[parameter.name], ... if parameter.default is inspect.Parameter.empty
                                          else deepcopy(parameter.default))
            self._build("command", name, revision, metadata, fields,
                        doc=handler.__doc__, handler=handler, permissions=permissions)
            return handler
        return decorate

    def entity_descriptor(self, name):
        return self._definition("entity", name).descriptor

    def command_descriptor(self, name):
        return self._definition("command", name).descriptor

    def record_descriptor(self, name):
        return self._definition("record", name).descriptor

    def descriptors(self, *, kind=None):
        """Complete generated catalogue, stable by ID; no separately authored tool list."""
        if kind is not None and kind not in ("entity", "record", "command"):
            raise ValueError("Unknown definition kind")
        return tuple(definition.descriptor for key, definition in sorted(self._definitions.items())
                     if kind is None or key[0] == kind)

    def tool_schemas(self):
        return tuple(self.tool_schema(descriptor.definition_id)
                     for descriptor in self.descriptors(kind="command"))

    def schema(self, name, *, kind="entity"):
        return thaw(self._definition(kind, name).descriptor.json_schema)

    def tool_schema(self, name):
        descriptor = self.command_descriptor(name)
        return {"name": descriptor.definition_id,
                "description": descriptor.metadata.get("description", ""),
                "parameters": thaw(descriptor.json_schema),
                "schema_revision": descriptor.revision, "schema_fingerprint": descriptor.fingerprint}

    def ui_bindings(self, name, *, kind="entity"):
        descriptor = self._definition(kind, name).descriptor
        return UiBindingDescriptor(descriptor.definition_id, kind, descriptor.fields, descriptor.metadata)

    def command_handler(self, name):
        """Trusted C2 integration handle. No execution, model args or context synthesis here."""
        return self._definition("command", name).handler

    def validate_collection_ref(self, ref, *, target_type=None):
        """Validate a direct Owned source schema, not owner existence or generation liveness.

        Minimal subset: source owner entity must be registered in this Registry;
        nullable/alias/query/scalar sources and cross-registry owner schemas refuse.
        """
        if type(ref) is not CollectionRef:
            raise TypeError("Expected an immutable CollectionRef identity")
        if target_type is not None:
            valid_type_id(target_type)
        try:
            descriptor = self.entity_descriptor(ref.owner.entity_type)
        except (KeyError, ValueError) as error:
            raise ValueError("Unknown collection source owner schema in this Registry") from error
        source = next((field.relation for field in descriptor.fields if field.name == ref.field), None)
        if source is None or source.kind != "owned" or not source.many or source.nullable:
            raise ValueError("Collection source must be one declared direct nonnullable Owned field")
        if target_type is not None and source.target_type != target_type:
            raise ValueError("Collection source element type does not match its annotation")
        return ref

    def check_accepted_entity(self, name, value):
        """Engine-private storage bridge: inspect a full native accepted tree, return None.

        No author callbacks, defaults, normalization or graph liveness. Known declared
        static constraints are rechecked; arbitrary author business invariants are not.
        This is structural inspection for trusted app storage, not hostile-writer proof.
        Public input validation/encode/decode retain their separate callback semantics.
        """
        definition = self._definition("entity", name)
        try:
            definition.accepted.check(value, self)
        except SchemaContractViolation as error:
            raise SchemaContractViolation(error.path, error.detail,
                                          definition_id=definition.descriptor.definition_id) from error

    def validate_record(self, name, raw):
        """One input-validation pass, including trusted defaults/validators.

        Returns a detached native dict, not a frozen snapshot. An input pass may
        explore multiple Pydantic union branches; accepted replay never runs it.
        """
        return self._definition("record", name).validate(raw)

    def check_accepted_record(self, name, accepted):
        """Inspect a complete native record callback-free under fixed codec limits.

        Limits/units are shared with accepted encode/decode in schema.codec.
        No defaults, migrations, normalization, graph-liveness or provenance proof.
        """
        from .codec import encode_accepted_record
        encode_accepted_record(self, name, accepted)

    def encode_accepted_record(self, name, accepted):
        """Encode complete accepted native data without replaying author callbacks."""
        from .codec import encode_accepted_record
        return encode_accepted_record(self, name, accepted)

    def decode_accepted_record(self, data):
        """Restore the exact current record and native topology, callback-free.

        A byte guard precedes parsing; structural limits follow parsing before
        reconstruction. No pre-JSON allocation, pre-BLOB, RSS or sandbox promise.
        """
        from .codec import decode_accepted_record
        return decode_accepted_record(self, data)

    def validate_entity(self, name, value):
        return self._definition("entity", name).validate(value)

    def validate_arguments(self, name, value):
        return self._definition("command", name).validate(value)

    def encode_entity(self, name, value):
        from .codec import encode
        return encode(self, "entity", name, value)

    def decode_entity(self, data):
        from .codec import decode
        return decode(self, "entity", data)

    def encode_arguments(self, name, value):
        from .codec import encode
        return encode(self, "command", name, value)

    def decode_arguments(self, data):
        from .codec import decode
        return decode(self, "command", data)

    def register_migration(self, name, *, source, migrate, kind="entity"):
        """One explicit direct migration from an exact validated source definition.

        The source Registry supplies its compiled old validator. There is no automatic
        coercion, migration search, command execution, or fallback to the current schema.
        """
        if source.namespace != self.namespace:
            raise ValueError("Migration must preserve the definition namespace")
        previous = source._definition(kind, name)
        current = self._definition(kind, name)
        if (source.revision > self.revision or previous.descriptor.revision > current.descriptor.revision
                or (source.revision, previous.descriptor.revision) == (self.revision, current.descriptor.revision)):
            raise ValueError("An explicit revision advance is required for migration")
        if not callable(migrate) or inspect.iscoroutinefunction(migrate) or inspect.isgeneratorfunction(migrate):
            raise TypeError("Migration must be a trusted synchronous callable")
        key = (source.revision, kind, previous.descriptor.definition_id,
               previous.descriptor.revision, previous.descriptor.fingerprint)
        if key in self._migrations:
            raise ValueError("Duplicate migration source identity")
        self._migrations[key] = (previous, migrate)
