import logging
from typing import Text, Dict, Any, List

import time
import uuid

from aiohttp import ClientError
from http import HTTPStatus
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

import rasax.community.jwt
import rasax.community.constants as constants
import rasax.community.tracker_utils as tracker_utils
import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
import rasax.community.config as rasa_x_config
import rasax.community.telemetry as telemetry

from rasa.shared.core import events
from rasa.shared.core.trackers import EventVerbosity
from rasa.shared.core.training_data.structures import Story
from rasax.community.api.decorators import (
    rasa_x_scoped,
    inject_rasa_x_user,
    validate_schema,
)
from rasax.community.services.data_service import DataService
from rasax.community.services.domain_service import DomainService
from rasax.community.services.event_service import EventService
from rasax.community.services.role_service import RoleService
from rasax.community.services.role_service import (
    normalise_permissions,
    guest_permissions,
)
from rasax.community.services.settings_service import SettingsService
from rasax.community.services.stack_service import StackService
from rasax.community.services.story_service import StoryService
from rasax.community.services.test_service import TestService
import rasax.community.services.user_service as user_service
import rasax.community.services.websocket_service as websocket_service

logger = logging.getLogger(__name__)

TAGS_ANY_REQUEST_PARAMETER = "tags_any"
FULL_CONVERSATION_FORMAT_REQUEST_PARAMETER = "full_conversation"


def _event_verbosity_from_request(request):
    if common_utils.bool_arg(request, "history", default=True):
        return EventVerbosity.ALL
    else:
        return EventVerbosity.AFTER_RESTART


def _story_service(request: Request) -> StoryService:
    return StoryService(request[constants.REQUEST_DB_SESSION_KEY])


def _domain_service(request: Request) -> DomainService:
    return DomainService(request[constants.REQUEST_DB_SESSION_KEY])


def _stack_service(request: Request, default_environment: Text) -> StackService:
    settings_service = SettingsService(request[constants.REQUEST_DB_SESSION_KEY])

    environment = common_utils.deployment_environment_from_request(
        request, default_environment
    )
    service = settings_service.get_stack_service(environment)
    if not service:
        raise ValueError(f"Service for requested environment '{environment}' not found")

    return service


def _event_service(request: Request) -> EventService:
    return EventService(request[constants.REQUEST_DB_SESSION_KEY])


def _role_service(request: Request) -> RoleService:
    return RoleService(request[constants.REQUEST_DB_SESSION_KEY])


