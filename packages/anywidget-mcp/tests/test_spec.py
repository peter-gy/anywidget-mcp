from __future__ import annotations

import inspect
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    contextmanager,
)
from dataclasses import FrozenInstanceError
from functools import partial
from typing import Any, Awaitable, Generator, Sequence

from anywidget import AnyWidget
from anywidget._descriptor import MimeBundleDescriptor
import pytest
import traitlets as t

from anywidget_mcp._spec import (
    CallableSpec,
    Identity,
    InputSpec,
    JsonSchema,
    ModelCapabilities,
    ResultSpec,
    TargetCapabilities,
    TargetSpec,
    classify,
    compile_inputs,
    describe_model,
    identity,
    rename,
    scan,
)


class Counter2D(AnyWidget):
    """Adjust a two-dimensional counter."""

    _esm = "export default { render() {} };"
    value = t.Int(0, min=0, max=10, help="Current count").tag(sync=True)
    label = t.Unicode("Count").tag(sync=True, webmcp=False)
    internal = t.Int(0)
    readonly = t.Int(0, read_only=True).tag(sync=True)
    encoded = t.Int(0).tag(sync=True, to_json=lambda value, _widget: str(value))
    binary = t.Bytes().tag(sync=True)
    _hidden = t.Int(0).tag(sync=True)

    def __init__(self, value: int = 0, **kwargs: Any) -> None:
        super().__init__(value=value, **kwargs)


def create_counter(value: int = 0) -> Counter2D:
    """Create a counter."""
    raise AssertionError("Scanning must not invoke factories")


def test_target_facts_describe_class_instance_and_factory() -> None:
    definition = scan(Counter2D)
    assert definition.kind == "widget-class"
    assert definition.identity == Identity(
        "Counter2D", "counter_2d", "Counter 2D", "Adjust a two-dimensional counter."
    )
    assert definition.call is not None
    assert definition.call.parameters == (
        inspect.Parameter(
            "value", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=0, annotation=int
        ),
    )
    assert definition.call.result.widget_type is Counter2D
    assert definition.call.result.cardinality == "single"
    assert definition.capabilities == TargetCapabilities(True, True, True, True)
    widget = Counter2D()
    try:
        instance = scan(widget)
        assert instance.kind == "instance"
        assert instance.call is None
        assert instance.capabilities.create is False
    finally:
        widget.close()
    factory = scan(create_counter)
    assert factory.kind == "factory"
    assert factory.identity.name == "create_counter"
    assert factory.model == definition.model
    assert scan(AnyWidget).kind == "widget-class"


def test_identity_overrides_derive_title_from_name() -> None:
    base = identity(Counter2D)
    assert rename(base, name="anywidget_plot").title == "AnyWidget Plot"
    assert rename(base, description="").description == ""
    assert rename(base, title="Counter controls").title == "Counter controls"
    assert identity(
        Counter2D, name="count", title="Sales", description="Open counter"
    ) == Identity("Counter2D", "count", "Sales", "Open counter")
    assert rename(base).name == "counter_2d"


def test_trait_inventory_retains_metadata_and_write_restrictions() -> None:
    model = describe_model(Counter2D)
    value = model.trait("value")
    assert value.input_schema is not None
    assert value.input_schema.to_dict() == {
        "type": "integer",
        "minimum": 0,
        "maximum": 10,
        "description": "Current count",
    }
    assert value.default == 0
    assert model.trait("label").metadata["webmcp"] is False
    assert "label" in model.default_state_names
    assert "_hidden" in model.synchronized_names
    assert "_hidden" not in model.default_state_names
    assert "layout" not in model.default_state_names
    assert "internal" in model.capabilities.observe
    assert "internal" in model.capabilities.read
    assert "value" in model.capabilities.write
    assert {
        name: model.trait(name).write_restriction
        for name in ("internal", "readonly", "encoded", "binary")
    } == {
        "internal": "unsynchronized",
        "readonly": "read-only",
        "encoded": "custom-serialization",
        "binary": "unsupported-type",
    }


def test_trait_scanning_preserves_lazy_defaults_and_dynamic_traits() -> None:
    class Dynamic(AnyWidget):
        value = t.Int().tag(sync=True)
        default_calls = 0

        @t.default("value")
        def _value_default(self) -> int:
            self.default_calls += 1
            return 7

    describe_model(Dynamic)
    assert Dynamic.default_calls == 0
    widget = Dynamic()
    try:
        widget.add_traits(extra=t.Unicode("extra").tag(sync=True))
        before = widget.default_calls
        model = describe_model(widget)
        assert "extra" in model.synchronized_names
        assert "extra" not in describe_model(Dynamic).synchronized_names
        assert widget.default_calls == before
    finally:
        widget.close()


