"""Schema identity leaves and declarative relations; no graph access or liveness."""
from dataclasses import dataclass
import re
from typing import Generic, TypeVar, get_args
from pydantic_core import core_schema

T = TypeVar("T")
_TYPE_ID = re.compile(r"[a-z][a-z0-9_]*(?:[.-][a-z][a-z0-9_]*)+\Z")
_FIELD_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_ENTITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


def valid_type_id(value: str) -> str:
    if type(value) is not str or not _TYPE_ID.fullmatch(value):
        raise ValueError("Expected a stable namespaced type ID")
    return value


@dataclass(frozen=True, slots=True)
class EntityKey:
    entity_type: str
    entity_id: str
    generation: int

    def __post_init__(self):
        valid_type_id(self.entity_type)
        if type(self.entity_id) is not str or not _ENTITY_ID.fullmatch(self.entity_id):
            raise ValueError("Expected a stable entity ID, 1..256 ASCII ID characters")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("Generation must be a strict positive integer")

    def as_dict(self) -> dict:
        return {"entity_type": self.entity_type, "entity_id": self.entity_id,
                "generation": self.generation}


class Ref(Generic[T]):
    __slots__ = ("key",)
    key: EntityKey

    def __init__(self, key: EntityKey):
        if type(key) is not EntityKey:
            raise TypeError("Ref requires an EntityKey")
        object.__setattr__(self, "key", key)

    def __setattr__(self, name, value):
        raise AttributeError("Ref identity is immutable")

    def __delattr__(self, name):
        raise AttributeError("Ref identity is immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        return type(other) is Ref and self.key == other.key

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        return f"Ref({self.key!r})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        args = get_args(source)
        if len(args) != 1 or not hasattr(args[0], "__entity_type__"):
            raise TypeError("Ref must target a registered entity class")
        target = args[0].__entity_type__
        wire = core_schema.typed_dict_schema({
            "entity_type": core_schema.typed_dict_field(core_schema.literal_schema([target])),
            "entity_id": core_schema.typed_dict_field(core_schema.str_schema(
                strict=True, pattern=_ENTITY_ID.pattern.replace("\\Z", "$"))),
            "generation": core_schema.typed_dict_field(core_schema.int_schema(strict=True, ge=1)),
        }, extra_behavior="forbid", strict=True)

        def build(value):
            ref = value if type(value) is Ref else Ref(EntityKey(**value))
            if ref.key.entity_type != target:
                raise ValueError("Reference entity type does not match its annotation")
            return ref

        return core_schema.no_info_after_validator_function(
            build, core_schema.json_or_python_schema(
                json_schema=wire,
                python_schema=core_schema.union_schema([
                    core_schema.is_instance_schema(Ref), wire])),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda ref: ref.key.as_dict(), when_used="json", return_schema=wire))


class CollectionRef(Generic[T]):
    """Immutable source identity. Contains no members, revision, view or resolver."""
    __slots__ = ("owner", "field")
    owner: EntityKey
    field: str

    def __init__(self, owner: EntityKey, field: str):
        if type(owner) is not EntityKey:
            raise TypeError("CollectionRef requires an EntityKey owner")
        if type(field) is not str or not _FIELD_ID.fullmatch(field):
            raise ValueError("Collection field must be one stable direct field identifier")
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "field", field)

    def __setattr__(self, name, value):
        raise AttributeError("CollectionRef identity is immutable")

    def __delattr__(self, name):
        raise AttributeError("CollectionRef identity is immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        return type(other) is CollectionRef and self.owner == other.owner and self.field == other.field

    def __hash__(self):
        return hash((self.owner, self.field))

    def __repr__(self):
        return f"CollectionRef(owner={self.owner!r}, field={self.field!r})"

    def as_dict(self):
        return {"owner": self.owner.as_dict(), "field": self.field}

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        args = get_args(source)
        if args and (len(args) != 1 or not hasattr(args[0], "__entity_type__")):
            raise TypeError("CollectionRef must target a registered entity class")
        owner = core_schema.typed_dict_schema({
            "entity_type": core_schema.typed_dict_field(core_schema.str_schema(
                strict=True, pattern=_TYPE_ID.pattern.replace("\\Z", "$"))),
            "entity_id": core_schema.typed_dict_field(core_schema.str_schema(
                strict=True, pattern=_ENTITY_ID.pattern.replace("\\Z", "$"))),
            "generation": core_schema.typed_dict_field(core_schema.int_schema(strict=True, ge=1)),
        }, extra_behavior="forbid", strict=True)
        wire = core_schema.typed_dict_schema({
            "owner": core_schema.typed_dict_field(owner),
            "field": core_schema.typed_dict_field(core_schema.str_schema(
                strict=True, pattern=_FIELD_ID.pattern.replace("\\Z", "$"))),
        }, extra_behavior="forbid", strict=True)

        def build(value):
            return value if type(value) is CollectionRef else CollectionRef(EntityKey(**value["owner"]), value["field"])

        return core_schema.no_info_after_validator_function(
            build, core_schema.json_or_python_schema(
                json_schema=wire,
                python_schema=core_schema.union_schema([
                    core_schema.is_instance_schema(CollectionRef), wire])),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda ref: ref.as_dict(), when_used="json", return_schema=wire))


class Owned(Generic[T]):
    """Declared owned sequence of Ref[T]; graph ownership is enforced by C1."""
    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return handler.generate_schema(tuple[Ref[get_args(source)[0]], ...])


class Resource(Generic[T]):
    """Declared process resource identity, not a graph resolver."""
    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return handler.generate_schema(Ref[get_args(source)[0]])


class ResourceCollection(Generic[T]):
    """Non-owning alias identity of a declared source collection; never a member tuple."""
    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return handler.generate_schema(CollectionRef[get_args(source)[0]])
