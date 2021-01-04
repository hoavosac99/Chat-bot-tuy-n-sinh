from http import HTTPStatus
from typing import Text, Optional
import logging
import time

from sanic import response, Blueprint
from sanic.request import Request
from sanic.response import HTTPResponse

import rasax.community.constants as constants
from rasax.community.api.decorators import validate_schema, rasa_x_scoped
from rasax.community.services.integrated_version_control.git_service import GitService
from rasax.community.services.integrated_version_control.exceptions import (
    GitConcurrentOperationException,
    GitCommitError,
    GitHTTPSCredentialsError,
    CredentialsError,
    ProjectLayoutError,
)
from rasax.community.services.integrated_version_control.ssh_key_provider import (
    GitSSHKeyProvider,
)
import rasax.community.services.websocket_service as websocket_service
import rasax.community.utils.common as common_utils

logger = logging.getLogger(__name__)


def _git(
    request: Request, project_id: Text, repository_id: Optional[int] = None
) -> GitService:
    return GitService(
        request[constants.REQUEST_DB_SESSION_KEY],
        project_id=project_id,
        repository_id=repository_id,
    )


def blueprint() -> Blueprint:
    git_endpoints = Blueprint("git_endpoints")

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("repositories.list")
    async def get_repositories(request: Request, project_id: Text) -> HTTPResponse:
        """List all stored Git repository credentials."""

        git_service = _git(request, project_id)
        repositories = git_service.get_repositories()
        return response.json(repositories, headers={"X-Total-Count": len(repositories)})

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories", methods=["POST"]
    )
    @rasa_x_scoped("repositories.create", allow_api_token=True)
    @validate_schema("git_repository")
    async def add_repository(request: Request, project_id: Text) -> HTTPResponse:
        """Store a new Git repository."""

        git_service = _git(request, project_id)
        try:
            saved = git_service.save_repository(request.json)
            git_service.trigger_immediate_project_synchronization()
            websocket_service.send_message(
                websocket_service.Message(
                    websocket_service.MessageTopic.IVC, "connected"
                )
            )

            return response.json(saved, HTTPStatus.CREATED)
        except common_utils.RasaLicenseException as e:
            return common_utils.error(
                HTTPStatus.CONFLICT, "RasaEnterpriseRequired", details=e
            )
        except ProjectLayoutError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "RepositoryCreationFailed",
                "The repository does not have the expected project layout.",
                details=e,
            )
        except CredentialsError as e:
            logger.error("An error occurred while creating a new repository:")
            logger.error(e)

            return common_utils.error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "RepositoryCreationFailed",
                "Insufficient permissions for remote repository.",
                details=e,
            )

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories/<repository_id:int>",
        methods=["GET", "HEAD"],
    )
    @rasa_x_scoped("repositories.get")
    async def get_repository(
        request: Request, project_id: Text, repository_id: int
    ) -> HTTPResponse:
        """Get information for a specific Git repository."""

        git_service = _git(request, project_id, repository_id)
        try:
            repository = git_service.get_repository()
            return response.json(repository)
        except ValueError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "RepositoryNotFound",
                f"Repository with ID '{repository_id}' could not be found.",
                details=e,
            )

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories/<repository_id:int>",
        methods=["PUT"],
    )
    @rasa_x_scoped("repositories.update")
    @validate_schema("git_repository_update")
    async def update_repository(
        request: Request, project_id: Text, repository_id: int
    ) -> HTTPResponse:
        """Update a specific Git repository."""

        git_service = _git(request, project_id, repository_id)
        try:
            updated = git_service.update_repository(request.json)
            return response.json(updated)
        except common_utils.RasaLicenseException as e:
            return common_utils.error(
                HTTPStatus.CONFLICT, "RasaEnterpriseRequired", details=e
            )
        except GitHTTPSCredentialsError as e:
            return common_utils.error(
                HTTPStatus.FORBIDDEN, "IncorrectCredentials", details=e
            )
        except ProjectLayoutError as e:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "InvalidProjectLayout",
                f"Repository with ID '{repository_id}' "
                f"does not have a valid Rasa project layout.",
                details=e,
            )
        except ValueError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "RepositoryNotFound",
                f"Repository with '{repository_id}' could not be found.",
                details=e,
            )

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories/<repository_id:int>",
        methods=["DELETE"],
    )
    @rasa_x_scoped("repositories.delete")
    async def delete_repository(
        request: Request, project_id: Text, repository_id: int
    ) -> HTTPResponse:
        """Delete a stored Git repository."""

        git_service = _git(request, project_id, repository_id)
        try:
            git_service.delete_repository()
            return response.text("", HTTPStatus.NO_CONTENT)
        except ValueError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "RepositoryNotFound",
                f"Repository with ID '{repository_id}' could not be found.",
                details=e,
            )

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories/<repository_id:int>/status",
        methods=["GET", "HEAD"],
    )
    @rasa_x_scoped("repository_status.get")
    async def get_repository_status(
        request: Request, project_id: Text, repository_id: int
    ) -> HTTPResponse:
        """Gets the status of the repository."""

        git_service = _git(request, project_id, repository_id)
        try:
            repository_status = git_service.get_repository_status()
            return response.json(repository_status, HTTPStatus.OK)
        except ValueError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "RepositoryNotFound",
                f"Repository with ID '{repository_id}' could not be found.",
                details=e,
            )

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories/<repository_id:int>/branches/"
        "<branch_name:path>",
        methods=["PUT"],
    )
    @rasa_x_scoped("branch.update")
    async def checkout_branch(
        request: Request, project_id: Text, repository_id: int, branch_name: Text
    ) -> HTTPResponse:
        """Change the current branch of the repository."""

        git_service = _git(request, project_id, repository_id)
        discard_any_changes = common_utils.bool_arg(request, "force", False)

        try:
            git_service.checkout_branch(
                branch_name, force=discard_any_changes, inject_changes=False
            )

            # wait for project synchronization to finish
            await git_service.synchronize_project(force_data_injection=True)

            return response.text("", HTTPStatus.NO_CONTENT)
        except ProjectLayoutError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "InvalidProjectLayout",
                f"Branch '{branch_name}' for repository with ID '{repository_id}' "
                f"does not have a valid Rasa project layout.",
                details=e,
            )
        except ValueError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "BranchNotFound",
                f"Branch '{branch_name}' for repository with ID '{repository_id}' "
                f"could not be found.",
                details=e,
            )

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories/<repository_id:int>/branches/"
        "<branch_name:path>/commits",
        methods=["POST"],
    )
    @rasa_x_scoped("commit.create")
    async def create_commit(
        request: Request, project_id: Text, repository_id: int, branch_name: Text
    ) -> HTTPResponse:
        """Commit and push the current local changes."""

        git_service = _git(request, project_id, repository_id)

        try:
            commit = await git_service.commit_and_push_changes_to(branch_name)
            websocket_service.send_message(
                websocket_service.Message(
                    websocket_service.MessageTopic.IVC,
                    "changes_pushed",
                    data={
                        "repository_id": repository_id,
                        "branch_name": branch_name,
                        "time": time.time(),
                    },
                )
            )

            return response.json(commit, HTTPStatus.CREATED)
        except ValueError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "RepositoryNotFound",
                f"Branch '{branch_name}' for repository with ID '{repository_id}' "
                f"could not be found.",
            )
        except GitCommitError as e:
            logger.debug(e)
            return common_utils.error(
                HTTPStatus.FORBIDDEN,
                "BranchIsProtected",
                f"Branch '{branch_name}' is protected. Please add your changes to a "
                f"different branch.",
            )
        except GitConcurrentOperationException:
            return common_utils.error(
                HTTPStatus.CONFLICT,
                "AnotherIVCOperationInProgress",
                "There is another Integrated Version Control operation in progress.",
            )

    @git_endpoints.route(
        "/projects/<project_id:string>/git_repositories/public_ssh_key",
        methods=["GET", "HEAD"],
    )
    @rasa_x_scoped("public_ssh_key.get")
    async def get_public_ssh_key(*_, **__) -> HTTPResponse:
        """Return the public ssh key which users can then add to their Git server."""

        public_key = GitSSHKeyProvider.get_public_ssh_key()

        return response.json({"public_ssh_key": public_key})

    return git_endpoints
