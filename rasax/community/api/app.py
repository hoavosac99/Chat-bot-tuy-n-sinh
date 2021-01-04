import logging
from http import HTTPStatus
from typing import Tuple, Dict, Text, Any, List, Optional, Union

import jwt
from sanic import Sanic
from sanic.request import Request
from sanic.response import HTTPResponse
from sanic.handlers import ErrorHandler
from sanic.exceptions import SanicException
from sanic_cors import CORS
from sanic_jwt import Initialize, Responses
from sanic_jwt import exceptions


import rasax.community.constants as constants
import rasax.community.config as rasa_x_config
import rasax.community.utils.common as common_utils
from rasax.community.api.blueprints import (
    stack,
    nlg,
    models,
    intents,
    project,
    interface,
    telemetry,
    websocket,
    logs,
    evaluations,
)
from rasax.community.api.blueprints.conversations import tags, slots, channels
from rasax.community.api.blueprints.nlu import (
    training_examples,
    regexes,
    lookup_tables,
    synonyms,
    entities,
)
from rasax.community.api.blueprints.core import stories, rules

from rasax.community.constants import (
    API_URL_PREFIX,
    REQUEST_DB_SESSION_KEY,
    USERNAME_KEY,
)
import rasax.community.database.utils as db_utils
import rasax.community.services.db_migration_service as db_migration_service
from rasax.community.services.role_service import normalise_permissions
from rasax.community.services.user_service import UserService, has_role, GUEST

logger = logging.getLogger(__name__)


class ExtendedResponses(Responses):
    @staticmethod
    def extend_verify(request, user=None, payload=None):
        return {
            constants.USERNAME_KEY: jwt.decode(request.token, verify=False)[
                constants.USERNAME_KEY
            ]
        }


class RasaXErrorHandler(ErrorHandler):
    """Sanic error handler for the Rasa X API.
    """

    def default(
        self, request: Request, exception: Union[Exception, SanicException]
    ) -> HTTPResponse:
        """Handle unexpected errors when processing an HTTP request.

        Args:
            request: The current HTTP request being processed.
            exception: The unhandled exception that was raised during processing.

        Returns:
            HTTP response with status code 500.
        """
        # Call `ErrorHandler.default()` to log the exception.
        super().default(request, exception)

        # Return a custom JSON that looks familiar to API users.
        return common_utils.error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "The server encountered an internal error and cannot complete the request.",
            message="See the server logs for more information.",
        )


async def init_args_access_set(request: Request) -> None:
    """Initialize the set where information about accessed query string
    arguments is stored.

    Args:
        request: Incoming HTTP request.
    """
    request.ctx.accessed_args = set()


async def process_accessed_args(request: Request, response: HTTPResponse) -> None:
    """Check which query string arguments have been accessed at some point by
    the endpoint handler and which haven't. Log all arguments that have not been
    accessed at least once.

    Args:
        request: Incoming HTTP request.
        response: Outgoing HTTP response.
    """
    if not hasattr(request.ctx, "accessed_args"):
        return

    if not str(response.status).startswith("2"):
        # If we're not returning 2XX, then maybe an exception was raised before
        # args could be accessed.
        return

    for key in request.args:
        if key in request.ctx.accessed_args:
            continue

        logger.debug(
            "%s %s: Query string argument '%s' was not used (value: '%s').",
            request.method,
            request.path,
            key,
            request.args.get(key),
        )


async def authenticate(request, *args, **kwargs):
    """Set up JWT auth."""

    user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])
    rjs = request.json

    # enterprise SSO single-use-token login
    if rjs and rjs.get("single_use_token") is not None:
        user = user_service.single_use_token_login(
            rjs["single_use_token"], return_api_token=True
        )
        if user:
            return user
        else:
            raise exceptions.AuthenticationFailed("Wrong authentication token.")

    if not rjs:
        raise exceptions.AuthenticationFailed("Missing username or password.")

    # standard auth with username and password in request
    username = rjs.get(constants.USERNAME_KEY, None)
    password = rjs.get("password", None)

    if username and password:
        return user_service.login(username, password, return_api_token=True)

    raise exceptions.AuthenticationFailed("Missing username or password.")


def remove_unused_payload_keys(user_dict: Dict[Text, Any]):
    """Removes unused keys from `user` dictionary in JWT payload.

    Removes keys `permissions`, `authentication_mechanism`, `projects` and  `team`."""

    for key in ["permissions", "authentication_mechanism", "projects", "team"]:
        del user_dict[key]


