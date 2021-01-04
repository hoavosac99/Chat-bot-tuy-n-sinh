import logging
import os

import typing

from rasax.community.services.integrated_version_control import git_service
from sqlalchemy.orm import Session

from rasax.community import initialise, global_state  # pytype: disable=import-error
import rasax.community.jwt
import rasax.community.sql_migrations as sql_migrations
import rasax.community.constants as constants
import rasax.community.utils.cli as cli_utils
import rasax.community.utils.common as common_utils
from rasax.community import config, telemetry, scheduler
from rasax.community.api.app import configure_app, initialize_app
from rasax.community.database import utils as db_utils
from rasax.community.services.settings_service import SettingsService
from rasax.community.services.config_service import ConfigService
from rasax.community.services import model_service

if typing.TYPE_CHECKING:
    from multiprocessing import Process  # type: ignore

logger = logging.getLogger(__name__)
logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)

NUMBER_OF_SANIC_WORKERS = 4


def main():
    common_utils.update_log_level()
    logger.debug("Starting API service.")

    global_state.initialize_global_state(NUMBER_OF_SANIC_WORKERS)

    app = configure_app(local_mode=False)
    rasax.community.jwt.initialise_jwt_keys()
    initialize_app(app)

    # Run initialization processes in background after the db migrations is done
    common_utils.run_in_process(_initialize_server_mode, daemon=False)

    cli_utils.print_success("Starting Rasa X server... 🚀")
    app.run(
        host="0.0.0.0",
        port=config.self_port,
        auto_reload=os.environ.get("SANIC_AUTO_RELOAD"),
        workers=NUMBER_OF_SANIC_WORKERS,
    )


def _initialize_server_mode() -> None:
    """Run additional processes after the database migration is done."""
    common_utils.update_log_level()

    # Wait for the DB migration that is running as a separate service
    if config.should_run_database_migration_separately:
        logger.debug(
            f"Environment variable '{constants.DATABASE_MIGRATION_SEPARATION_ENV}' "
            f"set to 'True', meaning Rasa X expects the database migration "
            f"to run as a separate service."
        )

        common_utils.run_in_loop(db_utils.wait_for_migrations(quiet=True))

    with db_utils.session_scope() as session:
        # Start the database migration within the process if
        # `DATABASE_MIGRATION_SEPARATION_ENV` is `False`.
        if not config.should_run_database_migration_separately:
            # Change logging level for alembic.runtime.migration
            logging.getLogger("alembic.runtime.migration").setLevel(logging.INFO)

            try:
                sql_migrations.run_migrations(session)
            except Exception as e:
                import signal

                logger.exception(e)
                logger.error("Cannot run the database migration, terminating...")
                os.killpg(os.getpgid(os.getppid()), signal.SIGTERM)

            logger.debug("The databases migration is done.")

        if common_utils.is_enterprise_installed():
            import rasax.enterprise.initialise as enterprise_initialise  # pytype: disable=import-error

            enterprise_initialise.create_initial_enterprise_user(session)
        else:
            password, is_password_generated = initialise.create_community_user(session)
            initialise.AppStartedCallable(password, is_password_generated)()

        # fork telemetry loop and background scheduler
        process = initialise_server_mode(session)

    launch_event_service()
    telemetry.track_server_start()

    # Run models discovery
    common_utils.run_in_loop(model_service.discover_models())

    # We need to keep the process where _initialize_server_mode was called alive
    # to avoid creating multiple orphan processes. It's not strictly necessary
    # but it is a good practice. If the scheduler is being killed, then it means
    # the entire Rasa X server is probably being restarted anyways.
    process.join()


def initialise_server_mode(session: Session) -> "Process":
    """Initialise common configuration for the server mode.

    Args:
        session: An established database session.

    Returns:
        The process which runs the background scheduler.
    """
    # Initialize environments before they are used in the model discovery process
    settings_service = SettingsService(session)
    settings_service.inject_environments_config_from_file(
        config.project_name, config.default_environments_config_path
    )

    # Initialize database with default configuration values so that they
    # can be read later.
    config_service = ConfigService(session)
    config_service.initialize_configuration()

    # Initialize telemetry
    telemetry.initialize_from_db(session)

    # Start background scheduler in separate process
    return scheduler.start_background_scheduler()


def _event_service() -> None:
    from rasax.community.services.event_service import main as event_service_main

    event_service_main(should_run_liveness_endpoint=False)


def launch_event_service() -> None:
    """Start the event service in a multiprocessing.Process if
    `EVENT_CONSUMER_SEPARATION_ENV` is `True`, otherwise do nothing."""

    if config.should_run_event_consumer_separately:
        logger.debug(
            f"Environment variable '{constants.EVENT_CONSUMER_SEPARATION_ENV}' "
            f"set to 'True', meaning Rasa X expects the event consumer "
            f"to run as a separate service."
        )
    else:
        logger.debug("Starting event service from Rasa X.")

        common_utils.run_in_process(fn=_event_service, daemon=True)


if __name__ == "__main__":
    main()
