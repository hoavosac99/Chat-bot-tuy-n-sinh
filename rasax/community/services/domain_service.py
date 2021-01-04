import json
import logging
import time
import uuid
from functools import reduce
from itertools import chain
from typing import Text, Dict, Any, Optional, List, Set, Tuple, Union, Iterable
from sqlalchemy import and_

import rasa.shared.core.constants
from rasa.shared.core.domain import (
    Domain as RasaDomain,
    SESSION_CONFIG_KEY,
    SESSION_EXPIRATION_TIME_KEY,
    CARRY_OVER_SLOTS_KEY,
)
from rasa.shared.core.slots import UnfeaturizedSlot
from rasa.shared.nlu.constants import RESPONSE_IDENTIFIER_DELIMITER
from rasa.shared.nlu.training_data.training_data import Message
from rasax.community.database.domain import (
    DomainEntity,
    DomainAction,
    DomainSlot,
    DomainIntent,
    Domain,
)
import rasax.community.utils.common as common_utils
import rasax.community.utils.cli as cli_utils
import rasax.community.utils.io as io_utils
import rasax.community.utils.yaml as yaml_utils
import rasax.community.config as rasa_x_config
from rasax.community.database.data import Response
from rasax.community.database.admin import ChatToken
from rasax.community.database.service import DbService
from rasax.community.services import background_dump_service

logger = logging.getLogger(__name__)