def test_instance_metadata_overrides_control_json_write_capability() -> None:
    widget = Counter2D()
    try:
        setattr(
            widget, "_value_metadata", {"to_json": lambda value, _widget: str(value)}
        )
        model = describe_model(widget)
        assert model.trait("value").write_restriction == "custom-serialization"
        assert model.trait("value").input_schema is None
        assert "value" not in model.capabilities.write
        assert "value" in describe_model(Counter2D).capabilities.write
        setattr(widget, "_encoded_metadata", {"to_json": None})
        encoded_schema = describe_model(widget).trait("encoded").input_schema
        assert encoded_schema is not None
        assert encoded_schema.to_dict() == {"type": "integer"}
    finally:
        widget.close()


def test_protocol_descriptor_is_inspected_before_comm_acquisition() -> None:
    class Descriptor(MimeBundleDescriptor):
        def __get__(self, instance: object, owner: type) -> Any:
            if instance is not None:
                raise AssertionError("Descriptor must remain unbound during scanning")
            return self

    class ProtocolModel(t.HasTraits):
        value = t.Int(3).tag(sync=True)
        _repr_mimebundle_ = Descriptor()

    model = ProtocolModel()
    spec = scan(model)
    assert spec.kind == "instance"
    assert spec.model is not None
    assert spec.model.default_state_names == ("value",)
    assert spec.capabilities == TargetCapabilities(False, True, True, True)


