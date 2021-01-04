import logging
import os
from pathlib import Path
import sanic
import sqlalchemy.orm as orm
import typing
from typing import Text, Tuple, Generator, Dict, List, Set, Any, Optional, Union

import rasa.shared.data
from rasa.shared.core.domain import InvalidDomain
import rasax.community.constants as constants
import rasax.community.utils.cli as cli_utils
import rasax.community.utils.config as config_utils
import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
import rasax.community.utils.yaml as yaml_utils
import rasax.community.config as rasa_x_config
from rasax.community.services.user_service import UserService
from rasax.community.services.domain_service import DomainService
from rasax.community.services.story_service import StoryService

if typing.TYPE_CHECKING:
    from rasax.community.services.settings_service import SettingsService
    from rasax.community.services.role_service import RoleService
    from rasax.community.services.data_service import DataService

logger = logging.getLogger(__name__)


class InjectionError(Exception):
    """Error when something went wrong while injecting files into Rasa X."""


def inject_config(
    config_path: Text, settings_service: "SettingsService"
) -> Union[List[Any], Dict[Text, Any]]:
    """Load a configuration file from `path` and save it to the database.

    Quits the application if config cannot be loaded.
    """

    if not os.path.exists(config_path):
        raise InjectionError(
            f"Failed to inject Rasa configuration. The file "
            f"'{os.path.abspath(config_path)}' does not exist."
        )

    _config = yaml_utils.read_yaml_file(config_path)
    if not _config:
        raise InjectionError(
            f"Failed to inject Rasa configuration:\n"
            f"Reading of yaml '{os.path.abspath(config_path)}' file failed. Most "
            f"likely the file was not found or uses the wrong syntax."
        )

    settings_service.save_config(
        rasa_x_config.team_name, "default", _config, config_path, should_dump=False
    )

    logger.debug(
        "Loaded local configuration from '{}' into database".format(
            os.path.abspath(config_path)
        )
    )
    return _config


def _read_data(paths: List[Text]) -> Generator[Tuple[Text, Text], None, None]:
    for filename in paths:
        try:
            yield io_utils.read_file(filename), filename
        except ValueError:
            cli_utils.print_warning(f"Cannot read file {filename}")


def inject_nlu_data(
    nlu_files: Union[List[Text], Set[Text]],
    project_id: Text,
    username: Text,
    data_service: "DataService",
) -> None:
    """Load Rasa NLU training data from `path` and save it to the database.

    Args:
        nlu_files: NLU files the data from which needs to be saved.
        project_id: ID of the project.
        username: Name of the user.
        data_service: Service to obtain the current NLU training data.
    """

    # delete existing data in db if files are provided
    if nlu_files:
        data_service.delete_data()
        data_service.delete_additional_training_features(project_id)

    training_examples_count = data_service.save_bulk_data_from_files(
        nlu_files, project_id, username
    )

    logger.debug(f"Injected {training_examples_count} NLU training data examples.")


async def inject_stories(
    story_files: Union[List[Text], Set[Text]],
    story_service: "StoryService",
    team: Text,
    project_id: Text,
    username: Text,
) -> List[Dict[Text, Any]]:
    """Load Core stories from `data_directory` and save to the database.

    Args:
        story_files: A set of files that contain stories.
        story_service: Service to obtain the Rasa Core stories training data.
        team: Users' team.
        project_id: ID of the project.
        username: Name of the user.

    Returns:
        A list of story blocks saved from the provided `story_files`.
    """

    story_blocks = []

    if story_files:
        # delete existing data in db if files are provided
        story_service.delete_all_stories()

        # store provided stories in db
        story_blocks = await story_service.save_stories_from_files(
            story_files, team, project_id, username
        )

    logger.debug(f"Injected {len(story_blocks)} Core stories.")
    return story_blocks


def inject_domain(
    domain_path: Text, domain_service: "DomainService", project_id: Text, username: Text
) -> Dict[Text, Any]:
    """Load Rasa Core domain at `path` and save it to database.

    Quits the application if domain cannot be loaded.
    """

    if not os.path.exists(domain_path):
        raise InjectionError(
            f"domain.yml could not be found at '{os.path.abspath(domain_path)}'. "
            f"Rasa X requires a domain in the project root directory."
        )

    try:
        domain_service.validate_and_store_domain_yaml(
            domain_yaml=io_utils.read_file(domain_path),
            project_id=project_id,
            username=username,
            path=domain_path,
            store_responses=True,
            should_dump_domain=False,
        )

    except InvalidDomain as e:
        raise InjectionError(f"Could not inject domain. Details:\n{e}")

    return domain_service.get_or_create_domain(project_id, username)


def create_project_and_settings(
    _settings_service: "SettingsService", _role_service: "RoleService", team: Text
) -> None:
    """Create project and settings."""

    project_id = rasa_x_config.project_name

    existing = _settings_service.get(team, project_id)

    if existing is None:
        _settings_service.init_project(team, project_id)

    _role_service.init_roles(project_id=project_id)