class DomainService(DbService):
    def store_domain(
        self,
        domain: Dict[Text, Any],
        project_id: Text,
        username: Text,
        path: Optional[Text] = None,
        store_responses: bool = False,
        have_responses_been_edited: bool = True,
    ) -> None:
        """Store a domain object.

        Args:
            domain: The domain as dictionary.
            project_id: The project_id the domain belongs to.
            path: File path of the domain.
            store_responses: Whether or not to store responses.
            username: Username performing this operation.
            have_responses_been_edited: Whether responses have been edited since the
                last training. Edited responses will be flagged as `edited=True`.
        """
        responses = self._responses(
            domain, have_responses_been_edited, project_id, store_responses, username
        )
        store_entities_as_slots = domain.get("config", {}).get(
            "store_entities_as_slots", True
        )
        session_config = domain.get(SESSION_CONFIG_KEY) or {}
        entities = _entities(domain)
        actions = _actions(domain)
        slots = _slots(domain)
        intents = _intents(domain)

        domain = Domain(
            project_id=project_id,
            store_entities_as_slots=store_entities_as_slots,
            session_expiration_time=session_config.get(SESSION_EXPIRATION_TIME_KEY),
            carry_over_slots=session_config.get(CARRY_OVER_SLOTS_KEY),
            actions=actions,
            intents=intents,
            slots=slots,
            entities=entities,
            path=path,
            responses=responses,
        )

        self._delete_old_domains(project_id)

        self.add(domain)

    def _responses(
        self,
        domain: Dict[Text, Any],
        have_responses_been_edited: bool,
        project_id: Text,
        store_responses: bool,
        username: Text,
    ) -> List[Response]:
        if store_responses:
            return _unique_responses(
                _create_responses_from_domain_dict(
                    domain, username, project_id, have_responses_been_edited
                )
            )

        previous_domain = self._get_domain(project_id)
        if previous_domain:
            # migrate existing responses to new domain
            return _unique_responses(
                _copy_responses_from_persisted_domain(previous_domain)
            )

        return []

    def _delete_old_domains(self, project_id: Text) -> None:
        old_domains = (
            self.session.query(Domain).filter(Domain.project_id == project_id).all()
        )
        self.delete_all(old_domains)

    def validate_and_store_domain_yaml(
        self,
        domain_yaml: Text,
        project_id: Text,
        username: Text,
        path: Optional[Text] = None,
        store_responses: bool = False,
        should_dump_domain: bool = True,
    ) -> Optional[Text]:
        """Store a domain from a yaml dump.

        Args:
            domain_yaml: The domain as yaml.
            project_id: The project_id the domain belongs to.
            path: File path of the domain.
            store_responses: Whether or not to store responses.
            username: Username performing this operation.
            should_dump_domain: Whether to dump domain to disk after storing it.
        """
        # create Rasa domain object (validation happens automatically)
        domain = RasaDomain.from_yaml(domain_yaml)
        cleaned = domain.cleaned_domain()
        self.store_domain(cleaned, project_id, username, path, store_responses)

        if should_dump_domain:
            background_dump_service.add_domain_change()

        return self.get_domain_yaml(project_id)

    def _get_domain(self, project_id: Text) -> Optional[Domain]:
        return (
            self.query(Domain)
            .filter(Domain.project_id == project_id)
            .order_by(Domain.id.desc())
            .first()
        )

    def dump_domain(self, project_id: Text, filename: Optional[Text] = None):
        """Dump domain to `filename` in yml format."""
        domain = self.get_domain(project_id)

        if not domain:
            return

        if not filename:
            filename = domain.get("path") or rasa_x_config.default_domain_path

        cleaned_domain = RasaDomain.from_dict(domain).cleaned_domain()
        domain_path = io_utils.get_project_directory() / filename
        yaml_utils.dump_yaml_to_file(domain_path, cleaned_domain)

    def get_domain(self, project_id: Text) -> Optional[Dict[Text, Any]]:
        """Return a stored domain, or `None` if no domain is present.

        Args:
            project_id: The project id of the domain.

        Returns:
            The domain as dictionary, or `None`.
        """
        domain = self._get_domain(project_id)
        return domain.as_dict() if domain else None

    def get_or_create_domain(self, project_id: Text, username: Text) -> Dict:
        """Return a stored domain, creating one if none exists.

        Args:
            project_id: The project id of the domain.
            username: User executing the action.

        Returns:
            The domain as dictionary.
        """
        return self._get_or_create_domain(project_id, username).as_dict()

    def _get_or_create_domain(self, project_id: Text, username: Text) -> Domain:
        domain = self._get_domain(project_id)
        if not domain:
            self.store_domain({}, project_id, username)
            domain = self._get_domain(project_id)

        return domain

    def get_parsed_domain(self, project_id: Text, username: Text) -> RasaDomain:
        return self._get_or_create_domain(project_id, username).as_rasa_domain()

    def has_empty_or_no_domain(self, project_id: Text) -> bool:
        """Return `True` if the project has an empty domain, or if the project has no
        domain associated.

        Args:
            project_id: The project id of the domain.

        Returns:
            `True` if the project has an empty domain, or no domain.
        """
        domain = self._get_domain(project_id)
        return not domain or domain.is_empty()

    @staticmethod
    def dump_cleaned_domain_yaml(domain: Dict[Text, Any]) -> Optional[Text]:
        """Take a domain as a dictionary, cleans it and returns it as a yaml string.

        Args:
            domain: Domain as a dictionary.

        Returns:
            The cleaned domain as a yaml string.
        """
        cleaned_domain = RasaDomain.from_dict(domain).cleaned_domain()
        return yaml_utils.dump_yaml(cleaned_domain)

    @staticmethod
    def remove_domain_edited_states(domain: Dict[Text, Any]) -> None:
        """Remove all 'edited_since_last_training' properties from responses in a
        domain. Modifies the domain in-place.

        Args:
            domain: Domain as a dictionary.
        """
        for responses in domain.get("responses", {}).values():
            for entry in responses:
                entry.pop("edited_since_last_training", None)

    def get_domain_yaml(self, project_id: Text) -> Optional[Text]:
        """Return a stored domain as yaml string.

        Args:
            project_id: The project id of the domain.

        Returns:
            The domain as yaml string.
        """
        domain = self.get_domain(project_id)
        return self.dump_cleaned_domain_yaml(domain) if domain else None

    def get_domain_id(self, project_id: Text) -> Optional[int]:
        """Return the ID of the stored domain.

        Args:
            project_id: The project id of the domain.

        Returns:
            The domain ID.
        """
        domain = self._get_domain(project_id)

        if domain:
            return domain.id

        return None

    def get_intents_from_domain(self, project_id: Text) -> Set[Text]:
        """Get all intents from the domain.

        Args:
            project_id: The project ID of the domain.

        Returns:
            Set of unique intent names.
        """
        intents = (
            self.query(DomainIntent.intent)
            .join(DomainIntent.domain)
            .filter(Domain.project_id == project_id)
            .all()
        )
        return {i for (i,) in intents}

    def get_entities_from_domain(self, project_id: Text) -> Set[Text]:
        """Get all entities from the domain.

        Args:
            project_id: The project id of the domain.

        Returns:
            Set of unique entity names.
        """
        entities = (
            self.query(DomainEntity.entity)
            .filter(DomainEntity.domain.has(Domain.project_id == project_id))
            .all()
        )
        return {e for (e,) in entities}

    def get_actions_from_domain(self, project_id: Text) -> Set[Text]:
        """Get all actions from the domain.

        Args:
            project_id: The project id of the domain.

        Returns:
            Set of unique action names.
        """
        actions = (
            self.query(DomainAction.action)
            .join(DomainAction.domain)
            .filter(Domain.project_id == project_id)
            .all()
        )
        return {a for (a,) in actions}

    def get_slots_from_domain(self, project_id: Text) -> Set[Text]:
        """Get all slot names from the domain.

        Args:
            project_id: The project id of the domain.

        Returns:
            Set of unique slot names.
        """
        slots = (
            self.query(DomainSlot.slot)
            .join(DomainSlot.domain)
            .filter(Domain.project_id == project_id)
            .all()
        )
        return {s for (s,) in slots}

    @staticmethod
    def _print_domain_change_info(_type: Text, origin: Text, items: List[Text]):
        if origin and items:
            cli_utils.print_info(
                "The following {} were found in your {} and will be added to the "
                "domain: {}".format(_type, origin, ", ".join(items))
            )

    def add_new_action(
        self, action: Dict[Text, Union[Text, bool]], project_id: Text, username: Text
    ) -> Dict[Text, Union[Text, bool]]:
        """Add a new action to the domain."""
        action_name = action["name"]

        domain = self._get_or_create_domain(project_id, username)
        if action_name in [action.action for action in domain.actions]:
            raise ValueError(f"Action '{action_name}' already exists.")

        new_action = DomainAction(
            action=action_name, is_form=action.get("is_form", False)
        )
        domain.actions.append(new_action)
        # Flush so new action gets id
        self.flush()

        return new_action.as_dict()

    def _add_actions_to_domain(
        self,
        domain: Domain,
        project_id: Text,
        actions: Optional[Iterable[Text]],
        origin: Optional[Text] = None,
    ) -> List[DomainAction]:
        domain_actions = self.get_actions_from_domain(project_id) or set()
        actions_to_add = set(actions) - domain_actions
        if not actions_to_add:
            logger.debug(
                "Actions '{}' are already contained in domain for "
                "project_id '{}'.".format(list(actions), project_id)
            )
            return []

        # exclude default actions from `actions_to_add`
        actions_to_add = [
            a
            for a in actions_to_add
            if a not in rasa.shared.core.constants.DEFAULT_ACTION_NAMES
        ]

        new_actions = [DomainAction(action=action) for action in actions_to_add]
        domain.actions.extend(new_actions)
        self._print_domain_change_info("actions", origin, actions_to_add)

        return new_actions

    def update_action(
        self, action_id: int, updated_action: Dict[Text, Union[Text, bool]]
    ) -> Dict[Text, Union[Text, bool]]:
        """Update an existing action by its ID."""
        action = self._get_action_for(action_id)

        if not action:
            raise ValueError(f"No action found for given id '{action_id}'.")

        action.action = updated_action.get("name") or action.action
        is_form = updated_action.get("is_form")
        if is_form is not None:
            action.is_form = is_form

        return action.as_dict()

    def _get_action_for(self, action_id: int) -> Optional[DomainAction]:
        return (
            self.query(DomainAction)
            .filter(and_(DomainAction.action_id == action_id))
            .first()
        )

    def delete_action(self, action_id: int) -> None:
        """Delete an existing action by its ID."""
        action = self._get_action_for(action_id)

        if not action:
            raise ValueError(f"No action found for given id '{action_id}'.")

        self.delete(action)

    def _add_slots_to_domain(
        self,
        domain: Domain,
        project_id: Text,
        slots: Optional[Iterable[Text]],
        origin: Optional[Text],
    ):
        domain_slots = self.get_slots_from_domain(project_id) or set()
        slots_to_add = set(slots) - domain_slots
        if not slots_to_add:
            logger.debug(
                "Slots '{}' are already contained in domain for "
                "project_id '{}'.".format(list(slots), project_id)
            )
            return

        new_slots = [DomainSlot(slot=slot) for slot in slots_to_add]
        domain.slots.extend(new_slots)
        self._print_domain_change_info("slots", origin, list(slots_to_add))

    def add_new_intent(self, project_id: Text, intent_name: Text) -> None:
        """Adds a new intent to the domain.

        Args:
            project_id: The project id of the domain.
            intent_name: The name of the intent to be created.
        """
        domain = self._get_domain(project_id)
        new_intent = DomainIntent(intent=intent_name)
        domain.intents.append(new_intent)

        self.flush()
        background_dump_service.add_domain_change()

    def intent_exists(self, project_id: Text, intent_name: Text) -> bool:
        """Queries the domain to see if the named intent exists.

        Args:
            project_id: The project id of the domain.
            intent_name: The name of the intent to be queried.

        Returns:
            True, if the named intent is present in the domain.
        """
        existing_intents = self.get_intents_from_domain(project_id)
        return intent_name in existing_intents

    @staticmethod
    def _get_retrieval_intents(intents: Optional[Iterable[Text]]) -> Set[Text]:
        """Get a list of retrieval intent names from `intents`

        Args:
            intents: Iterable of intent names.

        Returns:
            Retrieval intent names as list.
        """
        if not intents:
            return set()

        return set([i for i in intents if RESPONSE_IDENTIFIER_DELIMITER in i])

    def _add_intents_to_domain(
        self,
        domain: Domain,
        project_id: Text,
        intents: Optional[Iterable[Text]],
        origin: Optional[Text],
    ):
        domain_intents = self.get_intents_from_domain(project_id) or set()
        intents_to_add = (
            set(intents) - self._get_retrieval_intents(intents) - domain_intents
        )
        if not intents_to_add:
            logger.debug(
                "Intents '{}' are already contained in domain for "
                "project_id '{}'.".format(list(intents), project_id)
            )
            return

        new_intents = [DomainIntent(intent=intent) for intent in intents_to_add]
        domain.intents.extend(new_intents)
        self._print_domain_change_info("intents", origin, list(intents_to_add))

    def _add_entities_to_domain(
        self,
        domain: Domain,
        project_id: Text,
        entities: Optional[Iterable[Text]],
        origin: Optional[Text],
    ):
        domain_entities = self.get_entities_from_domain(project_id) or set()
        entities_to_add = set(entities) - domain_entities
        if not entities_to_add:
            logger.debug(
                "Entities '{}' are already contained in domain for "
                "project_id '{}'.".format(list(entities), project_id)
            )
            return None

        new_entities = [DomainEntity(entity=entity) for entity in entities_to_add]
        domain.entities.extend(new_entities)
        self._print_domain_change_info("entities", origin, list(entities_to_add))

    def add_items_to_domain(
        self,
        project_id: Text,
        username: Text,
        actions: Optional[Iterable[Text]] = None,
        intents: Optional[Iterable[Text]] = None,
        entities: Optional[Iterable[Text]] = None,
        slots: Optional[Iterable[Text]] = None,
        dump_data: bool = False,
        origin: Optional[Text] = None,
    ) -> Optional[Dict[Text, Any]]:
        """Add actions, intents, slots and entities to a project's domain. Create a new
        domain if none exists first.

        Args:
            project_id: The project id of the domain.
            username: User executing the action.
            actions: Set of action names to be added.
            intents: Set of intent names to be added.
            entities: Set of entity names to be added.
            slots: Set of the slot names to be added.
            dump_data: Whether to dump the domain.
            origin: origin of the domain changes to be printed as user info.

        Returns:
            Updated domain as dict.
        """
        domain = self._get_or_create_domain(project_id, username)

        if actions:
            self._add_actions_to_domain(domain, project_id, actions, origin)
        if slots:
            self._add_slots_to_domain(domain, project_id, slots, origin)
        if intents:
            self._add_intents_to_domain(domain, project_id, intents, origin)
        if entities:
            self._add_entities_to_domain(domain, project_id, entities, origin)

        if dump_data and any([actions, slots, entities, intents]):
            background_dump_service.add_domain_change()

        return self.get_domain(project_id)

    @staticmethod
    def _get_entities_from_training_data(entity_examples: List[Message]) -> Set[Text]:
        # exclude entities with an extractor attribute
        return {
            entity.get("entity")
            for example in entity_examples
            for entity in example.get("entities")
            if not entity.get("extractor")
        }

    async def get_domain_warnings(
        self, project_id: Text = rasa_x_config.project_name
    ) -> Optional[Tuple[Dict[Text, Dict[Text, List[Text]]], int]]:
        """Get domain warnings.

        Args:
            project_id: The project id of the domain.

        Returns:
            Dict of domain warnings and the total count of elements.
        """
        domain = self._get_domain(project_id)

        if domain:
            from rasax.community.services.data_service import DataService
            from rasax.community.services.nlg_service import NlgService
            from rasax.community.services.story_service import StoryService

            domain_object = RasaDomain.from_dict(domain.as_dict())

            training_data = DataService(self.session).get_nlu_training_data_object(
                project_id=project_id
            )

            # actions are response names and story bot actions
            actions = NlgService(self.session).fetch_all_response_names()

            # intents are training data intents and story intents
            intents = training_data.intents

            # entities are training data entities without `extractor` attribute
            entity_examples = training_data.entity_examples
            entities = self._get_entities_from_training_data(entity_examples)

            # slots are simply story slots
            slots = set()

            story_events = await StoryService(
                self.session
            ).fetch_domain_items_from_stories(project_id)

            if story_events:
                actions.update(story_events[0])
                intents.update(story_events[1])
                slots.update(story_events[2])
                entities.update(story_events[3])

            # exclude unfeaturized slots from warnings
            slots = self._remove_unfeaturized_slots(slots, domain_object)

            domain_warnings = self._domain_warnings_as_list(
                domain_object, intents, entities, actions, slots
            )

            return domain_warnings, self._count_total_warnings(domain_warnings)

        return None

    @staticmethod
    def _domain_warnings_as_list(
        domain_object: RasaDomain,
        intents: Set[Text],
        entities: Set[Text],
        actions: Set[Text],
        slots: Set[Text],
    ) -> Dict[Text, Dict[Text, List[Text]]]:
        """Returns domain warnings for `domain` object.

        Converts sets in domain warnings to lists for json serialisation.
        """

        _warnings = domain_object.domain_warnings(intents, entities, actions, slots)

        warnings = {}

        # convert sets to lists in dictionary at depth 2
        for warning_type, value in _warnings.items():
            warnings[warning_type] = {}
            for location, warning_set in value.items():
                if isinstance(warning_set, set):
                    warnings[warning_type][location] = list(warning_set)
                else:
                    warnings[warning_type][location] = warning_set

        # TODO: Fix on the Rasa OSS side: default intents are being included in
        # the domain warnings.

        # - - - - - - - - 8< - - - - - - -

        from rasa.shared.core.constants import DEFAULT_INTENTS

        intent_warnings = warnings.get("intent_warnings", {}).get("in_domain")
        if intent_warnings:
            for intent in DEFAULT_INTENTS:
                try:
                    intent_warnings.remove(intent)
                except ValueError:
                    pass

        # - - - - - - - - >8 - - - - - - -

        return warnings

    @staticmethod
    def _count_total_warnings(domain_warnings: Dict[Text, Any]) -> int:
        # iterator containing lengths of all warning sets
        warning_elements = chain(
            len(s) for t in domain_warnings.values() for s in t.values()
        )
        return reduce(lambda x, y: x + y, warning_elements)  # sum of this list

    @staticmethod
    def _expiration_timestamp(lifetime: int = 30) -> float:
        """Generate expiration timestamp `lifetime` days from current time."""

        return time.time() + lifetime * 60 * 60 * 24

    def generate_and_save_token(self, lifetime: int = 30) -> ChatToken:
        """Generate and save chat_token to db with `lifetime` in days."""

        token = uuid.uuid4().hex
        expires = self._expiration_timestamp(lifetime)
        chat_token = ChatToken(token=token, expires=int(expires))

        old_token = self._get_token()
        if old_token:
            self.delete(old_token)

        self.add(chat_token)

        return chat_token

    def update_token(self, bot_name: Text, description: Text, lifetime: int = 30):
        """Update chat_token by adding name and description, the expiry date is
        set to 30 days from the current date."""

        expires = self._expiration_timestamp(lifetime)
        token = self._get_token()
        token.bot_name = bot_name
        token.description = description
        token.expires = int(expires)

    def update_token_from_dict(self, update_dict: Dict[Text, Text], lifetime: int = 30):
        """Update chat_token from supplied `update_dict`.

        `update_dict` should contain keys `bot_name` and `description`.
        """

        self.update_token(
            update_dict.get("bot_name", ""),
            update_dict.get("description", ""),
            lifetime,
        )

    def has_token_expired(self, chat_token: Text) -> bool:
        """Return True if `chat_token` has expired, or token is not in db.

        Return False otherwise.
        """

        db_token = self._get_token()
        if db_token.token != chat_token:
            return True

        if int(time.time()) > db_token.expires:
            return True

        return False

    def get_token(self) -> Optional[Dict[Text, Text]]:
        """Get chat_token as dict."""

        token = self._get_token()
        if token:
            return token.as_dict()

        return None

    def _get_token(self) -> Optional[ChatToken]:
        return self.query(ChatToken).first()

    @staticmethod
    def _remove_unfeaturized_slots(
        slots: Set[Text], domain_object: RasaDomain
    ) -> Set[Text]:
        unfeaturized_domain_slots = [
            slot.name
            for slot in domain_object.slots
            if isinstance(slot, UnfeaturizedSlot)
        ]
        return {slot for slot in slots if slot not in unfeaturized_domain_slots}