def blueprint() -> Blueprint:
    stack_endpoints = Blueprint("stack_endpoints")

    @stack_endpoints.route("/conversations", methods=["GET", "HEAD"])
    @rasa_x_scoped("metadata.get", allow_api_token=True)
    @inject_rasa_x_user()
    async def list_clients(request, user=None):
        clients, number_clients = _get_clients(request, user)
        return response.json(clients, headers={"X-Total-Count": number_clients})

    def _get_clients(
        request: Request, user: Dict[Text, Any]
    ) -> common_utils.QueryResult:
        role_service = _role_service(request)
        event_service = _event_service(request)

        sort_by_latest_event_time = common_utils.bool_arg(
            request, "sort_by_latest_event_time", True
        )
        sort_by_confidence = common_utils.bool_arg(request, "sort_by_confidence", True)
        in_training_data = common_utils.default_arg(request, "in_training_data", None)
        if in_training_data is not None:
            in_training_data = in_training_data.lower() == "true"

        review_status = common_utils.enum_arg(
            request,
            "review_status",
            {
                constants.CONVERSATION_STATUS_UNREAD,
                constants.CONVERSATION_STATUS_REVIEWED,
                constants.CONVERSATION_STATUS_SAVED_FOR_LATER,
                None,
            },
            None,
        )
        include_interactive = common_utils.bool_arg(request, "interactive", True)
        start = common_utils.time_arg(request, "start", None)
        until = common_utils.time_arg(request, "until", None)
        minimum_confidence = common_utils.float_arg(request, "minimumConfidence")
        maximum_confidence = common_utils.float_arg(request, "maximumConfidence")
        minimum_nlu_confidence = common_utils.float_arg(request, "minimumNluConfidence")
        maximum_nlu_confidence = common_utils.float_arg(request, "maximumNluConfidence")
        minimum_user_messages = common_utils.int_arg(request, "minimumUserMessages")
        policies = common_utils.default_arg(request, "policies", None)
        intent_query = common_utils.default_arg(request, "intent", None)
        entity_query = common_utils.default_arg(request, "entity", None)
        text_query = common_utils.default_arg(request, "text", None)
        rasa_environment_query = common_utils.default_arg(
            request, "rasa_environment", constants.DEFAULT_RASA_ENVIRONMENT
        )
        action_query = common_utils.default_arg(request, "action", None)
        flag_query = common_utils.bool_arg(request, "is_flagged", False)
        limit = common_utils.int_arg(request, "limit")
        offset = common_utils.int_arg(request, "offset", 0)
        slots = common_utils.list_arg(request, "slots")
        input_channels = common_utils.list_arg(request, "input_channels")

        conversations_tags_filter_any = common_utils.list_arg(
            request, TAGS_ANY_REQUEST_PARAMETER
        )

        exclude = (
            [user[constants.USERNAME_KEY]]
            if common_utils.bool_arg(request, "exclude_self", False)
            else None
        )

        filter_created_by = None
        if not role_service.is_user_allowed_to_view_all_conversations(user):
            filter_created_by = user[constants.USERNAME_KEY]

        return event_service.get_conversation_metadata_for_all_clients(
            start=start,
            until=until,
            minimum_confidence=minimum_confidence,
            maximum_confidence=maximum_confidence,
            minimum_nlu_confidence=minimum_nlu_confidence,
            maximum_nlu_confidence=maximum_nlu_confidence,
            minimum_user_messages=minimum_user_messages,
            policies=policies,
            in_training_data=in_training_data,
            review_status=review_status,
            intent_query=intent_query,
            entity_query=entity_query,
            action_query=action_query,
            sort_by_date=sort_by_latest_event_time,
            sort_by_confidence=sort_by_confidence,
            text_query=text_query,
            rasa_environment_query=rasa_environment_query,
            only_flagged=flag_query,
            include_interactive=include_interactive,
            exclude=exclude,
            limit=limit,
            offset=offset,
            conversations_tags_filter_any=conversations_tags_filter_any,
            slots=slots,
            input_channels=input_channels,
            created_by=filter_created_by,
        )

    @stack_endpoints.route("/conversations", methods=["POST"])
    @rasa_x_scoped("metadata.create", allow_api_token=True)
    @inject_rasa_x_user()
    @validate_schema("conversation", optional=True)
    async def create_conversation(
        request: Request, user: Dict[Text, Any]
    ) -> HTTPResponse:
        payload = request.json or {}
        conversation_id = payload.get("sender_id")
        conversation_id_to_copy_from = payload.get("conversation_id_to_copy_from")
        events_until = payload.get("until") or time.time()

        try:
            event_service = _event_service(request)
            conversation = event_service.create_new_conversation(
                conversation_id,
                interactive=True,
                created_by=user.get(constants.USERNAME_KEY),
            )
            # Commit changes right away to make sure the conversation is not created
            # by Rasa Open Source sending the events to the `EventService`
            event_service.commit()

            stack_service = _stack_service(
                request, constants.RASA_PRODUCTION_ENVIRONMENT
            )

            if conversation_id_to_copy_from:
                _events = event_service.copy_events_from_conversation(
                    conversation_id_to_copy_from, events_until
                )
            else:
                initial_tracker = await stack_service.tracker_json(
                    conversation.sender_id
                )
                _events = initial_tracker["events"]

            tracker = await stack_service.append_events_to_tracker(
                conversation.sender_id, _events
            )

            return response.json(tracker, HTTPStatus.CREATED)
        except ClientError as e:
            return common_utils.error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "ConversationCreationFailed",
                "Error while trying to create conversation.",
                details=e,
            )
        except ValueError as e:
            return common_utils.error(
                HTTPStatus.NOT_FOUND, "ConversationEventCopyError", details=e
            )

    @stack_endpoints.route(
        "/conversations/<conversation_id>/reviewStatus", methods=["PUT"]
    )
    @rasa_x_scoped("conversationReviewStatus.update")
    @validate_schema("conversation_review_status")
    async def put_conversation_review_status(
        request: Request, conversation_id: Text
    ) -> HTTPResponse:
        """
        Update the review status of a conversation.

        Args:
            request: Incoming HTTP request.
            conversation_id: ID of conversation to be updated.
        """
        payload = request.json or {}
        review_status = payload["review_status"]
        try:
            previous_review_status = _event_service(
                request
            ).update_conversation_review_status(conversation_id, review_status)
        except ValueError as e:
            return common_utils.error(
                HTTPStatus.NOT_FOUND, "ConversationError", details=e
            )

        telemetry.track_conversation_review_status_update(
            review_status, previous_review_status
        )
        return response.text("", status=HTTPStatus.NO_CONTENT)

    @stack_endpoints.route("/conversations/<conversation_id>", methods=["DELETE"])
    @rasa_x_scoped("metadata.delete", allow_api_token=True)
    async def delete_conversation_by_id(
        request: Request, conversation_id: Text
    ) -> HTTPResponse:
        """Deletes conversation with specified conversation ID.

        Args:
            request: Incoming HTTP request.
            conversation_id: ID of conversation to be deleted.
        """
        try:
            _event_service(request).delete_conversation_by_id(conversation_id)
            telemetry.track(telemetry.CONVERSATION_DELETED)
            return response.json("", status=HTTPStatus.NO_CONTENT)
        except ValueError:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                f'Conversation id "{conversation_id}" was not found',
            )

    @stack_endpoints.route(
        "/conversations/<conversation_id>/tracker", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("clients.get", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True, extract_user_from_jwt=True)
    async def get_conversation_tracker(
        request: Request, conversation_id: Text, user: Dict[Text, Any]
    ):
        return await get_conversation_tracker_impl(request, conversation_id, user)

    def _has_access_to_conversation(
        event_service: EventService, conversation_id: Text, user: Dict[Text, Any]
    ) -> bool:
        """Check if `user` can access the conversation with `conversation_id`.

        Args:
            event_service: an `EventService` instance.
            conversation_id: ID of the conversation.
            user: Rasa X user who sent the HTTP request.

        Returns:
            `True` if the user has the access to the conversation and `False` otherwise.
        """
        if user_service.has_role(user, user_service.ADMIN):
            return True

        if (
            user_service.has_role(user, user_service.GUEST)
            and user[constants.USERNAME_KEY] == conversation_id
        ):
            # because username is a chat token in this case, and this token
            # allows to initiate a conversation
            return True

        conversation = event_service.get_conversation(conversation_id)
        if conversation and conversation.created_by == user.get(constants.USERNAME_KEY):
            return True

        return False

    async def get_conversation_tracker_impl(
        request: Request, conversation_id: Text, user: Dict[Text, Any]
    ):
        is_full_conversation = (
            common_utils.default_arg(
                request, "format", FULL_CONVERSATION_FORMAT_REQUEST_PARAMETER
            )
            == FULL_CONVERSATION_FORMAT_REQUEST_PARAMETER
        )

        if not is_full_conversation:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                f"Currently only format: {FULL_CONVERSATION_FORMAT_REQUEST_PARAMETER} "
                "is supported for this endpoint.",
            )

        event_service = _event_service(request)

        if not _has_access_to_conversation(event_service, conversation_id, user):
            return common_utils.error(
                HTTPStatus.UNAUTHORIZED, "NoPermission", "Access denied"
            )

        until_time = common_utils.float_arg(request, "until", None)
        since_time = common_utils.float_arg(request, "since", None)
        rasa_environment_query = common_utils.default_arg(
            request, "rasa_environment", constants.DEFAULT_RASA_ENVIRONMENT
        )
        event_verbosity = _event_verbosity_from_request(request)
        exclude_leading_action_session_start = common_utils.bool_arg(
            request, "exclude_leading_action_session_start", False
        )

        tracker = event_service.get_tracker_with_message_flags(
            conversation_id,
            until_time,
            since_time,
            event_verbosity,
            rasa_environment_query,
            exclude_leading_action_session_start,
        )

        if not tracker:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "ClientNotFound",
                f"Client for conversation_id '{conversation_id}' could not be found",
            )

        requested_format = request.headers.get("Accept")

        if requested_format == "application/json":
            dispo = f"attachment;filename={conversation_id}-dump.json"
            return response.json(
                tracker,
                content_type="application/json",
                headers={"Content-Disposition": dispo},
            )
        elif requested_format == "text/markdown":
            _events = events.deserialise_events(tracker["events"])
            story = Story.from_events(_events)
            exported = story.as_story_string(flat=True)
            return response.text(
                exported,
                content_type="text/markdown",
                headers={
                    "Content-Disposition": f"attachment;filename={conversation_id}-story.md"
                },
            )
        else:
            return response.json(tracker, headers={"Content-Disposition": "inline"})

    @stack_endpoints.route("/conversationActions", methods=["GET", "HEAD"])
    @rasa_x_scoped("conversationActions.list", allow_api_token=True)
    async def unique_actions(request):
        actions = _event_service(request).get_unique_actions()
        return response.json(actions, headers={"X-Total-Count": len(actions)})

    @stack_endpoints.route("/conversationIntents", methods=["GET", "HEAD"])
    @rasa_x_scoped("conversationIntents.list", allow_api_token=True)
    async def unique_intents(request):
        intents = _event_service(request).get_unique_intents()
        return response.json(intents, headers={"X-Total-Count": len(intents)})

    @stack_endpoints.route("/conversationEntities", methods=["GET", "HEAD"])
    @rasa_x_scoped("conversationEntities.list", allow_api_token=True)
    async def unique_entities(request):
        entities = _event_service(request).get_unique_entities()
        return response.json(entities, headers={"X-Total-Count": len(entities)})

    @stack_endpoints.route("/conversationPolicies", methods=["GET", "HEAD"])
    @rasa_x_scoped("conversationPolicies.list", allow_api_token=True)
    async def unique_policies(request):
        policies = _event_service(request).get_unique_policies()
        return response.json(policies, headers={"X-Total-Count": len(policies)})

    @stack_endpoints.route("/conversations/<conversation_id>/events", methods=["POST"])
    @rasa_x_scoped("clientEvents.create", allow_api_token=True)
    @inject_rasa_x_user()
    async def post_event(request, conversation_id, user):
        _event = request.json
        try:
            _ = await _stack_service(
                request, constants.RASA_PRODUCTION_ENVIRONMENT
            ).append_events_to_tracker(conversation_id, _event)

            if tracker_utils.is_user_event(_event):
                # add annotated user messages to training data
                data_service = DataService(request[constants.REQUEST_DB_SESSION_KEY])
                data_service.save_user_event_as_example(
                    user, rasa_x_config.project_name, _event
                )

                telemetry.track_message_annotated(
                    telemetry.MESSAGE_ANNOTATED_INTERACTIVE_LEARNING
                )

            return response.json(_event)
        except ClientError as e:
            logger.warning(
                f"Creating event for sender '{conversation_id}' failed. Error: {e}"
            )
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "EventCreationError",
                f"Error when creating event for sender '{conversation_id}'.",
                details=e,
            )
        except ValueError as e:
            return common_utils.error(
                HTTPStatus.NOT_FOUND, "ServiceNotFound", details=e
            )

    @stack_endpoints.route("/conversations/<conversation_id>/events", methods=["PUT"])
    @rasa_x_scoped("clientEvents.update", allow_api_token=True)
    async def update_events(request, conversation_id):
        _events = request.json
        try:
            _ = await _stack_service(
                request, constants.RASA_PRODUCTION_ENVIRONMENT
            ).update_events(conversation_id, _events)
            return response.json(_events)
        except ClientError as e:
            logger.warning(
                f"Updating events for sender '{conversation_id}' failed. Error: {e}"
            )
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "EventUpdatingError",
                f"Error when updating events for sender '{conversation_id}'.",
            )

    @stack_endpoints.route("/conversations/<conversation_id>/execute", methods=["POST"])
    @rasa_x_scoped("actionPrediction.create", allow_api_token=True)
    async def execute_action(request, conversation_id):
        action = request.json
        event_verbosity = _event_verbosity_from_request(request)

        try:
            result = await _stack_service(
                request, constants.RASA_PRODUCTION_ENVIRONMENT
            ).execute_action(conversation_id, action, event_verbosity)
            return response.json(result)
        except ClientError as e:
            logger.warning(
                f"Executing action for sender '{conversation_id}' failed. Error: {e}"
            )
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "ActionExecutionFailed",
                f"Error when executing action for sender '{conversation_id}'.",
            )

    @stack_endpoints.route(
        "/conversations/<conversation_id>/messages", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("clientMessages.list")
    @inject_rasa_x_user()
    async def get_messages(request, conversation_id, user=None):
        event_service = _event_service(request)

        if not _has_access_to_conversation(event_service, conversation_id, user):
            return common_utils.error(
                HTTPStatus.UNAUTHORIZED, "NoPermission", "Access denied"
            )

        until_time = common_utils.float_arg(request, "until", None)
        event_verbosity = _event_verbosity_from_request(request)
        tracker = event_service.get_messages_for(
            conversation_id, until_time, event_verbosity
        )

        if not tracker:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "ClientNotFound",
                f"Client for conversation_id '{conversation_id}' could not be found.",
            )

        return response.json(
            tracker, headers={"X-Total-Count": len(tracker.get("messages"))}
        )

    @stack_endpoints.route(
        "/conversations/<conversation_id>/messages", methods=["POST"]
    )
    @rasa_x_scoped("messages.create")
    async def send_message(request, conversation_id):
        return await chat_to_bot(request, conversation_id)

    @stack_endpoints.route("/statistics", methods=["GET", "HEAD"])
    @rasa_x_scoped("statistics.get")
    @inject_rasa_x_user()
    async def get_statistics(request, user=None):
        event_service = _event_service(request)
        if not user_service.has_role(user, user_service.ADMIN):
            stats = event_service.user_statistics_dict()
        else:
            stats = event_service.get_statistics()
        return response.json(stats)

    @stack_endpoints.route("/evaluate", methods=["POST"])
    @rasa_x_scoped("allEvaluations.create")
    async def update_conversation_evaluations(request):
        event_service = _event_service(request)
        conversation_ids = event_service.conversation_ids()
        for _id in conversation_ids:
            story = event_service.story_for_conversation_id(_id)
            try:
                content = await _stack_service(
                    request, constants.RASA_WORKER_ENVIRONMENT
                ).evaluate_story(story)
                event_service.update_evaluation(_id, content)
            except ClientError as e:
                return common_utils.error(
                    HTTPStatus.BAD_REQUEST,
                    "CoreEvaluationFailed",
                    f"Failed to create evaluation for conversation with ID '{_id}'.",
                    details=e,
                )
            except ValueError as e:
                return common_utils.error(
                    HTTPStatus.NOT_FOUND,
                    "StoringEvaluationFailed",
                    f"Failed to store evaluation for conversation_id '{_id}'. Conversation not found.",
                    details=e,
                )

        return response.text("", HTTPStatus.NO_CONTENT)

    @stack_endpoints.route("/evaluations", methods=["GET", "HEAD"])
    @rasa_x_scoped("allEvaluations.list")
    async def get_evaluations(request):
        event_service = _event_service(request)
        conversation_ids = event_service.conversation_ids()
        evaluations = []
        for _id in conversation_ids:
            _eval = event_service.get_evaluation(_id)
            evaluations.append({_id: _eval})
        return response.json(evaluations, headers={"X-Total-Count": len(evaluations)})

    @stack_endpoints.route(
        "/conversations/<conversation_id>/evaluation", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("clientEvaluation.get")
    async def get_evaluation(request, conversation_id):
        evaluation = _event_service(request).get_evaluation(conversation_id)
        return response.json(evaluation)

    @stack_endpoints.route(
        "/conversations/<conversation_id>/evaluation", methods=["DELETE"]
    )
    @rasa_x_scoped("clientEvaluation.delete")
    async def delete_evaluation(request, conversation_id):
        _event_service(request).delete_evaluation(conversation_id)
        return response.text("", HTTPStatus.NO_CONTENT)

    @stack_endpoints.route(
        "/conversations/<conversation_id>/evaluation", methods=["PUT"]
    )
    @rasa_x_scoped("clientEvaluation.update")
    async def put_evaluation(request, conversation_id):
        event_service = _event_service(request)
        story = event_service.story_for_conversation_id(conversation_id)

        if not story:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "ClientNotFound",
                f"Client for conversation_id {conversation_id} could not be found",
            )

        try:
            content = await _stack_service(
                request, constants.RASA_WORKER_ENVIRONMENT
            ).evaluate_story(story)
            event_service.update_evaluation(conversation_id, content)

            return response.json(content)
        except ClientError as e:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "CoreEvaluationFailed",
                f"Failed to create evaluation for conversation_id {conversation_id}",
                details=e,
            )
        except ValueError as e:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "StoringEvaluationFailed",
                f"Failed to store evaluation for conversation_id '{conversation_id}'.",
                details=e,
            )

    @stack_endpoints.route("/conversations/<conversation_id>/predict", methods=["POST"])
    @rasa_x_scoped("actionPrediction.get")
    async def predict_next_action(request, conversation_id):
        included_events = common_utils.default_arg(request, "include_events", "ALL")

        try:
            content = await _stack_service(
                request, constants.RASA_PRODUCTION_ENVIRONMENT
            ).predict_next_action(conversation_id, included_events)
            return response.json(content)
        except ClientError as e:
            logger.warning(f"Model training failed. Error: {e}")
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "ActionPredictionFailed",
                f"Failed to predict the next action for conversation_id '{conversation_id}'.",
                details=e,
            )

    @stack_endpoints.route(
        "/conversations/<conversation_id>/messages/<message_timestamp:number>/flag",
        methods=["PUT"],
    )
    @rasa_x_scoped("messageFlags.update")
    async def flag_message(
        request: Request, conversation_id: Text, message_timestamp: float
    ) -> HTTPResponse:
        _event_service(request).add_flagged_message(conversation_id, message_timestamp)
        telemetry.track(telemetry.MESSAGE_FLAGGED_EVENT, {"set_flag": True})

        return response.text("", HTTPStatus.CREATED)

    @stack_endpoints.route(
        "/conversations/<conversation_id>/messages/<message_timestamp:number>/flag",
        methods=["DELETE"],
    )
    @rasa_x_scoped("messageFlags.delete")
    async def delete_flag_from_conversation(
        request: Request, conversation_id: Text, message_timestamp: float
    ) -> HTTPResponse:
        _event_service(request).delete_flagged_message(
            conversation_id, message_timestamp
        )
        telemetry.track(telemetry.MESSAGE_FLAGGED_EVENT, {"set_flag": False})

        return response.text("", HTTPStatus.OK)

    @stack_endpoints.route(
        "/conversations/<conversation_id>/messages/<message_timestamp:number>/intent",
        methods=["PUT"],
    )
    @rasa_x_scoped("messageIntents.update")
    @inject_rasa_x_user()
    @validate_schema("intent")
    async def correct_message(
        request: Request,
        conversation_id: Text,
        message_timestamp: float,
        user: Dict[Text, Text],
    ) -> HTTPResponse:
        try:
            intent = request.json
            project_id = common_utils.default_arg(
                request, "project_id", rasa_x_config.project_name
            )
            _event_service(request).correct_message(
                conversation_id, message_timestamp, intent, user, project_id
            )

            return response.text("", HTTPStatus.OK)
        except ValueError as e:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "MessageRelabellingError.", e
            )

    @stack_endpoints.route(
        "/conversations/<conversation_id>/messages/<message_timestamp:number>/intent",
        methods=["DELETE"],
    )
    @rasa_x_scoped("messageIntents.delete")
    async def delete_message_correction(
        request: Request, conversation_id: Text, message_timestamp: float
    ) -> HTTPResponse:
        try:
            project_id = common_utils.default_arg(
                request, "project_id", rasa_x_config.project_name
            )
            _event_service(request).delete_message_correction(
                conversation_id, message_timestamp, project_id
            )
            return response.text("", HTTPStatus.OK)
        except ValueError as e:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "MessageRelabellingError.", e
            )

    @stack_endpoints.route("/tests", methods=["POST"])
    @rasa_x_scoped("tests.create")
    @inject_rasa_x_user()
    async def add_tests(request: Request, user: Dict[Text, Text]) -> HTTPResponse:
        end_to_end_test_story = io_utils.convert_bytes_to_string(request.body)

        saved_test_stories: List[Text] = TestService.save_tests(end_to_end_test_story)
        if saved_test_stories and common_utils.is_git_available():
            from rasax.community.services.integrated_version_control.git_service import (
                GitService,
            )

            GitService(
                request[constants.REQUEST_DB_SESSION_KEY]
            ).track_training_data_dumping(user[constants.USERNAME_KEY], time.time())

        if saved_test_stories:
            telemetry.track_e2e_test_created(request.headers.get("Referer"))
            return response.json(saved_test_stories, HTTPStatus.CREATED)

        return common_utils.error(
            HTTPStatus.BAD_REQUEST, "TestsSaveError", "Failed to save tests."
        )

    @stack_endpoints.route("/chat", methods=["POST"])
    @rasa_x_scoped("messages.create")
    @validate_schema("message")
    async def chat_to_bot(request, conversation_id=None):
        message_object = request.json

        if conversation_id:
            message_object["conversation_id"] = conversation_id

        environment = common_utils.deployment_environment_from_request(
            request, constants.DEFAULT_RASA_ENVIRONMENT
        )
        service = _stack_service(request, constants.DEFAULT_RASA_ENVIRONMENT)
        if not service:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "ServiceNotFound",
                f"Service for requested environment '{environment}' not found.",
            )
        try:
            token = request.headers.get("Authorization")
            message_object[
                "input_channel"
            ] = StackService.get_user_properties_from_bearer(token)[0]
            message_response = await service.send_message(message_object, token=token)
            websocket_service.send_message(
                websocket_service.Message(
                    websocket_service.MessageTopic.MESSAGES,
                    name="message_sent",
                    data={"channel": message_object["input_channel"]},
                )
            )
            return response.json(message_response)
        except ClientError as e:
            logger.error(f"Failed to send message to Rasa Chat webhook. Error: {e}")
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "MessageSendFailed",
                "Message '{}' could not be sent to environment '{}'.".format(
                    message_object["message"], environment
                ),
            )

    @stack_endpoints.route("/auth/jwt", methods=["POST"])
    @validate_schema("signed_jwt")
    async def issue_signed_jwt(request):
        id_object = request.json
        chat_token = _domain_service(request).get_token()
        if not chat_token:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "ChatTokenNotFound",
                "Could not find valid chat_token.",
            )
        if id_object["chat_token"] != chat_token["chat_token"]:
            return common_utils.error(
                HTTPStatus.UNAUTHORIZED, "InvalidChatToken", "Chat token is not valid."
            )

        if _domain_service(request).has_token_expired(id_object["chat_token"]):
            return common_utils.error(
                HTTPStatus.UNAUTHORIZED, "ChatTokenExpired", "Chat token has expired."
            )

        # set JWT expiration to one day from current time or chat token
        # expiration time, whichever is lower
        expires = min(int(time.time()) + 24 * 60 * 60, chat_token["expires"])
        conversation_id = uuid.uuid4().hex

        jwt_object = {
            constants.USERNAME_KEY: conversation_id,
            "exp": expires,
            "user": {
                constants.USERNAME_KEY: conversation_id,
                "roles": [user_service.GUEST],
            },
            "scopes": normalise_permissions(guest_permissions()),
        }

        encoded = rasax.community.jwt.encode_jwt(
            jwt_object, rasa_x_config.jwt_private_key
        )

        return response.json(
            {"access_token": encoded, "conversation_id": conversation_id}
        )

    return stack_endpoints