def test_observer_only_models_expose_observation_names() -> None:
    class Observable:
        def trait_names(self) -> list[str]:
            return ["value", "child"]

        def observe(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Scanning must not register observers")

        def unobserve(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Scanning must not detach observers")

    definition = describe_model(Observable())
    assert definition.traits == ()
    assert definition.capabilities == ModelCapabilities((), (), ("value", "child"))


@pytest.mark.parametrize(
    "annotation, cardinality, managed",
    [
        (Counter2D, "single", False),
        (list[Counter2D], "sequence", False),
        (Sequence[Counter2D], "sequence", False),
        (AbstractContextManager[Counter2D], "single", True),
        (AbstractAsyncContextManager[list[Counter2D]], "sequence", True),
        (Awaitable[Counter2D], "single", False),
    ],
)
def test_factory_result_annotations_describe_model_acquisition(
    annotation: Any, cardinality: str, managed: bool
) -> None:
    def factory() -> Any:
        raise AssertionError("Return inference must not invoke the factory")

    factory.__annotations__["return"] = annotation
    definition = scan(factory)
    assert definition.call is not None
    assert definition.call.result.widget_type is Counter2D
    assert definition.call.result.cardinality == cardinality
    assert definition.call.result.managed is managed


def test_contextmanager_factory_infers_managed_result() -> None:
    @contextmanager
    def factory() -> Generator[Counter2D, None, None]:
        raise AssertionError("Scanning must not enter the manager")
        yield Counter2D()

    spec = scan(factory)
    assert spec.call is not None
    assert spec.call.result.widget_type is Counter2D
    assert spec.call.result.managed is True


def test_unresolved_return_annotations_remain_advisory() -> None:
    def factory(value: int):
        raise AssertionError

    factory.__annotations__["return"] = "MissingLocalWidget"
    spec = scan(factory)
    assert spec.call is not None
    assert spec.call.result.annotation == "MissingLocalWidget"
    assert spec.call.result.widget_type is None
    assert spec.model is None
    assert compile_inputs(spec).validate({"value": 3}) == {"value": 3}


def test_partial_and_class_local_parameter_annotations_resolve_once() -> None:
    class LocalWidget(AnyWidget):
        Number = int

        def __init__(self, count: Number, **kwargs: Any) -> None:
            raise AssertionError("Class scanning must not construct widgets")

    spec = scan(LocalWidget)
    assert spec.call is not None
    assert spec.call.parameters[0].annotation is int
    partial_spec = scan(partial(create_counter, value=4))
    assert (
        compile_inputs(partial_spec).schema.to_dict()["properties"]["value"]["default"]
        == 4
    )


def test_unannotated_inputs_are_facts_for_the_selected_compiler() -> None:
    def factory(value):
        raise AssertionError

    spec = scan(factory)
    assert spec.call is not None
    assert spec.call.parameters[0].annotation is inspect.Parameter.empty
    with pytest.raises(TypeError, match="requires a type annotation"):
        compile_inputs(spec)


def test_custom_inspector_and_input_compiler_extend_the_shared_pipeline() -> None:
    source = object()
    calls: list[object] = []
    call = CallableSpec(
        lambda **arguments: arguments,
        inspect.Signature([inspect.Parameter("value", inspect.Parameter.KEYWORD_ONLY)]),
        ResultSpec(),
        False,
    )
    definition = TargetSpec(
        source,
        "factory",
        Identity("external", "external", "External", "External model"),
        call,
        None,
        TargetCapabilities(True, False, False, False),
    )

    def inspector(candidate: object) -> TargetSpec:
        calls.append(candidate)
        return definition

    def compiler(candidate: CallableSpec) -> InputSpec:
        calls.append(candidate)
        return InputSpec(
            candidate.signature,
            JsonSchema({"type": "object"}),
            lambda arguments: {"value": str(arguments["value"])},
        )

    result = compile_inputs(scan(source, inspector=inspector), compiler)
    assert calls == [source, call]
    assert result.schema.to_dict() == {"type": "object"}
    assert result.validate({"value": 4}) == {"value": "4"}


def test_classification_precedes_signature_and_trait_compilation() -> None:
    def factory(value):
        raise AssertionError("Classification must not call factories")

    factory.__annotations__["value"] = "UnknownInput"
    assert classify(factory) == "factory"
    assert classify(AnyWidget) == "widget-class"
    with pytest.raises(TypeError, match="Unable to evaluate type annotations"):
        scan(factory)


def test_specification_snapshots_isolate_consumers_from_mutable_inputs() -> None:
    original = {"type": "array", "items": {"enum": [1, 2]}}
    schema = JsonSchema(original)
    original["items"]["enum"].append(3)
    exported = schema.to_dict()
    exported["items"]["enum"].append(4)
    assert schema.to_dict() == {"type": "array", "items": {"enum": [1, 2]}}

    def callback(value: Any) -> Any:
        return value

    default = [1, {"label": "one"}]
    metadata = {"view": {"labels": ["one"]}, "codec": callback}
    from anywidget_mcp._spec import TraitSpec

    trait = TraitSpec(
        "value", "Any", "", True, False, False, default, metadata, schema, True, None
    )
    default.append(3)
    metadata["view"]["labels"].append("two")
    assert trait.default == (1, {"label": "one"})
    assert trait.metadata["view"]["labels"] == ("one",)
    assert trait.metadata["codec"] is callback
    with pytest.raises(TypeError):
        trait.default[1]["label"] = "changed"
    with pytest.raises(TypeError):
        trait.metadata["view"]["labels"] = ("changed",)

    definition = scan(create_counter)
    with pytest.raises(FrozenInstanceError):
        setattr(definition, "kind", "instance")
    inputs = compile_inputs(definition)
    inputs.schema.to_dict()["properties"]["value"]["type"] = "string"
    assert inputs.schema.to_dict()["properties"]["value"]["type"] == "integer"
    assert inputs.validate({"value": 2}) == {"value": 2}


def test_cyclic_trait_metadata_and_defaults_allow_mcp_registration() -> None:
    from anywidget_mcp._mcp.targets import prepare_target
    from anywidget_mcp._spec.types import ValueReference

    cycle: dict[str, Any] = {}
    cycle["self"] = cycle

    class Counter(AnyWidget):
        value = t.Int().tag(sync=True, display=cycle)
        cache = t.Any(default_value=cycle)

    target = prepare_target(Counter)
    assert target.tool.name == "counter"
    assert target.spec.model is not None
    model = target.spec.model
    assert isinstance(model.trait("value").metadata["display"]["self"], ValueReference)
    assert model.trait("cache").default["self"] == ValueReference(())
    cycle["later"] = 1
    assert "later" not in model.trait("value").metadata["display"]
    assert "later" not in model.trait("cache").default


def test_native_sync_inventory_matches_serialized_child_references() -> None:
    from anywidget import WidgetTrait
    from anywidget_mcp._widget_protocol import collect_widgets

    class Parent(AnyWidget):
        child = WidgetTrait().tag(sync=True)

    child = Counter2D()
    parent = Parent(child=child)
    try:
        setattr(parent, "_child_metadata", {"sync": False})
        assert parent.get_state("child") == {"child": f"anywidget:{child.model_id}"}
        assert "child" in describe_model(parent).synchronized_names
        assert collect_widgets(parent) == [parent, child]
    finally:
        parent.close()
        child.close()
