import asyncio  # pytype: disable=pyi-error
import json
import logging
import time
from pathlib import Path
from typing import Dict, Text, Any, Optional, List, Tuple, Union

from sanic.request import Request

from rasa.shared.constants import INTENT_MESSAGE_PREFIX
from rasa.shared.core.events import UserUttered, Event
import sqlalchemy as sa
from sqlalchemy import or_, false

import rasax.community.config as rasa_x_config
import rasax.community.utils.common as common_utils
import rasax.community.constants as constants
from rasax.community.database.conversation import MessageLog
from rasax.community.database.service import DbService
from rasax.community.services.model_service import ModelService
from rasax.community.services.settings_service import SettingsService

logger = logging.getLogger(__name__)


class LogsService(DbService):
    """Service to deal with parsed user messages."""

    def fetch_logs(
        self,
        text_query: Optional[Text] = None,
        intent_query: Optional[Text] = None,
        fields_query: Optional[List[Tuple[Text, bool]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        exclude_training_data: bool = False,
        sort_by: Optional[Text] = None,
        sort_order: Optional[Text] = None,
    ) -> common_utils.QueryResult:
        """Get the messages from all user conversations.

        Args:
            text_query: Text which the logs should be filtered by.
            intent_query: Intents separated by `,` whereby the message should at least
                match one (`OR` filter).
            fields_query: Fields which should be included in each returned message log
                object.
            limit: Maximum number of results to return.
            offset: Pagination offset.
            exclude_training_data: Whether to include message logs which are already
                part of the training data. The results obtained when setting this to
                `True` are useful to determine what new training data should be created
                to improve the understanding of future user messages.
            sort_by: Field to which sort results by.
            sort_order: Order in which results should be returned in, ascending or
                descending. Only applies when `sort_by` has been specified.

        Raises:
            ValueError: If the column specified in `sort_by` is invalid, or if
                the sorting order specified in `sort_order` is invalid.

        Returns:
            The filtered matching rows and the number of total matching rows.
        """
        # returns logs, sorts it in reverse-chronological order
        intents = intent_query.split(",") if intent_query else []
        query = True

        if text_query and intent_query:
            query = or_(
                MessageLog.text.like(f"%{text_query}%"), MessageLog.intent.in_(intents)
            )
        elif text_query:
            query = MessageLog.text.like(f"%{text_query}%")
        elif intent_query:
            query = MessageLog.intent.in_(intents)

        columns = common_utils.get_columns_from_fields(fields_query)
        # map `name` field to `intent`
        columns = [c if c != "name" else "intent" for c in columns]

        sort_order = sort_order or "desc"
        if sort_order not in ["desc", "asc"]:
            raise ValueError(
                f"Value for `sort_order` must be `asc` or `desc` (got: '{sort_order}')."
            )

        sort_column = MessageLog.__table__.columns.get(sort_by or "id")
        if sort_column is None:
            raise ValueError(f"Invalid column '{sort_by}' for MessageLog.")

        query_selectors = common_utils.get_query_selectors(MessageLog, columns)

        logs = (
            self.query(*query_selectors)
            .filter(query)
            .filter(MessageLog.archived == false())
        )

        if exclude_training_data:
            logs = logs.filter(MessageLog.in_training_data == false())

        # Get the total count before applying `sort`
        total_number_logs = logs.count()

        # Only order by selected column if it was selected (empty `columns`
        # implies all columns were selected)
        if not columns or sort_column.name in columns:
            logs = logs.order_by(
                sort_column.desc() if sort_order == "desc" else sort_column.asc()
            )

        logs = logs.offset(offset).limit(limit).all()

        if columns:
            results = [common_utils.query_result_to_dict(r, fields_query) for r in logs]
        else:
            results = [t.as_dict() for t in logs]

        return common_utils.QueryResult(results, total_number_logs)

    def archive(self, log_id: int) -> bool:
        """Mark a message log as archived.

        Args:
            log_id: The ID of the message log.

        Returns:
            `True` if a log with this ID was found, else `False`.
        """
        log = self._get_log_by_id(log_id)

        if log:
            log.archived = True

        return log is not None

    def _get_log_by_id(self, log_id: int) -> Optional[MessageLog]:
        return self.query(MessageLog).filter(MessageLog.id == log_id).first()

    def get_log_by_hash(self, _hash: Text) -> Optional[MessageLog]:
        """Get a log by its hashed text.

        Args:
            _hash: The text hash the logs are filtered by.

        Returns:
            A matching message log or `None` if no log matched.
        """
        return self.query(MessageLog).filter(MessageLog.hash == _hash).first()

    def replace_log(
        self,
        existing_log: Dict[Text, Any],
        parse_data: Dict[Text, Any],
        created_from_model: bool = True,
    ) -> Dict[Text, Any]:
        """Replace `existing_log` with log created from `parse_data`.

        `created_from_model` indicates whether `parse_data` has been created by a
        Rasa model.
        """

        new_log = self._create_log(parse_data, created_from_model=created_from_model)
        new_log.id = existing_log["id"]

        self.merge(new_log)

        return new_log.as_dict()

    def create_log_from_parse_data(
        self,
        parse_data: Dict[Text, Any],
        created_from_model: bool = True,
        event_id: Optional[int] = None,
        sender_id: Optional[Text] = None,
    ) -> Dict[Text, Any]:
        """Create a log from the Rasa NLU inference result.

        Args:
            parse_data: The NLU parse result.
            created_from_model: `True` if this log should be referenced with a model ID.
            event_id: The ID of the `ConversationEvent` object if the event was stored
                by the `EventService` previously.
            sender_id: ID of the `Conversation` object if the event was stored
                by the `EventService` previously.

        Returns:
            The saved message log.
        """
        project_id = parse_data.get("project") or rasa_x_config.project_name
        log = self._create_log(parse_data, event_id, created_from_model, sender_id)
        stored_log = self.get_log_by_hash(log.hash)
        if stored_log:
            self._merge_log_with_existing(log, stored_log.id)
        else:
            self._insert_new_log(log, project_id)

        logger.debug(f"Saving to NLU logs:\n{log}")

        return log.as_dict()

    def _merge_log_with_existing(self, log: MessageLog, id_of_existing: int) -> None:
        log.id = id_of_existing
        self.merge(log)

    def _insert_new_log(self, log: MessageLog, project_id: Text) -> None:
        log.in_training_data = self._is_log_with_hash_in_training_data(log, project_id)
        self.add(log)
        # flush so id gets assigned
        self.flush()

    def _is_log_with_hash_in_training_data(
        self, log: MessageLog, project_id: Text
    ) -> bool:
        from rasax.community.services.data_service import DataService

        data_service = DataService(self.session)
        return (
            log.text.startswith(INTENT_MESSAGE_PREFIX)
            or data_service.get_example_by_hash(project_id, log.hash) is not None
        )

    @staticmethod
    def _model_from_parse_data(parse_data: Dict[Text, Any]) -> Optional[Text]:
        # try to get the model from `parse_data`
        model_from_parse_data = parse_data.get("model")
        if model_from_parse_data:
            return model_from_parse_data

        logger.debug(
            "Could not find a model in the supplied NLU parse data. Will try to "
            "associate the message log with the current production model instead."
        )
        return None

    def _get_currently_active_model(self, project_id: Text) -> Optional[Text]:
        # try to get the model that's currently marked as production
        model_service = ModelService(
            "", self.session, constants.DEFAULT_RASA_ENVIRONMENT
        )
        active_model = model_service.model_for_tag(
            project_id, constants.DEFAULT_RASA_ENVIRONMENT
        )
        if active_model:
            return active_model["model"]

        logger.debug(
            f"Could not find a model currently marked as "
            f"`{constants.DEFAULT_RASA_ENVIRONMENT}` in Rasa X. "
            f"Will try to associate this log with the latest model stored in "
            f"the database instead."
        )
        return None

    def _get_latest_model(self, project_id: Text) -> Optional[Text]:
        model_service = ModelService(
            "", self.session, constants.DEFAULT_RASA_ENVIRONMENT
        )
        latest_model = model_service.latest_model(project_id)
        if latest_model:
            return latest_model.name

        logger.debug(
            f"Could not find a valid model. Will associate the message log "
            f"with model ID {constants.UNAVAILABLE_MODEL_NAME} instead."
        )

    def _get_loaded_model(
        self, project_id: Text, timeout_in_seconds: float = 0.5
    ) -> Optional[Text]:
        """Returns the name of the loaded Rasa model from the Rasa production
        service.

        If available, it returns the value of the `model_file` reported by the Rasa
        service's `/status` endpoint.

        Args:
            project_id: Name of the project.
            timeout_in_seconds: Request timeout in seconds which is used for
                HTTP request to the stack service.

        Returns:
            Name of the model file if it's available, else `None`.
        """
        settings_service = SettingsService(self.session)

        stack_service = settings_service.get_stack_service(
            constants.DEFAULT_RASA_ENVIRONMENT, project_id
        )
        if not stack_service:
            return None

        # we do not want to hold up the creation of the log, so
        # pass a very short timeout
        status = common_utils.run_in_loop(
            stack_service.server_status(timeout_in_seconds=timeout_in_seconds)
        )
        if not status:
            return None

        return self._get_model_name_from_model_file_status(status)

    @staticmethod
    def _get_model_name_from_model_file_status(
        status: Dict[Text, Any]
    ) -> Optional[Text]:
        model_file = status.get("model_file")

        if model_file:
            return Path(model_file).name.replace(".tar.gz", "")

        return None

    def _get_model_name_for_parse_data(
        self, project_id: Text, parse_data: Dict[Text, Any]
    ) -> Text:
        """Return the model name to be associated with `parse_data`.

        Args:
            project_id: Name of the project to be associated with this log.
            parse_data: NLU parse result.

        Returns:
            Model referenced in `parse_data` if present, otherwise the currently
            active model or the latest model in the database.
        """
        return (
            self._model_from_parse_data(parse_data)
            or self._get_currently_active_model(project_id)
            or self._get_loaded_model(project_id)
            or self._get_latest_model(project_id)
            or constants.UNAVAILABLE_MODEL_NAME
        )

    def _create_log(
        self,
        parse_data: Dict[Text, Any],
        event_id: Optional[int] = None,
        created_from_model: bool = True,
        sender_id: Optional[Text] = None,
    ) -> MessageLog:
        """Create a new `MessageLog` object from a parsed user message data.

        Args:
            parse_data: NLU parse result for a user message.
            event_id: ID of the user message event.
            created_from_model: `True` if this log should be referenced with a
                model ID.
            sender_id: ID of the conversation where the event occurred.

        Returns:
            A new `MessageLog` object.
        """
        project = parse_data.get("project") or rasa_x_config.project_name

        model_name = (
            self._get_model_name_for_parse_data(project, parse_data)
            if created_from_model
            else constants.UNAVAILABLE_MODEL_NAME
        )

        text = parse_data.get("text")
        intent = parse_data.get("intent", {})

        return MessageLog(
            model=model_name,
            text=text,
            hash=common_utils.get_text_hash(text),
            intent=intent.get("name"),
            confidence=intent.get("confidence", 0),
            entities=json.dumps(parse_data.get("entities", [])),
            intent_ranking=json.dumps(parse_data.get("intent_ranking", [])),
            time=time.time(),
            event_id=event_id,
            conversation_id=sender_id,
        )

    def save_nlu_logs_from_event(
        self,
        event_data: Union[Text, bytes],
        event_id: Optional[int] = None,
        sender_id: Optional[Text] = None,
    ) -> Optional[int]:
        """Save the log to the database in case it's a `UserUttered` event.

        Args:
            event_data: The event as JSON string.
            event_id: The ID of the `ConversationEvent` object if the event was stored
                by the `EventService` previously.
            sender_id: ID of the `Conversation` object if the event was stored
                by the `EventService` previously.

        Returns:
            The ID of the created or updated message log in the database.
        """
        try:
            event = Event.from_parameters(json.loads(event_data))
            if isinstance(event, UserUttered):
                log = self.create_log_from_parse_data(
                    event.parse_data, event_id=event_id, sender_id=sender_id
                )
                return log["id"]

        except ValueError as e:
            logger.exception(f"Could not persist event '{e}' to NLU logs:\n {e}")

    def bulk_update_in_training_data_column(
        self,
        message_log: Optional[sa.Table] = None,
        training_data: Optional[sa.Table] = None,
    ) -> None:
        """Bulk update the `in_training_data_column` of the `message_logs`.

        This should be triggered whenever there were bulk operations for the
        training data.

         Args:
            message_log: The reflected `MessageLog` table. If `None`, the method will
                try to obtain the table from the ORM.
            training_data: The reflected `NluTrainingData` table. If `None`, the method
                will try to obtain the table from the ORM.
        """
        if message_log is None:
            # noinspection PyUnresolvedReferences
            message_log = MessageLog.__table__

        if training_data is None:
            from rasax.community.database import TrainingData

            # noinspection PyUnresolvedReferences
            training_data = TrainingData.__table__

        # Nuke all `in_training_data` values
        query = sa.update(message_log).values(in_training_data=False)
        self.execute(query)

        # Update logs which are in the training data
        # This isn't done in single query since Oracle doesn't accept an `IN` clause
        # as part of a `SET` subquery.
        query = (
            sa.update(message_log)
            .values(in_training_data=True)
            .where(
                sa.or_(
                    message_log.c.hash.in_(sa.select([training_data.c.hash])),
                    message_log.c.text.like(f"{INTENT_MESSAGE_PREFIX}%"),
                )
            )
        )

        self.execute(query)

    @staticmethod
    def from_request(
        request: Request, other_service: "DbService" = None
    ) -> "LogsService":
        """Constructs Service object from the incoming request"""
        return LogsService(request[constants.REQUEST_DB_SESSION_KEY])
