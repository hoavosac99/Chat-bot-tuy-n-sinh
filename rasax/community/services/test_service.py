import re
import os
import logging
from typing import Optional, Text, List
from pathlib import Path

import rasax.community.config as rasa_x_config
import rasax.community.utils.cli as cli_utils
import rasax.community.utils.io as io_utils

logger = logging.getLogger(__name__)

DEFAULT_FILENAME = str(
    Path(rasa_x_config.default_e2e_tests_dir) / rasa_x_config.default_e2e_test_file_path
)


def _split_tests(tests_string: Text) -> List[Text]:
    split_tests_string = re.split("(\n|^)##?", tests_string)
    return [s.strip() for s in split_tests_string if s not in ("", "\n")]


def get_tests_from_file(filename: Optional[Text] = None) -> List[Text]:
    """Returns an list of tests from a `filename`.

    Args:
        filename: Path to a test file.
    """

    if not filename:
        filename = io_utils.get_project_directory() / DEFAULT_FILENAME

    try:
        content = io_utils.read_file(filename)
        return _split_tests(content)
    except ValueError as e:
        cli_utils.raise_warning(
            f"Unable to get tests from {filename}:\n{e} "
            f"Please, make sure you have end-to-end tests added to your assistant. "
            f"See https://rasa.com/docs/rasa-x/user-guide/test-assistant "
            f"for more information.",
            UserWarning,
        )
        return []


def delete_tests_from_file(filename: Optional[Text] = None):
    """Deletes a file.

    Args:
        filename: Path to a test file.
    """

    if not filename:
        filename = io_utils.get_project_directory() / DEFAULT_FILENAME

    try:
        os.remove(filename)
    except OSError:
        logger.exception(f"Unable to delete tests from {filename}")


class TestService:
    """Service which operates with tests."""

    @staticmethod
    def save_tests(tests_string: Text, filename: Optional[Text] = None) -> List[Text]:
        """Saves `test_string` to a file `filename`.

        Args:
            tests_string: Test that needs to be saved.
            filename: Path to a test file.
        """

        if not filename:
            filename = io_utils.get_project_directory() / DEFAULT_FILENAME

        existing_tests = get_tests_from_file(filename)
        new_tests = [
            test for test in _split_tests(tests_string) if test not in existing_tests
        ]

        if new_tests:
            all_tests = [f"## {test}\n" for test in existing_tests + new_tests]

            io_utils.create_directory(os.path.dirname(filename))
            io_utils.write_file(filename, "\n".join(all_tests))

        return [f"## {test}" for test in new_tests]
