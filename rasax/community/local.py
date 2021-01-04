import argparse
import asyncio  # pytype: disable=pyi-error
import logging
import os
import signal
import typing
from typing import Text, Tuple, Any, Union, Optional

import rasax.community.initialise as initialise
from sqlalchemy.orm import Session

import rasax.community.config as rasa_x_config
import rasax.community.constants as constants
import rasax.community.jwt
import rasax.community.utils.config as config_utils
import rasax.community.utils.common as common_utils
import rasax.community.utils.cli as cli_utils
from rasax.community import telemetry, sql_migrations, scheduler, global_state
from rasax.community.database.utils import session_scope
from rasax.community.api.app import initialize_app
import rasax.community.server as rasa_x_server
from rasax.community.services import model_service
from rasax.community.services.domain_service import DomainService
from rasax.community.services.settings_service import (
    SettingsService,
    default_environments_config_local,
)

if typing.TYPE_CHECKING:
    from sanic import Sanic

logger = logging.getLogger(__name__)

NUMBER_OF_SANIC_WORKERS = 1

LOCAL_DATA_DIR = "data"
LOCAL_DEFAULT_NLU_FILENAME = "nlu.md"
LOCAL_DEFAULT_STORIES_FILENAME = "stories.yml"
LOCAL_DOMAIN_PATH = "domain.yml"
LOCAL_MODELS_DIR = "models"
LOCAL_ENDPOINTS_PATH = "endpoints.yml"


def _configure_for_local_server(
    data_path: Text, config_path: Text, token: Optional[Text] = None
) -> None:
    """Create `models` directory and set variables for local mode.

    Sets the API-wide token if provided.
    """

    if not os.path.isdir(LOCAL_MODELS_DIR):
        os.makedirs(LOCAL_MODELS_DIR)

    if token is not None:
        rasa_x_config.rasa_x_token = token

    rasa_x_config.data_dir = data_path
    rasa_x_config.rasa_model_dir = LOCAL_MODELS_DIR
    rasa_x_config.project_name = constants.COMMUNITY_PROJECT_NAME
    rasa_x_config.team_name = constants.COMMUNITY_TEAM_NAME
    rasa_x_config.data_dir = LOCAL_DATA_DIR
    rasa_x_config.default_nlu_filename = LOCAL_DEFAULT_NLU_FILENAME
    rasa_x_config.default_stories_filename = LOCAL_DEFAULT_STORIES_FILENAME
    rasa_x_config.default_username = constants.COMMUNITY_USERNAME
    rasa_x_config.default_domain_path = LOCAL_DOMAIN_PATH
    rasa_x_config.default_config_path = config_path
    rasa_x_config.endpoints_path = LOCAL_ENDPOINTS_PATH


def check_license_and_telemetry(args: argparse.Namespace) -> None:
    """Ask the user to accept terms and conditions and initialize global variables.

    If already accepted, skip it. Also, prompt the user to set the telemetry settings.

    Args:
        args: Parsed command line arguments.
    """
    if not config_utils.are_terms_accepted():
        cli_utils.accept_terms_or_raise(args)

    telemetry.initialize_from_file(args.no_prompt)


def _enable_development_mode_and_get_additional_auth_endpoints(
    app: "Sanic",
) -> Union[Tuple[()], Tuple[Text, Any]]:
    """Enable development mode if Rasa Enterprise is installed.

    Configures enterprise endpoints and returns additional authentication
    endpoints if possible.

    Args:
        app: Sanic app to configure.

    Returns:
        Tuple of authentication endpoints if Rasa Enterprise is installed and
        Rasa X is run in development, otherwise an empty tuple.

    """
    if rasa_x_config.development_mode:
        if not common_utils.is_enterprise_installed():
            raise Exception(
                "Rasa Enterprise is not installed. Using enterprise endpoints in "
                "local development mode requires an installation of "
                "Rasa Enterprise."
            )

        import rasax.enterprise.server as rasa_x_enterprise_server  # pytype: disable=import-error

        rasa_x_enterprise_server.configure_enterprise_endpoints(app)

        return rasa_x_enterprise_server.additional_auth_endpoints()

    return ()