def _entities(domain: Dict[Text, Any]) -> List[DomainEntity]:
    entities = domain.get("entities", [])
    return [DomainEntity(entity=e) for e in entities]


def _form_name(form: Union[Text, Dict[Text, Any]]) -> Text:
    """Extract the name of a form, given its entry object in the domain file.
    Forms were previously described with a single string (their name), but from
    Rasa OSS 2.0.0, they are objects with properties.

    Args:
        form: Object (string or dict) representing the form in the domain.

    Returns:
        Form name.
    """
    if isinstance(form, str):
        return form

    return list(form.keys())[0]


def _actions(domain: Dict[Text, Any]) -> List[DomainAction]:
    actions = domain.get("actions", [])
    actions = [DomainAction(action=a, is_form=False) for a in actions]

    forms = domain.get("forms", [])
    forms = [DomainAction(action=_form_name(f), is_form=True) for f in forms]

    return actions + forms


def _slots(domain: Dict[Text, Any]) -> List[DomainSlot]:
    slots = domain.get("slots", {})
    return [
        DomainSlot(
            slot=s,
            auto_fill=v.get("auto_fill", True),
            initial_value=json.dumps(v["initial_value"])
            if v.get("initial_value") is not None
            else None,
            type=v.get("type", "rasa.shared.core.slots.UnfeaturizedSlot"),
            values=json.dumps(v["values"]) if v.get("values") else None,
        )
        for s, v in slots.items()
    ]


