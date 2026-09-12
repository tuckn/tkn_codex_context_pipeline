"""Validate every supplied configuration field before layered merging."""

from __future__ import annotations

from typing import Any, get_args, get_origin

from pydantic import BaseModel, TypeAdapter


def validate_config_layer(model: type[BaseModel], value: dict[str, Any], prefix: str = "") -> None:
    fields = {field.alias or name: field for name, field in model.model_fields.items()}
    for key, item in value.items():
        field = fields.get(key)
        location = f"{prefix}.{key}" if prefix else key
        if field is None:
            raise ValueError(f"unknown configuration key: {location}")
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if not isinstance(item, dict):
                raise ValueError(f"{location} must be a mapping")
            validate_config_layer(annotation, item, location)
        elif get_origin(annotation) is dict:
            key_type, item_type = get_args(annotation)
            if not isinstance(item, dict):
                raise ValueError(f"{location} must be a mapping")
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                for nested_key, nested_value in item.items():
                    TypeAdapter(key_type).validate_python(nested_key)
                    if not isinstance(nested_value, dict):
                        raise ValueError(f"{location}.{nested_key} must be a mapping")
                    validate_config_layer(item_type, nested_value, f"{location}.{nested_key}")
            else:
                TypeAdapter(field.rebuild_annotation()).validate_python(item)
        else:
            TypeAdapter(field.rebuild_annotation()).validate_python(item)
