import glob
import errno
import json
import os
import logging
import tarfile
import tempfile
from pathlib import Path
from typing import Any, List, Text, Union, Optional
from sanic.request import File

import rasax.community.config as rasa_x_config

logger = logging.getLogger(__name__)
DEFAULT_ENCODING = "utf-8"


def set_project_directory(directory: Union[Path, Text]) -> None:
    """Sets the path to the current project directory."""

    with rasa_x_config.PROJECT_DIRECTORY.get_lock():
        rasa_x_config.PROJECT_DIRECTORY.value = str(directory).encode(DEFAULT_ENCODING)


def get_project_directory() -> Path:
    """Returns the path to the current project directory."""

    if not rasa_x_config.PROJECT_DIRECTORY.value:
        return Path()
    else:
        return Path(rasa_x_config.PROJECT_DIRECTORY.value.decode(DEFAULT_ENCODING))


def create_directory(directory_path: Text) -> None:
    """Creates a directory and its super paths.

    Succeeds even if the path already exists."""

    try:
        os.makedirs(directory_path)
    except OSError as e:
        # be happy if someone already created the path
        if e.errno != errno.EEXIST:
            raise


def _filename_without_prefix(file: Text) -> Text:
    """Splits of a filenames prefix until after the first ``_``."""
    return "_".join(file.split("_")[1:])


def list_directory(path: Text) -> List[Text]:
    """Returns all files and folders excluding hidden files.

    If the path points to a file, returns the file. This is a recursive
    implementation returning files in any depth of the path."""

    if os.path.isfile(path):
        return [path]
    elif os.path.isdir(path):
        results = []
        for base, dirs, files in os.walk(path, followlinks=True):
            # sort files for same order across runs
            files = sorted(files, key=_filename_without_prefix)
            # add not hidden files
            good_files = filter(lambda x: not x.startswith("."), files)
            results.extend(os.path.join(base, f) for f in good_files)
            # add not hidden directories
            good_directories = filter(lambda x: not x.startswith("."), dirs)
            results.extend(os.path.join(base, f) for f in good_directories)
        return results
    else:
        raise ValueError(
            "Could not locate the resource '{}'.".format(os.path.abspath(path))
        )


def list_files(path: Text) -> List[Text]:
    """Returns all files excluding hidden files.

    If the path points to a file, returns the file."""

    return [fn for fn in list_directory(path) if os.path.isfile(fn)]


def list_subdirectories(path: Text) -> List[Text]:
    """Returns all folders excluding hidden files.

    If the path points to a file, returns an empty list."""

    return [fn for fn in glob.glob(os.path.join(path, "*")) if os.path.isdir(fn)]


def should_dump() -> bool:
    """Whether data should be dumped to disk."""
    return bool(rasa_x_config.PROJECT_DIRECTORY.value)


def create_path(file_path: Text) -> None:
    """Makes sure all directories in the 'file_path' exists."""

    parent_dir = os.path.dirname(os.path.abspath(file_path))
    if not os.path.exists(parent_dir):
        os.makedirs(parent_dir)


def create_temporary_file(data: Any, suffix: Text = "", mode: Text = "w+") -> Text:
    """Creates a tempfile.NamedTemporaryFile object for data.

    mode defines NamedTemporaryFile's  mode parameter in py3."""

    encoding = None if "b" in mode else DEFAULT_ENCODING
    f = tempfile.NamedTemporaryFile(
        mode=mode, suffix=suffix, delete=False, encoding=encoding
    )
    f.write(data)

    f.close()
    return f.name


def write_file(
    file_path: Union[Text, Path],
    content: Any,
    encoding: Text = DEFAULT_ENCODING,
    mode: Text = "w",
) -> None:
    """Writes text to a file.

    Args:
        file_path: The path to which the content should be written.
        content: The content to write.
        encoding: The encoding which should be used.
        mode: The mode in which the file is opened.
    """
    create_path(file_path)

    with open(file_path, mode, encoding=encoding if "b" not in mode else None) as file:
        file.write(content)


def read_file(filename: Union[Text, Path], encoding: Text = DEFAULT_ENCODING) -> Any:
    """Read text from a file."""

    try:
        with open(filename, encoding=encoding) as f:
            return f.read()
    except FileNotFoundError:
        raise ValueError(f"File '{filename}' does not exist.")


def read_file_as_bytes(path: str) -> bytes:
    """Read in a file as a byte array."""
    with open(path, "rb") as f:
        return f.read()


def convert_bytes_to_string(data: Union[bytes, bytearray, Text]) -> Text:
    """Convert `data` to string if it is a bytes-like object."""

    if isinstance(data, (bytes, bytearray)):
        return data.decode(DEFAULT_ENCODING)

    return data


def read_json_file(filename: Union[Text, Path]) -> Any:
    """Read json from a file."""
    content = read_file(filename)
    try:
        return json.loads(content)
    except ValueError as e:
        raise ValueError(
            "Failed to read json from '{}'. Error: "
            "{}".format(os.path.abspath(filename), e)
        )


def write_request_file_to_disk(_file: File, filename: Text) -> Text:
    """Write the request file to a temporary file and return the path."""

    tdir = tempfile.mkdtemp()
    tpath = os.path.join(tdir, filename)
    write_file(tpath, _file.body, mode="wb")
    return tpath


def unpack_file(
    model_file: Text, working_directory: Optional[Union[Path, Text]] = None
) -> Text:
    """Unpack a zipped file.

    Args:
        model_file: Path to zipped file.
        working_directory: Location where the file should be unpacked to.
            If `None` a temporary directory will be created.

    Returns:
        Path to unpacked directory.

    """
    if working_directory is None:
        working_directory = tempfile.mkdtemp()

    # All files are in a subdirectory.
    try:
        with tarfile.open(model_file, mode="r:gz") as tar:
            tar.extractall(working_directory)
            logger.debug(f"Extracted model to '{working_directory}'.")
    except Exception as e:
        logger.error(f"Failed to extract model at {model_file}. Error: {e}")
        raise

    return str(working_directory)
