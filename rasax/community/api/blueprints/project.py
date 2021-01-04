import logging
import shutil
from typing import Any, Dict, Text

from sanic import Blueprint, response
from sanic.response import HTTPResponse
from sanic.request import Request

import rasax.community.config as rasa_x_config
import rasax.community.constants as constants
import rasax.community.jwt
import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
import rasax.community.utils.yaml as yaml_utils
import rasax.community.version
from rasa.shared.core.domain import InvalidDomain
from rasax.community.api.decorators import (
    inject_rasa_x_user,
    rasa_x_scoped,
    validate_schema,
)
from rasax.community.services import config_service
import rasax.community.services.db_migration_service as db_migration_service
from rasax.community.services.domain_service import DomainService
from rasax.community.services.feature_service import FeatureService
from rasax.community.services.settings_service import SettingsService, ProjectException
from rasax.community.services.stack_service import StackService, RASA_VERSION_KEY
from rasax.community.services.user_service import (
    UserException,
    UserService,
    MismatchedPasswordsException,
)

logger = logging.getLogger(__name__)


async def collect_stack_results(
    stack_services: Dict[Text, StackService]
) -> Dict[Text, Any]:
    """Creates status result dictionary for stack services."""
    from rasax.community.services import stack_service

    stack_result = dict()

    version_responses = await stack_service.collect_version_calls(stack_services)
    for name, _status in version_responses.items():
        if isinstance(_status, dict) and RASA_VERSION_KEY in _status:
            _result = _status.copy()
            _result["status"] = 200
        else:
            _result = {"status": 500, "message": _status}
        stack_result[name] = _result

    return stack_result


def _rasa_services(request: Request) -> Dict[Text, StackService]:
    settings_service = SettingsService(request[constants.REQUEST_DB_SESSION_KEY])
    return settings_service.stack_services()


def _domain_service(request: Request) -> DomainService:
    return DomainService(request[constants.REQUEST_DB_SESSION_KEY])


def _user_service(request: Request) -> UserService:
    return UserService(request[constants.REQUEST_DB_SESSION_KEY])