def _event_service(endpoints_path: Text) -> None:
    """Start the event service."""
    # noinspection PyUnresolvedReferences
    from rasax.community.services import event_service

    # set endpoints path variable in this process
    rasa_x_config.endpoints_path = endpoints_path

    def signal_handler(sig, frame):
        print("Stopping event service.")
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGINT, signal_handler)

    event_service.main(should_run_liveness_endpoint=False)


def _start_event_service() -> None:
    """Run the event service in a separate process."""

    common_utils.run_in_process(
        fn=_event_service, args=(LOCAL_ENDPOINTS_PATH,), daemon=True
    )


def _initialize_with_local_data(
    project_path: Text,
    data_path: Text,
    session: Session,
    rasa_port: Union[int, Text],
    config_path: Text,
) -> None:

    try:
        settings_service = SettingsService(session)
        default_env = default_environments_config_local(rasa_port)
        settings_service.save_environments_config(
            constants.COMMUNITY_PROJECT_NAME, default_env.get("environments")
        )

        loop = asyncio.get_event_loop()
        # inject data
        loop.run_until_complete(
            rasax.community.initialise.inject_files_from_disk(
                project_path, data_path, session, config_path=config_path
            )
        )

        # dump domain once
        domain_service = DomainService(session)
        domain_service.dump_domain(rasa_x_config.project_name)

    except initialise.InjectionError as e:
        logger.debug(f"An error happened when injecting the local project: {e}")
        cli_utils.print_error_and_exit(str(e))


def main(
    args: argparse.Namespace,
    project_path: Text,
    data_path: Text,
    token: Optional[Text] = None,
    config_path: Optional[Text] = None,
) -> None:
    """Start Rasa X in local mode.

    Args:
        args: The parsed command line arguments which were passed to `rasa x`.
        project_path: The path to the Rasa Open Source project which includes training
            data and others.
        data_path: The path to the training data within `project_path`.
        token: The token which will be used by Rasa Open Source to authenticate
            against the Rasa X API.
        config_path: The path to the `config.yml` within the `project_path`.
    """
    # Initialize the global state before doing anything else.
    # This will set config.LOCAL_MODE to True!
    global_state.initialize_global_state(NUMBER_OF_SANIC_WORKERS, is_local_mode=True)

    # Make sure the user has agreed to the Rasa X license.
    check_license_and_telemetry(args)

    if config_path is None:
        config_path = rasa_x_config.default_config_path

    cli_utils.print_success("Starting Rasa X in local mode... 🚀")

    rasa_x_config.self_port = args.rasa_x_port

    _configure_for_local_server(data_path, config_path, token)

    rasax.community.jwt.initialise_jwt_keys()

    app = rasa_x_server.configure_app(local_mode=True)

    with session_scope() as session:
        auth_endpoints = _enable_development_mode_and_get_additional_auth_endpoints(app)
        initialize_app(app, class_views=auth_endpoints)

        sql_migrations.run_migrations(session)

        password, is_password_generated = initialise.create_community_user(session)
        common_utils.run_operation_in_single_sanic_worker(
            app, initialise.AppStartedCallable(password, is_password_generated)
        )

        _initialize_with_local_data(
            project_path, data_path, session, args.port, config_path
        )

        telemetry.track(telemetry.LOCAL_START_EVENT)
        telemetry.track_project_status(session)

    # this needs to run after initial database structures are created
    # otherwise projects assigned to events won't be present
    _start_event_service()

    scheduler.start_background_scheduler()

    # Run models discovery
    common_utils.run_in_loop(model_service.discover_models())

    app.run(
        host="0.0.0.0",
        port=rasa_x_config.self_port,
        auto_reload=os.environ.get("SANIC_AUTO_RELOAD"),
        access_log=False,
        workers=NUMBER_OF_SANIC_WORKERS,
    )