def _intents(domain: Dict[Text, Any]) -> List[DomainIntent]:
    intents_raw = domain.get("intents", [])
    intents = []
    for i in intents_raw:
        if isinstance(i, str):
            name = i
            _config = {}
        else:
            name, _config = next(iter(i.items()))

        intents.append(
            DomainIntent(
                intent=name,
                use_entities=json.dumps(_config.get("use_entities", True)),
                ignore_entities=json.dumps(_config.get("ignore_entities", [])),
                triggered_action=_config.get("triggers"),
            )
        )

    return intents


def _copy_responses_from_persisted_domain(domain: Domain) -> List[Response]:
    """Copy the `Response` objects from the `Domain` database object.

    Args:
        domain: A persisted domain including referenced responses.

    Returns:
        A copy of the responses included in the domain.
    """
    return [
        Response(
            response_name=response.response_name,
            content=response.content,
            text=response.text,
            annotator_id=response.annotator_id,
            annotated_at=response.annotated_at,
            project_id=response.project_id,
            hash=response.hash,
        )
        for response in domain.responses
    ]


def _create_responses_from_domain_dict(
    domain: Dict[Text, Any],
    username: Text,
    project_id: Text,
    have_responses_been_edited: bool = True,
) -> List[Response]:
    """Create `Response` objects based on the assistant's domain as a `dict`.

    Args:
        domain: The domain which includes the responses in the `responses` key.
        username: The name of the user who is creating the responses.
        project_id: The project ID which the responses should belong to.
        have_responses_been_edited: `True` if the responses should be marked as edited
            since the last training.

    Returns:
        The created responses objects which now can be added to the database.
    """
    responses = domain.get("responses", domain.get("templates", {}))

    return [
        Response(
            response_name=response_name.strip() if response_name else None,
            content=json.dumps(response, sort_keys=True),
            text=response["text"].strip() if response.get("text") else None,
            annotator_id=username,
            annotated_at=time.time(),
            project_id=project_id,
            edited_since_last_training=have_responses_been_edited,
            hash=common_utils.get_text_hash(json.dumps(response, sort_keys=True)),
        )
        for response_name, response_as_list in responses.items()
        for response in response_as_list
    ]


def _unique_responses(responses: List[Response]) -> List[Response]:
    """Remove duplicates from the list of responses and return a new list"""

    # We don't use __eq__ or __hash__ here because Response objects are
    # actually different
    unique_responses = set()
    new_responses = []
    for r in responses:
        fingerprint = (r.project_id, r.response_name, r.hash)
        if fingerprint not in unique_responses:
            new_responses.append(r)
            unique_responses.add(fingerprint)

    return new_responses
