import re
import os
import io
from pathlib import Path
from typing import Any, Dict, List, Text, TextIO, Union, Optional
from collections import OrderedDict

import ruamel.yaml as yaml
from ruamel.yaml.comments import CommentedMap

import rasax.community.utils.io as io_utils

YAML_VERSION = (1, 2)


def _dump_yaml(obj: Dict, output: Union[Text, Path, io.StringIO]) -> None:
    yaml_writer = yaml.YAML(pure=True, typ="safe")
    yaml_writer.unicode_supplementary = True
    yaml_writer.default_flow_style = False
    yaml_writer.version = YAML_VERSION

    yaml_writer.dump(obj, output)


def _fix_yaml_loader() -> None:
    """Ensure that any string read by yaml is represented as unicode."""

    def construct_yaml_str(self, node):
        # Override the default string handling function
        # to always return unicode objects
        return self.construct_scalar(node)

    yaml.Loader.add_constructor("tag:yaml.org,2002:str", construct_yaml_str)
    yaml.SafeLoader.add_constructor("tag:yaml.org,2002:str", construct_yaml_str)


def _replace_yaml_environment_variables() -> None:
    """Enable yaml loader to process the environment variables in the yaml."""

    # eg. ${USER_NAME}, ${PASSWORD}
    env_var_pattern = re.compile(r"^(.*)\$\{(.*)\}(.*)$")
    yaml.add_implicit_resolver("!env_var", env_var_pattern)

    def env_var_constructor(loader, node):
        """Process environment variables found in the YAML."""
        value = loader.construct_scalar(node)
        expanded_vars = os.path.expandvars(value)
        if "$" in expanded_vars:
            not_expanded = [w for w in expanded_vars.split() if "$" in w]
            raise ValueError(
                "Error when trying to expand the environment variables"
                " in '{}'. Please make sure to also set these environment"
                " variables: '{}'.".format(value, not_expanded)
            )
        return expanded_vars

    yaml.SafeConstructor.add_constructor("!env_var", env_var_constructor)


def _is_ascii(text: Text) -> bool:
    return all(ord(character) < 128 for character in text)


def _enable_ordered_dict_yaml_dumping() -> None:
    """Ensure that `OrderedDict`s are dumped so that the order of keys is respected."""

    def _order_rep(dumper: yaml.Representer, _data: Dict[Any, Any]) -> Any:
        return dumper.represent_mapping(
            "tag:yaml.org,2002:map", _data.items(), flow_style=False
        )

    yaml.add_representer(OrderedDict, _order_rep)


def _convert_to_ordered_dict(obj: Any) -> Any:
    """Convert object to an `OrderedDict`.

    Args:
        obj: Object to convert.

    Returns:
        An `OrderedDict` with all nested dictionaries converted if `obj` is a
        dictionary, otherwise the object itself.
    """
    # use recursion on lists
    if isinstance(obj, list):
        return [_convert_to_ordered_dict(element) for element in obj]

    if isinstance(obj, dict):
        out = OrderedDict()
        # use recursion on dictionaries
        for k, v in obj.items():
            out[k] = _convert_to_ordered_dict(v)

        return out

    # return all other objects
    return obj


def load_yaml(content: Union[str, TextIO]) -> Any:
    """Load content from yaml."""
    return yaml.round_trip_load(content)


def read_yaml(content: Text) -> Union[List[Any], Dict[Text, Any]]:
    """Parses yaml from a text.

     Args:
        content: A text containing yaml content.
    """
    _fix_yaml_loader()

    _replace_yaml_environment_variables()

    yaml_parser = yaml.YAML(typ="safe")
    yaml_parser.version = YAML_VERSION

    if _is_ascii(content):
        # Required to make sure emojis are correctly parsed
        content = (
            content.encode("utf-8")
            .decode("raw_unicode_escape")
            .encode("utf-16", "surrogatepass")
            .decode("utf-16")
        )

    return yaml_parser.load(content) or {}


def read_yaml_file(filename: Text) -> Union[List[Any], Dict[Text, Any]]:
    """Parses a yaml file.

     Args:
        filename: The path to the file which should be read.
    """
    return read_yaml(io_utils.read_file(filename, io_utils.DEFAULT_ENCODING))


def write_yaml_file(
    data: Any, filename: Union[Text, Path], should_preserve_key_order: bool = False
) -> None:
    """Writes a yaml file.

    Args:
        data: The data to write.
        filename: The path to the file which should be written.
        should_preserve_key_order: Whether to preserve key order in `data`.
    """
    if should_preserve_key_order:
        _enable_ordered_dict_yaml_dumping()
        data = _convert_to_ordered_dict(data)

    with Path(filename).open("w", encoding=io_utils.DEFAULT_ENCODING) as outfile:
        yaml.dump(data, outfile, default_flow_style=False, allow_unicode=True)


def dump_yaml(content: Any) -> Optional[str]:
    """Dump content to yaml."""

    _content = CommentedMap(content)
    return yaml.round_trip_dump(_content, default_flow_style=False)


def dump_as_yaml_to_temporary_file(data: Dict) -> Optional[Text]:
    """Dump `data` as yaml to a temporary file."""

    content = dump_yaml(data)
    return io_utils.create_temporary_file(content)


def dump_obj_as_yaml_to_string(obj: Dict) -> Text:
    """Writes data (python dict) to a yaml string."""
    str_io = io.StringIO()
    _dump_yaml(obj, str_io)
    return str_io.getvalue()


# unlike rasa.utils.io's yaml writing method, this one
# uses round_trip_dump() which preserves key order and doesn't print yaml markers
def dump_yaml_to_file(filename: Union[Text, Path], content: Any) -> None:
    """Dump content to yaml."""
    io_utils.write_file(filename, dump_yaml(content))


# TODO(alwx): We need to get rid of this function 'cause it's redundant. However,
# I tried some approaches and they didn't work. Need to investigate further.
def extract_partial_endpoint_config(
    endpoint_config_path: Text, key: Text
) -> Optional[Dict]:
    """Extracts partial endpoint config at `key`.

    Args:
        endpoint_config_path: Path to endpoint config file to read.
        key: Endpoint config key (section) to extract.

    Returns:
        Endpoint config initialised only from `key`.
    """

    # read endpoint config file and create dictionary containing only one
    # key-value pair
    content = io_utils.read_file(endpoint_config_path)
    endpoint_dict = {key: load_yaml(content)[key]}

    # dump this sub-dictionary to a temporary file and load endpoint config from it
    temp_path = dump_as_yaml_to_temporary_file(endpoint_dict)

    yaml_content = read_yaml_file(temp_path)
    if type(yaml_content) is dict and key in yaml_content:
        return yaml_content[key]

    return None
