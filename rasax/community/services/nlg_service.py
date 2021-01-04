import itertools
import json
import logging
import time
from typing import Text, List, Dict, Any, Optional, Tuple, Set, NamedTuple

from sqlalchemy import or_, and_, true, false

import rasax.community.constants as constants
import rasax.community.config as rasa_x_config
import rasax.community.utils.common as common_utils
import rasax.community.utils.cli as cli_utils
from rasa.shared.constants import UTTER_PREFIX
from rasax.community.database.data import Response
from rasax.community.database.service import DbService

logger = logging.getLogger(__name__)


def _fix_response_name_key(response: Dict[Text, Any]) -> None:
    if "template" in response:
        response[constants.RESPONSE_NAME_KEY] = response.pop("template")
        cli_utils.raise_warning(
            f"The response you provided includes the key 'template'. This key has been "
            f"deprecated and renamed to '{constants.RESPONSE_NAME_KEY}'. The 'template' key will "
            f"no longer work in future versions of Rasa X. Please use "
            f"'{constants.RESPONSE_NAME_KEY}' instead.",
            FutureWarning,
        )


class HashedResponse(NamedTuple):
    response_content: Text
    content_hash: Text


def _get_hashed_response(response: Dict[Text, Any]) -> HashedResponse:
    response_content = json.dumps(response, sort_keys=True)
    content_hash = common_utils.get_text_hash(response_content)

    return HashedResponse(response_content=response_content, content_hash=content_hash)