def create_community_user(session: orm.Session) -> Tuple[Text, bool]:
    from rasax.community.services.role_service import RoleService
    from rasax.community.services.settings_service import SettingsService

    role_service = RoleService(session)
    role_service.init_roles(project_id=constants.COMMUNITY_PROJECT_NAME)

    settings_service = SettingsService(session)
    password = settings_service.get_community_user_password()
    is_password_generated = False

    # only re-assign password in local mode or if it doesn't exist
    if rasa_x_config.LOCAL_MODE or not password:
        password = os.environ.get(constants.ENV_RASA_X_PASSWORD)
        if not password:
            password = common_utils.random_password()
            is_password_generated = True
        settings_service.save_community_user_password(password)

    user_service = UserService(session)
    user_service.insert_or_update_user(
        constants.COMMUNITY_USERNAME, password, constants.COMMUNITY_TEAM_NAME
    )

    return (password, is_password_generated)


class AppStartedCallable:
    """A class that represents a callable that is called after the start of the
    application."""

    def __init__(self, password: Text, is_password_generated: bool) -> None:
        """The constructor for the AppStartedCallable

        Args:
            password: The password for the initial user.
            is_password_generated: Specifies if this password was randomly generated.
        """
        self.password = password
        self.is_password_generated = is_password_generated

    @staticmethod
    def open_web_browser(login_url: Text) -> None:
        """Opens a new tab on the user's preferred web browser and points it to `login_url`.
        Depending on the telemetry configuration, a separate tab may be opened as well,
        showing the user a welcome page.

        Args:
            login_url: URL which the tab should be pointed at.
        """

        import webbrowser

        telemetry_config = config_utils.read_global_config_value(
            constants.CONFIG_FILE_TELEMETRY_KEY
        )

        if telemetry_config and telemetry_config[constants.CONFIG_TELEMETRY_ENABLED]:
            # If the telemetry config does not contain CONFIG_TELEMETRY_WELCOME_SHOWN,
            # then the user has upgraded from a previous version of Rasa X (before
            # this config value was introduced). In these cases, assume that the
            # user has already seen the welcome page.
            if not telemetry_config.get(constants.CONFIG_TELEMETRY_WELCOME_SHOWN, True):
                webbrowser.open_new_tab(constants.WELCOME_PAGE_URL)

            telemetry_config[constants.CONFIG_TELEMETRY_WELCOME_SHOWN] = True
            config_utils.write_global_config_value(
                constants.CONFIG_FILE_TELEMETRY_KEY, telemetry_config
            )

        webbrowser.open_new_tab(login_url)

    def __call__(self) -> None:
        """Execute a set of actions that should be executed after the successful application start.
        In local mode, this callable prints a login url to console and opens a browser window.
        Otherwise, it checks if the password was generated, and then just prints this password if it was.
        """
        username = constants.COMMUNITY_USERNAME

        if not rasa_x_config.LOCAL_MODE:
            if self.is_password_generated:
                cli_utils.print_success(f"Your login password is '{self.password}'.")
        else:
            server_url = f"http://localhost:{rasa_x_config.self_port}"
            login_url = (
                f"{server_url}/login?username={username}&password={self.password}"
            )

            cli_utils.print_success(f"\nThe server is running at {login_url}\n")

            if rasa_x_config.OPEN_WEB_BROWSER:
                AppStartedCallable.open_web_browser(login_url)


async def inject_files_from_disk(
    project_path: Union[Path, Text],
    data_path: Text,
    session: orm.Session,
    config_path: Optional[Text],
    username: Optional[Text] = constants.COMMUNITY_USERNAME,
) -> None:
    """Injects local files into database.

    Args:
        project_path: Path to the project of which the data should be injected.
        data_path: Path to the data within this project.
        session: Database session.
        username: The username which is used to inject the data.
        config_path: Path to the config file within the project

    Raises:
        InjectionError: If anything goes wrong while injecting the data from the files.
    """
    from rasax.community.local import LOCAL_DOMAIN_PATH
    from rasax.community.services.data_service import DataService
    from rasax.community.services.settings_service import SettingsService

    io_utils.set_project_directory(project_path)

    domain_service = DomainService(session)
    inject_domain(
        os.path.join(project_path, LOCAL_DOMAIN_PATH),
        domain_service,
        constants.COMMUNITY_PROJECT_NAME,
        username,
    )

    settings_service = SettingsService(session)
    inject_config(os.path.join(project_path, config_path), settings_service)

    nlu_files = rasa.shared.data.get_data_files(
        [data_path], rasa.shared.data.is_nlu_file
    )
    story_files = rasa.shared.data.get_data_files(
        [data_path], rasa.shared.data.is_story_file
    )

    story_service = StoryService(session)
    await inject_stories(
        story_files,
        story_service,
        constants.COMMUNITY_TEAM_NAME,
        constants.COMMUNITY_PROJECT_NAME,
        username,
    )

    data_service = DataService(session)
    inject_nlu_data(nlu_files, constants.COMMUNITY_PROJECT_NAME, username, data_service)
