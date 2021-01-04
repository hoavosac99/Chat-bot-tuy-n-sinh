from typing import Text, List, Tuple, Set, Optional
from pathlib import Path
from enum import Enum, unique


@unique
class FileFormat(Enum):
    MARKDOWN = ".md"
    YAML = ".yml"
    JSON = ".json"
    GRAPHVIZ = ".dot"


_CONTENT_TYPES: List[Tuple[Set[Text], Set[Text], FileFormat]] = [
    # Tuples of:
    # - File extensions set
    # - MIME-type set
    # - Enum value
    ({".md"}, {"text/markdown", "text/x-markdown"}, FileFormat.MARKDOWN),
    (
        {".yml", ".yaml"},
        {"text/yaml", "text/x-yaml", "application/yaml", "application/x-yaml",},
        FileFormat.YAML,
    ),
    ({".json"}, {"application/json"}, FileFormat.JSON),
    ({".dot", ".gv"}, {"text/vnd.graphviz"}, FileFormat.GRAPHVIZ),
]


def format_from_mime_type(
    mime_type: Text, default: Optional[FileFormat] = None
) -> FileFormat:
    """Returns a `FileFormat` given a MIME type. Note that `mime_type` can
    contain a list of comma-separated types.

    Arguments:
        mime_type: MIME type as text.
        default: Default `FileFormat` to return if no matching type was found.

    Raises:
        ValueError: If MIME type could not be recognized.

    Returns:
        `FileFormat` corresponding to the MIME type.
    """
    types_list = [ct.strip() for ct in mime_type.split(",")]

    for type_entry in types_list:
        for _, content_types, file_format in _CONTENT_TYPES:
            if type_entry in content_types:
                return file_format

    if default:
        return default

    raise ValueError(f"No matches for MIME type '{mime_type}'.")


def format_from_filename(filename: Text) -> FileFormat:
    """Returns a `FileFormat` given a file path.

    Arguments:
        filename: String containing a file path.

    Raises:
        ValueError: If the file's format could not be determined.

    Returns:
        `FileFormat` corresponding to the file path.
    """
    suffix = Path(filename).suffix

    for extensions, _, file_format in _CONTENT_TYPES:
        if suffix in extensions:
            return file_format

    raise ValueError(f"Unknown file format for: '{filename}'.")
