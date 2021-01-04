import logging
import os

import time
from typing import Dict, Text

from aiohttp import ClientError
from ruamel.yaml import YAMLError
from sanic import Blueprint, response
from sanic.request import Request
import http

from rasa.nlu.config import InvalidConfigError
import rasax.community.config as rasa_x_config
import rasax.community.constants as constants
import rasax.community.utils.common as common_utils
import rasax.community.utils.config as config_utils
import rasax.community.utils.yaml as yaml_utils
import rasax.community.telemetry as telemetry
from rasax.community.api.decorators import rasa_x_scoped, inject_rasa_x_user
from rasax.community.services.model_service import ModelService
from rasax.community.services.nlg_service import NlgService
from rasax.community.services.settings_service import SettingsService
import rasax.community.services.websocket_service as websocket_service

logger = logging.getLogger(__name__)


def _model_service(request: Request) -> ModelService:
    session = request[constants.REQUEST_DB_SESSION_KEY]
    return ModelService(
        rasa_x_config.rasa_model_dir, session, constants.DEFAULT_RASA_ENVIRONMENT
    )


def blueprint() -> Blueprint:
    endpoints = Blueprint("model_endpoints")

    @endpoints.route("/projects/<project_id>/models", methods=["GET", "HEAD"])
    @rasa_x_scoped("models.list", allow_api_token=True, allow_rasa_x_token=True)
    async def get_models(request: Request, project_id: Text):
        limit = common_utils.int_arg(request, "limit")
        offset = common_utils.int_arg(request, "offset", 0)
        tag = common_utils.default_arg(request, "tag")

        models, total_models = await _model_service(request).get_models(
            project_id, limit, offset, tag
        )

        return response.json(models, headers={"X-Total-Count": total_models})

    @endpoints.route("/projects/<project_id>/models", methods=["POST"])
    @rasa_x_scoped("models.create", allow_api_token=True, allow_rasa_x_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    async def upload_model(
        request: Request, project_id: Text, user: Dict
    ) -> response.HTTPResponse:
        model_service = _model_service(request)
        try:
            tpath = model_service.save_model_to_disk(request)
        except (FileNotFoundError, ValueError, TypeError) as e:
            return common_utils.error(
                http.HTTPStatus.BAD_REQUEST,
                "ModelSaveError",
                f"Could not save model.\n{e}",
            )

        minimum_version = await model_service.minimum_compatible_version()

        if not model_service.is_model_compatible(minimum_version, fpath=tpath):
            return common_utils.error(
                http.HTTPStatus.BAD_REQUEST,
                "ModelVersionError",
                f"Model version unsupported.\n"
                f"The minimum compatible version is {minimum_version}.",
            )

        try:
            filename: Text = os.path.basename(tpath)
            model_name = filename.split(".tar.gz")[0]
            saved = await model_service.save_uploaded_model(
                project_id, model_name, tpath
            )
            if saved:
                websocket_service.send_message(
                    websocket_service.Message(
                        websocket_service.MessageTopic.MODELS,
                        name="model_added",
                        data={"method": "uploaded", "model_name": model_name},
                        recipient=user[constants.USERNAME_KEY],
                    )
                )
                telemetry.track(telemetry.MODEL_UPLOADED_EVENT)

                return response.json(saved, http.HTTPStatus.CREATED)
            return common_utils.error(
                http.HTTPStatus.BAD_REQUEST,
                "ModelSaveError",
                "Could not save model.\nModel name '{}'."
                "File path '{}'.".format(model_name, tpath),
            )
        except FileExistsError:
            return common_utils.error(
                http.HTTPStatus.CONFLICT,
                "ModelExistsError",
                "A model with that name already exists.",
            )

    @endpoints.route(
        "/projects/<project_id>/models/tags/<tag>", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped(
        "models.modelByTag.get", allow_api_token=True, allow_rasa_x_token=True
    )
    async def get_model_for_tag(
        request: Request, project_id: Text, tag: Text
    ) -> response.HTTPResponse:
        model = _model_service(request).model_for_tag(project_id, tag)
        if not model:
            return common_utils.error(
                404, "TagNotFound", f"Tag '{tag}' not found for project '{project_id}'."
            )
        model_hash = model["hash"]
        try:
            if model_hash == request.headers.get("If-None-Match"):
                return response.text("", http.HTTPStatus.NO_CONTENT)

            return await response.file_stream(
                location=model["path"],
                headers={
                    "ETag": model_hash,
                    "filename": os.path.basename(model["path"]),
                },
                mime_type="application/gzip",
            )
        except FileNotFoundError:
            logger.warning(
                "Tried to download model file '{}', "
                "but file does not exist.".format(model["path"])
            )
            return common_utils.error(
                404,
                "ModelDownloadFailed",
                "Failed to find model file '{}'.".format(model["path"]),
            )

    @endpoints.route("/projects/<project_id>/models/<model>", methods=["GET", "HEAD"])
    @rasa_x_scoped("models.get", allow_api_token=True, allow_rasa_x_token=True)
    async def get_model_by_name(
        request: Request, project_id: Text, model: Text
    ) -> response.HTTPResponse:
        model = _model_service(request).get_model_by_name(project_id, model)
        if not model:
            return common_utils.error(
                http.HTTPStatus.NOT_FOUND,
                "ModelNotFound",
                f"Model '{model}' not found for project '{project_id}'.",
            )
        model_hash = model["hash"]
        try:
            if model_hash == request.headers.get("If-None-Match"):
                return response.text("", http.HTTPStatus.NO_CONTENT)

            return await response.file_stream(
                location=model["path"],
                headers={
                    "ETag": model_hash,
                    "filename": os.path.basename(model["path"]),
                },
                mime_type="application/gzip",
            )
        except FileNotFoundError as e:
            logger.exception(e)
            return common_utils.error(
                http.HTTPStatus.NOT_FOUND,
                "ModelDownloadFailed",
                f"Failed to download file.\n{e}",
            )

    # noinspection PyUnusedLocal
    @endpoints.route("/projects/<project_id>/models/<model>", methods=["DELETE"])
    @rasa_x_scoped("models.delete", allow_api_token=True)
    async def delete_model(
        request: Request, project_id: Text, model: Text
    ) -> response.HTTPResponse:
        deleted = _model_service(request).delete_model(project_id, model)
        if deleted:
            return response.text("", http.HTTPStatus.NO_CONTENT)

        return common_utils.error(
            http.HTTPStatus.NOT_FOUND,
            "ModelDeleteFailed",
            f"Failed to delete model '{model}'.",
        )

    @endpoints.route("/projects/<project_id>/models/jobs", methods=["POST"])
    @rasa_x_scoped("models.jobs.create", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    async def train_model(
        request: Request, project_id: Text, user: Dict
    ) -> response.HTTPResponse:
        stack_services = SettingsService(
            request[constants.REQUEST_DB_SESSION_KEY]
        ).stack_services(project_id)
        environment = common_utils.deployment_environment_from_request(
            request, constants.RASA_WORKER_ENVIRONMENT
        )
        stack_service = stack_services[environment]

        try:
            training_start = time.time()
            content = await stack_service.start_training_process()
            telemetry.track(telemetry.MODEL_TRAINED_EVENT)

            model_name = await _model_service(request).save_trained_model(
                project_id, content
            )

            websocket_service.send_message(
                websocket_service.Message(
                    websocket_service.MessageTopic.MODELS,
                    name="model_added",
                    data={"method": "trained", "model_name": model_name},
                    recipient=user[constants.USERNAME_KEY],
                )
            )

            nlg_service = NlgService(request[constants.REQUEST_DB_SESSION_KEY])
            nlg_service.mark_responses_as_used(training_start)

            return response.json({"info": "New model trained.", "model": model_name})
        except FileExistsError as e:
            logger.info(f"Nothing changed. You can use the old model stored at '{e}'.")
            return response.json(
                {"info": "Model already exists.", "path": str(e)},
                http.HTTPStatus.CREATED,
            )
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                http.HTTPStatus.BAD_REQUEST,
                "StackTrainingFailed",
                "Encountered an exception while training.",
                details=e,
            )
        except ClientError as e:
            logger.error(
                f"{e}\nEncountered an exception while training. "
                f"Please check the logs of the rasa worker container for more information."
            )
            return common_utils.error(
                http.HTTPStatus.INTERNAL_SERVER_ERROR,
                "StackTrainingFailed",
                "Failed to train a Rasa model.",
                details=e,
            )

    # noinspection PyUnusedLocal
    @endpoints.route(
        "/projects/<project_id>/models/<model>/tags/<tag>", methods=["PUT"]
    )
    @rasa_x_scoped("models.tags.update", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    async def tag_model(
        request: Request, project_id: Text, model: Text, tag: Text, user: Dict,
    ) -> response.HTTPResponse:
        try:
            await _model_service(request).tag_model(project_id, model, tag)
            if tag == constants.RASA_PRODUCTION_ENVIRONMENT:
                websocket_service.send_message(
                    websocket_service.Message(
                        websocket_service.MessageTopic.MODELS,
                        name="model_promoted",
                        data={"model_name": model, "time": time.time()},
                        recipient=user[constants.USERNAME_KEY],
                    )
                )

                telemetry.track(telemetry.MODEL_PROMOTED_EVENT)

            return response.text("", http.HTTPStatus.NO_CONTENT)
        except ValueError as e:
            return common_utils.error(
                http.HTTPStatus.NOT_FOUND,
                "ModelTagError",
                f"Failed to tag model '{model}'.",
                details=e,
            )

    # noinspection PyUnusedLocal
    @endpoints.route(
        "/projects/<project_id>/models/<model>/tags/<tag>", methods=["DELETE"]
    )
    @rasa_x_scoped("models.tags.delete", allow_api_token=True)
    async def untag(request, project_id, model, tag) -> response.HTTPResponse:
        try:
            _model_service(request).delete_tag(project_id, model, tag)
            return response.text("", http.HTTPStatus.NO_CONTENT)
        except ValueError as e:
            return common_utils.error(
                http.HTTPStatus.NOT_FOUND,
                "TagDeletionFailed",
                "Failed to delete model tag",
                details=e,
            )

    @endpoints.route("/projects/<project_id>/settings", methods=["GET", "HEAD"])
    @rasa_x_scoped("models.settings.get", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    async def get_model_config(request, project_id, user=None) -> response.HTTPResponse:
        settings_service = SettingsService(request[constants.REQUEST_DB_SESSION_KEY])
        stack_config = settings_service.get_config(user["team"], project_id)
        if not stack_config:
            return common_utils.error(
                http.HTTPStatus.NOT_FOUND, "SettingsFailed", "Could not find settings."
            )

        yaml_config = yaml_utils.dump_yaml(stack_config)

        return response.text(yaml_config)

    @endpoints.route("/projects/<project_id>/settings", methods=["PUT"])
    @rasa_x_scoped("models.settings.update")
    @inject_rasa_x_user()
    async def save_model_config(
        request, project_id, user=None
    ) -> response.HTTPResponse:
        settings_service = SettingsService(request[constants.REQUEST_DB_SESSION_KEY])
        try:
            config_yaml = settings_service.inspect_and_save_yaml_config_from_request(
                request.body, user["team"], project_id
            )
            return response.text(config_yaml)
        except YAMLError as e:
            return common_utils.error(
                400, "InvalidConfig", f"Failed to read configuration file.  Error: {e}"
            )
        except config_utils.InvalidConfigError as e:
            return common_utils.error(
                http.HTTPStatus.UNPROCESSABLE_ENTITY, "ConfigMissingKeys", f"Error: {e}"
            )

    return endpoints
