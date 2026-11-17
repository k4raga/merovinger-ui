"""Callback-free semantic/schema policy over the ORIGINAL Registry annotations.

This is a conservative projection predicate, not validation/normalization and not
a second app schema. Unsupported or forbidden nested fragments refuse the whole
containing field/schema. No author validator, serializer or default factory runs.
"""
from collections.abc import Mapping
from types import UnionType
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from agent_native.schema import Ref, Resource, Owned, CollectionRef, ResourceCollection, EntityKey
from agent_native.state import FrozenList


def base(annotation):
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def scalar(annotation):
    annotation = base(annotation)
    return annotation in (str, bool, int, float, type(None)) or get_origin(annotation) is Literal


class SemanticProjection:
    def __init__(self, rules, declarations, private_types, allow_source, *, identity_record_type=None):
        self.rules, self.declarations = rules, declarations
        self.private_types, self.allow_source = private_types, allow_source
        self.identity_record_type = identity_record_type

    def annotation(self, annotation, *, seen=()):
        while get_origin(annotation) is Annotated:
            args = get_args(annotation)
            for item in args[1:]:
                if isinstance(item, FieldInfo):
                    extra = item.json_schema_extra
                    if extra is not None and (not isinstance(extra, Mapping)
                            or not self.rules.fragment(extra)):
                        return False
            annotation = args[0]
        origin, args = get_origin(annotation), get_args(annotation)
        if scalar(annotation):
            return True
        if origin in (Ref, Resource, Owned, CollectionRef, ResourceCollection):
            target = args[0].__entity_type__
            definition = self.declarations.get(target)
            return (target not in self.private_types and definition is not None
                    and self.rules.permits(definition.descriptor))
        if origin in (Union, UnionType, list, tuple, dict):
            values = args[1:] if origin is dict else args
            return all(item is Ellipsis or self.annotation(item, seen=seen) for item in values)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            ident = getattr(annotation, "__record_type__", None)
            if ident is None or ident in seen:
                return False
            definition = self.declarations.get(ident)
            if definition is None or not self.rules.permits(definition.descriptor, metadata=True):
                return False
            return all(self.rules.permits(definition.descriptor, name=name, metadata=True)
                       and self.annotation(item, seen=(*seen, ident))
                       for name, item in definition.annotations)
        return False

    def value(self, annotation, value, *, wire=False):
        if not self.annotation(annotation):
            return False
        annotation = base(annotation)
        origin, args = get_origin(annotation), get_args(annotation)
        if annotation in (str, bool, int, float, type(None)):
            return type(value) is annotation
        if origin is Literal:
            return any(type(value) is type(item) and value == item for item in args)
        if origin in (Union, UnionType):
            return any(self.value(item, value, wire=wire) for item in args)
        if origin in (Ref, Resource):
            if type(value) is Ref:
                ref = value
            elif wire and isinstance(value, Mapping) and set(value) == {"entity_type", "entity_id", "generation"}:
                try:
                    ref = Ref(EntityKey(**value))
                except (TypeError, ValueError):
                    return False
            else:
                return False
            return ref.key.entity_type == args[0].__entity_type__ and self.allow_source(ref)
        if origin in (CollectionRef, ResourceCollection):
            if type(value) is CollectionRef:
                alias = value
            elif wire and isinstance(value, Mapping) and set(value) == {"owner", "field"}:
                try:
                    if not isinstance(value["owner"], Mapping) or set(value["owner"]) != {"entity_type", "entity_id", "generation"}:
                        return False
                    alias = CollectionRef(EntityKey(**value["owner"]), value["field"])
                except (TypeError, ValueError):
                    return False
            else:
                return False
            owner = self.declarations.get(alias.owner.entity_type)
            if owner is None:
                return False
            source = next((f.relation for f in owner.descriptor.fields if f.name == alias.field), None)
            return (source is not None and source.kind == "owned" and not source.nullable
                    and source.target_type == args[0].__entity_type__ and self.allow_source(alias))
        if origin in (tuple, list, Owned):
            if type(value) not in (tuple, list, FrozenList):
                return False
            if origin is Owned:
                annotations = (Ref[args[0]],) * len(value)
            elif origin is list or (len(args) == 2 and args[1] is Ellipsis):
                annotations = (args[0],) * len(value)
            elif len(value) == len(args):
                annotations = args
            else:
                return False
            return all(self.value(item, child, wire=wire) for item, child in zip(annotations, value))
        if origin is dict:
            return isinstance(value, Mapping) and all(type(k) is str and self.value(args[1], v, wire=wire) for k, v in value.items())
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            definition = self.declarations[annotation.__record_type__]
            if annotation.__record_type__ == self.identity_record_type:
                from .bindings import identity
                try:
                    return self.allow_source(identity(value))
                except (TypeError, ValueError, KeyError):
                    return False
            if not isinstance(value, Mapping):
                return False
            fields = dict(definition.annotations)
            return (set(value) <= set(fields)
                    and all(self.value(fields[name], child, wire=wire) for name, child in value.items()))
        return False

    @staticmethod
    def _resolved(node, root):
        link = node.get("$ref")
        if link is None:
            return node
        if type(link) is not str or not link.startswith("#/$defs/"):
            return None
        return root.get("$defs", {}).get(link[len("#/$defs/"):])

    def _matches(self, annotation, node, root):
        annotation = base(annotation)
        origin, args = get_origin(annotation), get_args(annotation)
        node = self._resolved(node, root)
        if not isinstance(node, Mapping):
            return False
        if origin in (Union, UnionType):
            return any(self._matches(item, node, root) for item in args)
        kind = node.get("type")
        if origin in (Ref, Resource):
            return kind == "object" and node.get("properties", {}).get("entity_type", {}).get("const") == args[0].__entity_type__
        if origin in (CollectionRef, ResourceCollection):
            return kind == "object" and set(node.get("properties", {})) == {"owner", "field"}
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            definition = self.declarations.get(annotation.__record_type__)
            return definition is not None and node.get("title") == definition.descriptor.json_schema.get("title")
        if origin in (tuple, list, Owned):
            return kind == "array"
        if origin is dict:
            return kind == "object"
        if origin is Literal:
            return all(self.value(annotation, value, wire=True) for value in node.get("enum", (node.get("const"),)))
        return kind == {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}.get(annotation)

    def schema(self, annotation, node, root, seen=()):
        """Use declared child annotations for every typed literal/default/enum."""
        if not isinstance(node, Mapping) or not self.annotation(annotation) or not self.rules.fragment(node, root):
            return False
        known = {"$ref", "$defs", "type", "properties", "required", "additionalProperties",
                 "items", "prefixItems", "anyOf", "oneOf", "title", "description",
                 "default", "const", "enum", "examples", "deprecated", "readOnly", "writeOnly",
                 "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                 "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
                 "uniqueItems", "minProperties", "maxProperties", "export_scope", "metadata_scope",
                 "x-agent-native"}
        if set(node) - known:
            return False  # unknown fragment has no typed projection proof
        if "x-agent-native" in node:
            native = base(annotation)
            if not (isinstance(native, type) and issubclass(native, BaseModel)):
                return False
            declaration = self.declarations.get(getattr(native, "__record_type__", None))
            if declaration is None or node["x-agent-native"] != declaration.descriptor.json_schema.get("x-agent-native"):
                return False
        for key in ("default", "const"):
            if key in node and not self.value(annotation, node[key], wire=True):
                return False
        if "enum" in node and (type(node["enum"]) not in (tuple, list)
                or not all(self.value(annotation, v, wire=True) for v in node["enum"])):
            return False
        if "examples" in node and (type(node["examples"]) not in (tuple, list)
                or not all(self.value(annotation, v, wire=True) for v in node["examples"])):
            return False
        annotation = base(annotation)
        origin, args = get_origin(annotation), get_args(annotation)
        branches = node.get("anyOf", node.get("oneOf"))
        if branches is not None:
            choices = args if origin in (Union, UnionType) else (annotation,)
            return all(any(self._matches(choice, branch, root) and self.schema(choice, branch, root)
                           for choice in choices) for branch in branches)
        if origin in (Union, UnionType):
            return any(self._matches(choice, node, root) and self.schema(choice, node, root) for choice in args)
        resolved = self._resolved(node, root)
        if resolved is None:
            return False
        if resolved is not node:
            link = node.get("$ref")
            return link not in seen and self.schema(annotation, resolved, root, (*seen, link))
        if not self._matches(annotation, node, root):
            return False
        if origin in (Ref, Resource, CollectionRef, ResourceCollection):
            # Full identity literals above have provenance. Partial identity
            # property literals/defaults have no complete incarnation proof.
            def no_partial(value, path=()):
                if isinstance(value, Mapping):
                    for key, child in value.items():
                        if key in ("default", "enum", "const", "examples"):
                            if not (origin in (Ref, Resource) and path == ("entity_type",)
                                    and key == "const" and child == args[0].__entity_type__):
                                return False
                        elif key == "properties":
                            if not all(no_partial(v, (*path, k)) for k, v in child.items()):
                                return False
                        elif type(child) in (tuple, list):
                            if not all(no_partial(v, path) for v in child):
                                return False
                        elif isinstance(child, Mapping) and not no_partial(child, path):
                            return False
                return True
            return all(
                no_partial(child, (name,)) for name, child in node.get("properties", {}).items())
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            definition = self.declarations[annotation.__record_type__]
            return self.fields(definition, node, root)
        if origin in (tuple, list, Owned):
            children = (Ref[args[0]],) if origin is Owned else args
            prefix = node.get("prefixItems")
            if prefix is not None:
                return len(prefix) == len(children) and all(self.schema(item, frag, root) for item, frag in zip(children, prefix))
            item = children[0] if children else None
            return item is not None and self.schema(item, node.get("items", {}), root)
        if origin is dict:
            return self.schema(args[1], node.get("additionalProperties", {}), root)
        return not any(key in node for key in ("properties", "items", "prefixItems"))

    def field(self, definition, name, *, text=False):
        """Effective declared field predicate shared by data/Draft eligibility.

        Text editing/export supports exact compiled str only. Whole-record,
        optional/composite/identity values require a separate explicit contract.
        No field value or Draft text is read by this callback-free predicate.
        """
        if type(name) is not str or not name:
            return False
        annotations = dict(definition.annotations)
        annotation = annotations.get(name)
        if annotation is None or not self.rules.permits(definition.descriptor, name=name, metadata=True):
            return False
        if text and base(annotation) is not str:
            return False
        schema = definition.descriptor.json_schema
        return self.schema(annotation, schema.get("properties", {}).get(name, {}), schema)

    def fields(self, definition, node=None, root=None):
        node = definition.descriptor.json_schema if node is None else node
        root = node if root is None else root
        if not self.rules.permits(definition.descriptor, metadata=True):
            return False
        properties = node.get("properties", {})
        if set(properties) != {name for name, _ in definition.annotations}:
            return False
        return all(self.rules.permits(definition.descriptor, name=name, metadata=True)
                   and self.schema(annotation, properties[name], root)
                   for name, annotation in definition.annotations)

    def definition(self, definition):
        root = definition.descriptor.json_schema
        if not self.fields(definition):
            return False
        by_title = {d.descriptor.json_schema.get("title"): d for d in self.declarations.values()}
        for node in root.get("$defs", {}).values():
            nested = by_title.get(node.get("title"))
            if nested is None or nested.descriptor.kind != "record" or not self.fields(nested, node, root):
                return False
        return True