class NlgService(DbService):
    def save_response(
        self,
        response: Dict[Text, Any],
        username: Optional[Text] = None,
        domain_id: Optional[int] = None,
        project_id: Text = rasa_x_config.project_name,
    ) -> Dict[Text, Any]:
        """Save response.

        Args:
            response: Response object to save.
            username: Username performing the save operation.
            domain_id: Domain associated with the response.
            project_id: Project ID associated with the response.

        Raises:
            `ValueError` if any of:
            1. no domain ID is specified
            2. the response name does not start with `rasa.shared.constants.UTTER_PREFIX`
            3. a response with the same text already exists

        Returns:
            The saved response object.
        """
        response = response.copy()

        if domain_id is None:
            raise ValueError("Response could not be saved since domain ID is `None`.")

        _fix_response_name_key(response)

        response_name = Response.get_stripped_value(
            response, constants.RESPONSE_NAME_KEY
        )

        if not response_name or not response_name.startswith(UTTER_PREFIX):
            raise AttributeError(
                f"Failed to save response. Response '{response_name}' does "
                f"not begin with '{UTTER_PREFIX}' prefix."
            )

        response[constants.RESPONSE_NAME_KEY] = response_name
        response["text"] = Response.get_stripped_value(response, "text")

        if self.get_response(project_id, response):
            raise ValueError(
                f"Another response with same pair of ({constants.RESPONSE_NAME_KEY}, text) "
                f"(({response_name},{response['text']})) already exists for this "
                f"project and domain."
            )

        if not username:
            username = rasa_x_config.default_username

        hashed_response = _get_hashed_response(response)

        new_response = Response(
            response_name=response[constants.RESPONSE_NAME_KEY],
            text=response["text"],
            content=hashed_response.response_content,
            annotated_at=time.time(),
            annotator_id=username,
            project_id=project_id,
            hash=hashed_response.content_hash,
        )

        if domain_id:
            new_response.domain_id = domain_id

        self.add(new_response)

        # flush so ID becomes available
        self.flush()

        return new_response.as_dict()

    def fetch_responses(
        self,
        text_query: Optional[Text] = None,
        response_query: Optional[Text] = None,
        fields_query: List[Tuple[Text, bool]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        sort_by_response_name: bool = False,
        intersect_filters: bool = False,
    ) -> common_utils.QueryResult:
        """Returns a list of responses. Each response includes its response name
        as a property under RESPONSE_NAME.

        An example of a response item could be:
        ```
        {
            "text": "Hey! How are you?",
            RESPONSE_NAME: "utter_greet",
            "id": 6,
            "annotator_id": "me",
            "annotated_at": 1567780833.8198001,
            "project_id": "default",
            "edited_since_last_training": false
        }
        ```

        Args:
            text_query: Filter results by response text (case insensitive).
            response_query: Filter results by response name.
            fields_query: Fields to include per item returned.
            limit: Maximum number of responses to be retrieved.
            offset: Excludes the first ``offset`` responses from the result.
            sort_by_response_name: If ``True``, sort responses by their
                RESPONSE_NAME property.
            intersect_filters: If ``True``, join ``text_query`` and
                ``response_query`` conditions with an AND instead of an OR.

        Returns:
            List of responses with total number of responses found.
        """

        responses_to_query = response_query.split(",") if response_query else []
        columns = common_utils.get_columns_from_fields(fields_query)

        if not responses_to_query and not text_query:
            # Case 1: No conditions were specified - base query is TRUE (all results)
            query = true()
        else:
            if intersect_filters:
                # Case 2: One or both conditions were specified, and caller wants
                #         to join them with AND.
                query = true()
            else:
                # Case 3: One or both conditions were specified, and caller wants
                #         to join them with OR.
                query = false()

        query_joiner = and_ if intersect_filters else or_

        if text_query:
            query = query_joiner(query, Response.text.ilike(f"%{text_query}%"))

        if response_query:
            query = query_joiner(query, Response.response_name.in_(responses_to_query))

        responses = self.query(
            *common_utils.get_query_selectors(Response, columns)
        ).filter(query)

        if sort_by_response_name:
            responses = responses.order_by(Response.response_name.asc())

        total_number_of_results = responses.count()

        responses = responses.offset(offset).limit(limit).all()

        if columns:
            results = [
                common_utils.query_result_to_dict(r, fields_query) for r in responses
            ]
        else:
            results = [t.as_dict() for t in responses]

        return common_utils.QueryResult(results, total_number_of_results)

    def get_grouped_responses(
        self, text_query: Optional[Text] = None, response_query: Optional[Text] = None
    ) -> common_utils.QueryResult:
        """Return responses grouped by their response name.

        Args:
            text_query: Filter responses by response text (case insensitive).
            response_query: Filter response groups by response name.

        Returns:
            `QueryResult` containing grouped responses and total number of responses
            across all groups.
        """
        # Sort since `groupby` only groups consecutive entries
        responses = self.fetch_responses(
            text_query=text_query,
            response_query=response_query,
            sort_by_response_name=True,
            intersect_filters=True,
        ).result

        grouped_responses = itertools.groupby(
            responses, key=lambda each: each[constants.RESPONSE_NAME_KEY]
        )

        result = [
            {constants.RESPONSE_NAME_KEY: k, "responses": list(g)}
            for k, g in grouped_responses
        ]
        count = sum(len(item["responses"]) for item in result)

        return common_utils.QueryResult(result, count)

    def fetch_all_response_names(self) -> Set[Text]:
        """Fetch a list of all response names in db."""

        return {t[constants.RESPONSE_NAME_KEY] for t in self.fetch_responses()[0]}

    def delete_response(self, _id: int) -> bool:
        delete_result = self.query(Response).filter(Response.id == _id).delete()

        return delete_result

    def delete_all_responses(self) -> None:
        self.query(Response).delete()

    def update_response(
        self, _id: int, response: Dict[Text, Any], username: Text
    ) -> Optional[Response]:
        response = response.copy()

        old_response = self.query(Response).filter(Response.id == _id).first()
        if not old_response:
            raise KeyError(f"Could not find existing response with ID '{_id}'.")

        _fix_response_name_key(response)

        response_name = Response.get_stripped_value(
            response, constants.RESPONSE_NAME_KEY
        )

        if not response_name or not response_name.startswith(UTTER_PREFIX):
            raise AttributeError(
                f"Failed to update response. Response '{response_name}' does "
                f"not begin with '{UTTER_PREFIX}' prefix."
            )

        response[constants.RESPONSE_NAME_KEY] = response_name
        response["text"] = Response.get_stripped_value(response, "text")

        if self.get_response(old_response.project_id, response):
            raise ValueError(
                f"Response could not be saved since another one with same pair of "
                f"({constants.RESPONSE_NAME_KEY}, text) exists."
            )

        hashed_response = _get_hashed_response(response)

        old_response.response_name = response[constants.RESPONSE_NAME_KEY]
        old_response.text = response["text"]
        old_response.annotated_at = time.time()
        old_response.content = hashed_response.response_content
        old_response.annotator_id = username
        old_response.edited_since_last_training = True
        old_response.hash = hashed_response.content_hash

        # because it's the same as the updated response
        return old_response

    def replace_responses(
        self,
        new_responses: List[Dict[Text, Any]],
        username: Optional[Text] = None,
        domain_id: Optional[int] = None,
    ) -> int:
        """Deletes all responses and adds new responses.

        Returns the number of inserted responses.
        """
        self.delete_all_responses()
        insertions = 0
        for response in new_responses:
            try:
                inserted = self.save_response(response, username, domain_id=domain_id)
                if inserted:
                    insertions += 1
            except ValueError:
                pass

        return insertions

    def get_response(
        self, project_id: Text, response: Dict[Text, Any]
    ) -> Optional[Response]:
        """Return a response that has the specified `project_id` and `response`.

        Args:
            project_id: Project ID.
            response: Response.
        """
        _fix_response_name_key(response)

        return (
            self.query(Response)
            .filter(
                and_(
                    Response.project_id == project_id,
                    Response.hash == _get_hashed_response(response).content_hash,
                )
            )
            .first()
        )

    def mark_responses_as_used(
        self, training_start_time: float, project_id: Text = rasa_x_config.project_name
    ) -> None:
        """Unset the `edited_since_last_training_flag` for responses which were included
        in the last training.

        Args:
            training_start_time: annotation time until responses should be marked as
                used in training.
            project_id: Project which was trained.

        """
        responses_which_were_used_in_training = (
            self.query(Response)
            .filter(
                and_(
                    Response.annotated_at <= training_start_time,
                    Response.edited_since_last_training,
                    Response.project_id == project_id,
                )
            )
            .all()
        )
        for response in responses_which_were_used_in_training:
            response.edited_since_last_training = False

    def rename_responses(
        self, old_response_name: Text, response: Dict[Text, Text], annotator: Text
    ) -> None:
        """
        Bulk-rename all responses with this response name.

        Args:
            old_response_name: The name of the response which should be renamed.
            response: The object containing the new response values.
            annotator: The name of the user who is doing the rename.
        """
        responses = (
            self.query(Response)
            .filter(Response.response_name == old_response_name)
            .all()
        )
        new_response_name = response["name"]
        for response in responses:
            content = json.loads(response.content)
            content[constants.RESPONSE_NAME_KEY] = new_response_name
            hashed_response = _get_hashed_response(content)

            response.response_name = new_response_name
            response.annotated_at = time.time()
            response.annotator_id = annotator
            response.content = hashed_response.response_content
            response.hash = hashed_response.content_hash