async def scope_extender(user: Dict[Text, Any], *args, **kwargs) -> List[Text]:
    permissions = user["permissions"]
    remove_unused_payload_keys(user)
    return normalise_permissions(permissions)


async def payload_extender(payload, user):
    payload.update({"user": user})
    return payload


async def retrieve_user(
    request: Request,
    payload: Dict,
    allow_api_token: bool,
    extract_user_from_jwt: bool,
    *args: Any,
    **kwargs: Any,
) -> Optional[Dict]:
    if extract_user_from_jwt and payload and has_role(payload.get("user"), GUEST):
        return payload["user"]

    user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])

    if allow_api_token:
        api_token = common_utils.default_arg(request, "api_token")
        if api_token:
            return user_service.api_token_auth(api_token)

    if payload:
        username = payload.get(constants.USERNAME_KEY)
        saml_id = payload.get("user", {}).get("saml_id")
        if username is not None:
            return user_service.fetch_user(username)
        elif saml_id:
            # user is first-time enterprise user and has username None
            # in this case we'll fetch the profile using the `saml_id`
            return user_service.fetch_user(saml_id)

    return None


def initialize_app(app: Sanic, class_views: Tuple = ()) -> None:
    Initialize(
        app,
        authenticate=authenticate,
        add_scopes_to_payload=scope_extender,
        extend_payload=payload_extender,
        class_views=class_views,
        responses_class=ExtendedResponses,
        retrieve_user=retrieve_user,
        algorithm=constants.JWT_METHOD,
        private_key=rasa_x_config.jwt_private_key,
        public_key=rasa_x_config.jwt_public_key,
        url_prefix="/api/auth",
        user_id=constants.USERNAME_KEY,
    )


def configure_app(local_mode: Optional[bool] = None) -> Sanic:
    """Create the Sanic app with the endpoint blueprints.

    Args:
        local_mode: `True` if Rasa X is running in local mode, `False` for server mode.

    Returns:
        Sanic app including the available blueprints.
    """

    local_mode = local_mode if local_mode is not None else rasa_x_config.LOCAL_MODE
    # sanic-cors shows a DEBUG message for every request which we want to
    # suppress
    logging.getLogger("sanic_cors").setLevel(logging.INFO)
    logging.getLogger("spf.framework").setLevel(logging.INFO)

    app = Sanic(__name__, configure_logging=rasa_x_config.debug_mode)

    # Install a custom error handler
    # Note: this needs to happen before CORS is initialised, as `app.error_handler`
    # will appear as the `orig_handler` property of the `CORSErrorHandler`
    app.error_handler = RasaXErrorHandler()

    # allow CORS and OPTIONS on every endpoint
    CORS(
        app,
        expose_headers=["X-Total-Count"],
        automatic_options=True,
        max_age=rasa_x_config.SANIC_ACCESS_CONTROL_MAX_AGE,
    )

    # Configure Sanic response timeout
    app.config.RESPONSE_TIMEOUT = rasa_x_config.SANIC_RESPONSE_TIMEOUT_IN_SECONDS

    # set max request size (for large model uploads)
    app.config.REQUEST_MAX_SIZE = rasa_x_config.SANIC_REQUEST_MAX_SIZE_IN_BYTES

    # set JWT expiration time
    app.config.SANIC_JWT_EXPIRATION_DELTA = rasa_x_config.jwt_expiration_time

    app.register_middleware(init_args_access_set, "request")
    app.register_middleware(process_accessed_args, "response")

    # Set up Blueprints
    app.blueprint(interface.blueprint())
    app.blueprint(project.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(stack.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(tags.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(models.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(nlg.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(intents.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(telemetry.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(slots.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(channels.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(logs.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(evaluations.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(training_examples.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(synonyms.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(regexes.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(entities.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(lookup_tables.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(
        websocket.blueprint(), url_prefix=constants.API_URL_PREFIX,
    )
    app.blueprint(stories.blueprint(), url_prefix=constants.API_URL_PREFIX)
    app.blueprint(rules.blueprint(), url_prefix=constants.API_URL_PREFIX)

    if not local_mode and common_utils.is_git_available():
        from rasax.community.api.blueprints import git

        app.blueprint(git.blueprint(), url_prefix=constants.API_URL_PREFIX)

    # Setup DB
    app.add_task(db_utils.setup_db(app))

    return app