def blueprint() -> Blueprint:
    project_endpoints = Blueprint("project_endpoints")

    @project_endpoints.route("/health", methods=["GET", "HEAD"])
    async def health(request: Request) -> HTTPResponse:
        stack_result = {}

        if hasattr(request.app, "session_maker"):
            stack_services = _rasa_services(request)
            stack_result = await collect_stack_results(stack_services)

        stack_result[
            "database_migration"
        ] = await db_migration_service.migration_status()

        return response.json(stack_result)

    @project_endpoints.route("/version", methods=["GET", "HEAD"])
    async def version(request: Request) -> HTTPResponse:
        rasa_services = _rasa_services(request)

        rasa_versions = {
            environment: await rasa_service.rasa_version()
            for environment, rasa_service in rasa_services.items()
        }
        result = {"rasa": rasa_versions, "rasa-x": rasax.community.__version__}

        update_version = await common_utils.check_for_updates()
        if update_version:
            result["updates"] = {
                "rasa-x": {
                    "version": update_version,
                    "changelog_url": constants.RASA_X_CHANGELOG,
                }
            }

        rasax.community.jwt.add_jwt_key_to_result(result)
        return response.json(result)

    @project_endpoints.route("/user", methods=["GET", "HEAD"])
    @rasa_x_scoped("user.get")
    @inject_rasa_x_user()
    async def profile(request: Request, user: Dict) -> HTTPResponse:
        user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])
        return response.json(
            user_service.fetch_user(user[constants.USERNAME_KEY], return_api_token=True)
        )

    @project_endpoints.route("/user", methods=["PATCH"])
    @rasa_x_scoped("user.update")
    @inject_rasa_x_user()
    @validate_schema("username")
    async def update_username(request: Request, user: Dict) -> HTTPResponse:
        rjs = request.json

        try:
            user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])
            user_profile = user_service.update_saml_username(
                user["saml_id"], rjs[constants.USERNAME_KEY]
            )

        except UserException as e:
            return common_utils.error(
                404,
                "UserException",
                "Could not assign username {} to name_id {}"
                "".format(rjs[constants.USERNAME_KEY], user["name_id"]),
                details=e,
            )

        return response.json(user_profile)

    @project_endpoints.route("/users", methods=["GET", "HEAD"])
    @rasa_x_scoped("users.list")
    async def list_users(request: Request) -> HTTPResponse:
        user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])
        username_query = common_utils.default_arg(request, constants.USERNAME_KEY, None)
        role_query = common_utils.default_arg(request, "role", None)
        users = user_service.fetch_all_users(
            rasa_x_config.team_name, username_query, role_query
        )
        if not users:
            return common_utils.error(404, "NoUsersFound", "No users found")

        profiles = [user_service.fetch_user(u[constants.USERNAME_KEY]) for u in users]

        return response.json(profiles, headers={"X-Total-Count": len(profiles)})

    @project_endpoints.route("/users/<username:string>", methods=["PUT"])
    @rasa_x_scoped("user.values.update")
    @inject_rasa_x_user()
    @validate_schema("user_update")
    async def update_user(
        request: Request, username: Text, user: Dict[Text, Any]
    ) -> HTTPResponse:
        """Update properties of a `User`."""

        if username != user[constants.USERNAME_KEY]:
            return common_utils.error(
                403, "UserUpdateError", "Users can only update their own propeties."
            )

        try:
            _user_service(request).update_user(
                user[constants.USERNAME_KEY], request.json
            )
            return response.text("", 204)
        except UserException as e:
            return common_utils.error(404, "UserUpdateError", details=e)

    @project_endpoints.route("/users/<username>", methods=["DELETE"])
    @rasa_x_scoped("users.delete")
    @inject_rasa_x_user()
    async def delete_user(request: Request, username: Text, user: Dict) -> HTTPResponse:
        user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])

        try:
            deleted = user_service.delete_user(
                username, requesting_user=user[constants.USERNAME_KEY]
            )
            return response.json(deleted)
        except UserException as e:
            return common_utils.error(404, "UserDeletionError", str(e))

    @project_endpoints.route("/user/password", methods=["POST"])
    @rasa_x_scoped("user.password.update")
    @validate_schema("change_password")
    async def change_password(request: Request) -> HTTPResponse:
        rjs = request.json
        user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])

        try:
            user = user_service.change_password(rjs)
            if user is None:
                return common_utils.error(404, "UserNotFound", "user not found")
            return response.json(user)
        except MismatchedPasswordsException:
            return common_utils.error(403, "WrongPassword", "wrong password")

    @project_endpoints.route("/projects/<project_id>", methods=["POST"])
    @rasa_x_scoped("projects.create")
    @inject_rasa_x_user()
    async def create_project(
        request: Request, project_id: Text, user: Dict
    ) -> HTTPResponse:
        settings_service = SettingsService(request[constants.REQUEST_DB_SESSION_KEY])

        try:
            project = settings_service.init_project(user["team"], project_id)
        except ProjectException as e:
            return common_utils.error(404, "ProjectCreationError", details=e)

        user_service = UserService(request[constants.REQUEST_DB_SESSION_KEY])
        user_service.assign_project_to_user(user, project_id)

        return response.json(project)

    # no authentication because features may be needed
    # before a user is authenticated
    @project_endpoints.route("/features", methods=["GET", "HEAD"])
    async def features(request: Request) -> HTTPResponse:
        feature_service = FeatureService(request[constants.REQUEST_DB_SESSION_KEY])
        return response.json(feature_service.features())

    @project_endpoints.route("/features", methods=["POST"])
    @rasa_x_scoped("features.update", allow_api_token=True)
    @validate_schema("feature")
    async def set_feature(request: Request) -> HTTPResponse:
        rjs = request.json
        feature_service = FeatureService(request[constants.REQUEST_DB_SESSION_KEY])
        feature_service.set_feature(rjs)
        return response.json(rjs)

    @project_endpoints.route("/logs")
    @rasa_x_scoped("logs.list", allow_api_token=True)
    async def logs(_request: Request) -> HTTPResponse:
        shutil.make_archive("/tmp/logs", "zip", "/logs")
        return await response.file("/tmp/logs.zip")

    @project_endpoints.route("/environments", methods=["GET", "HEAD"])
    @rasa_x_scoped("environments.list", allow_api_token=True)
    async def get_environment_config(request: Request) -> HTTPResponse:
        settings_service = SettingsService(request[constants.REQUEST_DB_SESSION_KEY])
        environments = settings_service.get_environments_config(
            rasa_x_config.project_name
        )

        if not environments:
            return common_utils.error(
                400,
                "EnvironmentSettingsNotFound",
                "could not find environment settings",
            )

        return response.json(
            {"environments": yaml_utils.dump_yaml(environments.get("environments"))}
        )

    @project_endpoints.route("/chatToken", methods=["GET", "HEAD"])
    @rasa_x_scoped("chatToken.get", allow_rasa_x_token=True)
    async def get_chat_token(request: Request) -> HTTPResponse:
        domain_service = _domain_service(request)
        return response.json(domain_service.get_token())

    @project_endpoints.route("/chatToken", methods=["PUT"])
    @rasa_x_scoped("chatToken.update", allow_api_token=True)
    @validate_schema("update_token")
    async def update_chat_token(request: Request) -> HTTPResponse:
        domain_service = _domain_service(request)
        domain_service.update_token_from_dict(request.json)
        return response.json(domain_service.get_token())

    @project_endpoints.route("/domain", methods=["GET", "HEAD"])
    @rasa_x_scoped("domain.get", allow_api_token=True)
    async def get_domain(request: Request) -> HTTPResponse:
        domain_service = DomainService(request[constants.REQUEST_DB_SESSION_KEY])
        domain = domain_service.get_domain(rasa_x_config.project_name)
        if domain is None:
            return common_utils.error(400, "DomainNotFound", "Could not find domain.")

        domain_service.remove_domain_edited_states(domain)
        return response.text(domain_service.dump_cleaned_domain_yaml(domain))

    @project_endpoints.route("/projects/<project_id>/actions", methods=["GET", "HEAD"])
    @rasa_x_scoped("actions.get")
    async def get_domain_actions(request: Request, project_id: Text) -> HTTPResponse:
        domain_actions = DomainService(
            request[constants.REQUEST_DB_SESSION_KEY]
        ).get_actions_from_domain(project_id)

        if domain_actions is None:
            return common_utils.error(400, "DomainNotFound", "Could not find domain.")

        # convert to list for json serialisation
        domain_actions = list(domain_actions)

        return response.json(
            domain_actions, headers={"X-Total-Count": len(domain_actions)}
        )

    @project_endpoints.route("/projects/<project_id>/actions", methods=["POST"])
    @rasa_x_scoped("actions.create")
    @validate_schema("action")
    @inject_rasa_x_user()
    async def create_new_action(
        request: Request, project_id: Text, user: Dict[Text, Any]
    ) -> HTTPResponse:
        domain_service = DomainService(request[constants.REQUEST_DB_SESSION_KEY])
        try:
            created = domain_service.add_new_action(
                request.json, project_id, user[constants.USERNAME_KEY]
            )
            return response.json(created, status=201)
        except ValueError as e:
            return common_utils.error(
                400, "ActionCreationError", "Action already exists.", details=e
            )

    @project_endpoints.route(
        "/projects/<project_id>/actions/<action_id:int>", methods=["PUT"]
    )
    @rasa_x_scoped("actions.update")
    @validate_schema("action")
    async def update_action(
        request: Request, action_id, project_id: Text
    ) -> HTTPResponse:
        domain_service = DomainService(request[constants.REQUEST_DB_SESSION_KEY])
        try:
            updated = domain_service.update_action(action_id, request.json)
            return response.json(updated)
        except ValueError as e:
            return common_utils.error(
                404,
                "ActionNotFound",
                f"Action with id '{action_id}' was not found.",
                details=e,
            )

    @project_endpoints.route(
        "/projects/<project_id>/actions/<action_id:int>", methods=["DELETE"]
    )
    @rasa_x_scoped("actions.delete")
    async def delete_action(
        request: Request, action_id: int, project_id: Text
    ) -> HTTPResponse:
        domain_service = DomainService(request[constants.REQUEST_DB_SESSION_KEY])
        try:
            domain_service.delete_action(action_id)
            return response.text("", 204)
        except ValueError as e:
            return common_utils.error(
                404,
                "ActionNotFound",
                f"Action with id '{action_id}' was not found.",
                details=e,
            )

    @project_endpoints.route("/domain", methods=["PUT"])
    @rasa_x_scoped("domain.update", allow_api_token=True)
    @inject_rasa_x_user()
    async def update_domain(request: Request, user: Dict) -> HTTPResponse:
        common_utils.handle_deprecated_request_parameters(
            request, "store_templates", "store_responses"
        )
        store_responses = common_utils.bool_arg(request, "store_responses", False)
        domain_yaml = io_utils.convert_bytes_to_string(request.body)
        try:
            updated_domain = DomainService(
                request[constants.REQUEST_DB_SESSION_KEY]
            ).validate_and_store_domain_yaml(
                domain_yaml,
                project_id=rasa_x_config.project_name,
                username=user[constants.USERNAME_KEY],
                store_responses=store_responses,
            )
        except InvalidDomain as e:
            return common_utils.error(
                400, "InvalidDomainError", "Could not update domain.", e
            )

        return response.text(updated_domain)

    @project_endpoints.route("/domainWarnings", methods=["GET", "HEAD"])
    @rasa_x_scoped("domainWarnings.get")
    async def get_domain_warnings(request: Request) -> HTTPResponse:
        domain_service = DomainService(request[constants.REQUEST_DB_SESSION_KEY])
        domain_warnings = await domain_service.get_domain_warnings()
        if domain_warnings is None:
            return common_utils.error(400, "DomainNotFound", "Could not find domain.")

        return response.json(
            domain_warnings[0], headers={"X-Total-Count": domain_warnings[1]}
        )

    @project_endpoints.route("/config", methods=["GET", "HEAD"])
    @rasa_x_scoped("config.get", allow_rasa_x_token=True)
    async def get_runtime_config(_: Request) -> HTTPResponse:
        config_dict, errors = config_service.get_runtime_config_and_errors(
            rasa_x_config.credentials_path, rasa_x_config.endpoints_path
        )

        if errors:
            return common_utils.error(
                400,
                "FileNotFoundError",
                common_utils.add_plural_suffix(
                    "Could not find runtime config file{}.", errors
                ),
                details=errors,
            )

        return response.json(config_dict)

    return project_endpoints
