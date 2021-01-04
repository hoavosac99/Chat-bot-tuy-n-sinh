from typing import NamedTuple, Text, List
import multiprocessing as mp


class GlobalState(NamedTuple):
    """All state which is shared by multiple processes.

    Note that we can exclude state which is shared only by Sanic workers as they are
    always created using `fork`. This means that Sanic workers will always obtain
    a copy of the initialized state of the parent process.
    """

    telemetry_queue: mp.Queue  # type: ignore
    telemetry_write_key: Text  # type: ignore

    jobs_queue: mp.Queue  # type: ignore

    git_global_operation_lock: mp.Lock  # type: ignore
    is_target_branch_ahead: mp.Value  # type: ignore

    is_local_mode: bool
    project_directory: mp.Array  # type: ignore

    were_models_discovered: mp.Value  # type: ignore

    websocket_queues: List[mp.Queue]  # type: ignore

    timestamp_of_oldest_pending_change: mp.Value  # type: ignore
    timestamp_of_latest_pending_change: mp.Value  # type: ignore


def initialize_global_state(
    number_of_sanic_workers: int, is_local_mode: bool = False
) -> None:
    """Initializes all global state which is shared by processes within Rasa X.

    Args:
        number_of_sanic_workers: How many Sanic workers are used to run the Rasa X
            server.
        is_local_mode: `True` if Rasa X is run in local mode.
    """
    from rasax.community import (
        scheduler,
        config as rasa_x_config,
    )
    import rasax.community.utils.common as common_utils
    from rasax.community.services.integrated_version_control import git_service
    from rasax.community.api.blueprints import websocket
    from rasax.community.services import background_dump_service, model_service
    from rasax.community import telemetry

    rasa_x_config.LOCAL_MODE = is_local_mode

    mp_context = common_utils.mp_context()

    websocket.initialize_global_sanic_worker_states(number_of_sanic_workers, mp_context)

    git_service.initialize_global_state(mp_context)
    scheduler.initialize_global_state(mp_context)
    background_dump_service.initialize_global_state(mp_context)
    model_service.initialize_global_state(mp_context)
    telemetry.initialize_global_state(mp_context)
    rasa_x_config.initialize_global_state(mp_context)


def get_global_state() -> GlobalState:
    """Get the current global state (e.g. to pass it to a new process).

    Returns:
        The current global state.
    """
    from rasax.community import config as rasa_x_config
    from rasax.community import telemetry, scheduler
    from rasax.community.services.integrated_version_control import git_service
    from rasax.community.services import (  # pytype: disable=pyi-error
        model_service,
        websocket_service,
        background_dump_service,
    )

    telemetry_queue = telemetry.get_events_queue()
    jobs_queue = scheduler.get_jobs_queue()
    (
        git_global_operation_lock,
        is_target_branch_ahead,
    ) = git_service.get_git_global_variables()
    oldest_pending, latest_pending = background_dump_service.get_global_state()

    return GlobalState(
        telemetry_queue=telemetry_queue,
        telemetry_write_key=rasa_x_config.telemetry_write_key,
        jobs_queue=jobs_queue,
        git_global_operation_lock=git_global_operation_lock,
        is_target_branch_ahead=is_target_branch_ahead,
        is_local_mode=rasa_x_config.LOCAL_MODE,
        project_directory=rasa_x_config.PROJECT_DIRECTORY,
        were_models_discovered=model_service.were_models_discovered,
        websocket_queues=websocket_service.get_message_queues(),
        timestamp_of_oldest_pending_change=oldest_pending,
        timestamp_of_latest_pending_change=latest_pending,
    )


def set_global_state(state: GlobalState) -> None:
    """Set the global state within this process.

    Args:
        state: The global state which should be applied.
    """
    from rasax.community import scheduler, config as rasa_x_config
    from rasax.community.services.integrated_version_control import git_service
    from rasax.community.services import (  # pytype: disable=pyi-error
        background_dump_service,
        model_service,
        websocket_service,
    )
    from rasax.community import telemetry

    telemetry.set_events_queue(state.telemetry_queue)
    rasa_x_config.telemetry_write_key = state.telemetry_write_key

    scheduler.set_jobs_queue(state.jobs_queue)

    git_service.set_git_global_variables(
        state.git_global_operation_lock, state.is_target_branch_ahead
    )
    rasa_x_config.LOCAL_MODE = state.is_local_mode
    rasa_x_config.PROJECT_DIRECTORY = state.project_directory

    background_dump_service.set_global_state(
        state.timestamp_of_oldest_pending_change,
        state.timestamp_of_latest_pending_change,
    )

    websocket_service.set_message_queues(state.websocket_queues)

    model_service.were_models_discovered = state.were_models_discovered


def initialize_global_state_for_standalone_event_service() -> None:
    """Initializes the global state when the event service runs in standalone mode."""
    from rasax.community import config as rasa_x_config

    # Override this configuration value, as we know for certain that the event
    # service has been started as a separate service.
    rasa_x_config.should_run_event_consumer_separately = True

    # When the event service is run as a standalone service, it does not have
    # access to the mounted model volume. In order to enable the model service to
    # access the models stored in the database right away, Rasa X is configured
    # is configured to not wait for model discovery to finish.
    rasa_x_config.wait_for_model_discovery = False

    initialize_global_state(number_of_sanic_workers=0)
