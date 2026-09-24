"""Compare supported broker contracts without pinning documentation wording."""

from functools import lru_cache
import json
from pathlib import Path

ANNOTATIONS = {"description", "title", "examples", "$comment"}
SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
SCHEMA_CHILDREN = {"items", "additionalProperties", "additionalItems", "contains",
                   "propertyNames", "not", "if", "then", "else", "unevaluatedProperties",
                   "unevaluatedItems"}
SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}
PAGINATED_TOOLS = {"get_option_chains", "get_option_instruments", "get_option_orders", "get_option_positions"}


def schema_shape(schema):
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in ANNOTATIONS:
            continue
        if key in SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {name: schema_shape(child) for name, child in value.items()}
        elif key in SCHEMA_CHILDREN:
            result[key] = schema_shape(value)
        elif key in SCHEMA_LISTS and isinstance(value, list):
            result[key] = [schema_shape(child) for child in value]
        elif key == "required" and isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[key] = sorted(value)
        else:
            # Defaults, enum values and unknown constraints are data, not annotations.
            result[key] = value
    return result


@lru_cache(maxsize=1)
def supported_contracts():
    return json.loads(Path(__file__).with_name("schema-contracts.json").read_text())["tools"]


def pagination_description(tool):
    if tool.get("name") not in PAGINATED_TOOLS:
        return None
    node = tool.get("outputSchema")
    for key in ("properties", "data", "properties", "next"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node.get("description") if isinstance(node, dict) else None


def _differences(expected, actual, path, *, output=False):
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return [] if json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True) else [path + " changed"]
    problems = []
    for key in sorted(expected.keys() | actual.keys()):
        location = path + "." + key
        if key == "required":
            left, right = expected.get(key, []), actual.get(key, [])
            if isinstance(left, list) and isinstance(right, list) and all(isinstance(item, str) for item in left + right):
                for name in sorted(set(right) - set(left)):
                    problems.append(location + " added " + name)
                for name in sorted(set(left) - set(right)):
                    problems.append(location + " removed " + name)
                continue
        if key not in expected:
            problems.append(location + " added")
        elif key not in actual:
            problems.append(location + " removed")
        elif key in SCHEMA_MAPS and isinstance(expected[key], dict) and isinstance(actual[key], dict):
            for name in sorted(expected[key].keys() | actual[key].keys()):
                field = location + "." + name
                if name not in expected[key]:
                    if not (output and key == "properties"):
                        problems.append(field + " added")
                elif name not in actual[key]:
                    problems.append(field + " removed")
                else:
                    problems.extend(_differences(expected[key][name], actual[key][name], field, output=output))
        elif key in SCHEMA_CHILDREN:
            problems.extend(_differences(expected[key], actual[key], location, output=output))
        elif key in SCHEMA_LISTS and isinstance(expected[key], list) and isinstance(actual[key], list) and len(expected[key]) == len(actual[key]):
            for index, (left, right) in enumerate(zip(expected[key], actual[key])):
                problems.extend(_differences(left, right, f"{location}[{index}]", output=output))
        elif json.dumps(expected[key], sort_keys=True) != json.dumps(actual[key], sort_keys=True):
            problems.append(location + " changed")
    return problems


def schema_problem(tool):
    profiles = supported_contracts().get(tool.get("name"), [])
    if not profiles:
        return "tool has no supported contract"
    actual_input = schema_shape(tool.get("inputSchema"))
    actual_output = schema_shape(tool.get("outputSchema"))
    best = None
    for profile in profiles:
        problems = _differences(profile["inputSchema"], actual_input, "inputSchema")
        problems += _differences(profile["outputSchema"], actual_output, "outputSchema", output=True)
        if pagination_description(tool) != profile["pagination_description"]:
            problems.append("outputSchema.properties.data.properties.next.description changed (pagination contract)")
        if not problems:
            return None
        if best is None or len(problems) < len(best):
            best = problems
    return "; ".join(best[:2]) + (f"; {len(best) - 2} more changes" if len(best) > 2 else "")


def cursor_mode(tool):
    modes = {profile["cursor_mode"] for profile in supported_contracts().get(tool.get("name"), [])
             if profile["pagination_description"] == pagination_description(tool)}
    return next(iter(modes)) if len(modes) == 1 else None
