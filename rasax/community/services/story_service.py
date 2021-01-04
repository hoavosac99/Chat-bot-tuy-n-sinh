import copy
import logging
import re
import time
import typing
from pathlib import Path
from typing import Any, Text, Dict, List, Optional, Tuple, Set, Union

from sqlalchemy import and_, or_

import rasa.shared.constants
import rasa.shared.core.constants
from rasa.shared.core.domain import Domain as RasaDomain
from rasa.shared.core.events import ActionExecuted, UserUttered, SlotSet
from rasa.shared.core.training_data.story_reader.story_reader import (
    StoryReader,
    StoryParseError,
)
from rasa.shared.core.training_data.story_reader.markdown_story_reader import (
    MarkdownStoryReader,
)
from rasa.shared.core.training_data.story_reader.yaml_story_reader import (
    YAMLStoryReader,
)
from rasa.shared.core.training_data.story_writer.yaml_story_writer import (
    YAMLStoryWriter,
)
from rasa.shared.core.training_data.structures import StoryStep, RuleStep
import rasax.community.config as rasa_x_config
import rasax.community.data as data
import rasax.community.constants as constants
import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
from rasax.community.database.admin import User
from rasax.community.database.data import Story
from rasax.community.database.service import DbService
from rasax.community.services import background_dump_service

if typing:
    from rasa.shared.core.domain import Domain
    from sanic.request import Request

logger = logging.getLogger(__name__)


class StoryService(DbService):
    @staticmethod
    def _get_reader(
        domain: Domain,
        filename: Optional[Text] = None,
        file_format: Optional[data.FileFormat] = None,
    ) -> "StoryReader":
        """Return an appropriate story reader, depending on an expicitly specified
        file type, or on a filename's extension.

        Args:
            domain: Domain for story file.
            filename: Name of file containing stories, if present.
            file_format: Story file format, if already known.

        Raises:
            ValueError: If the story file's format could not be determined.

        Returns:
            `StoryReader` subclass for reading stories.
        """
        if not filename and not file_format:
            raise ValueError("Not enough information to determine story format.")

        if filename:
            format_from_filename = data.format_from_filename(filename)

            if format_from_filename == data.FileFormat.YAML:
                return YAMLStoryReader(domain, source_name=filename)
            elif format_from_filename == data.FileFormat.MARKDOWN:
                return MarkdownStoryReader(domain, source_name=filename)

        if file_format == data.FileFormat.YAML:
            return YAMLStoryReader(domain)
        elif file_format == data.FileFormat.MARKDOWN:
            return MarkdownStoryReader(domain)

        raise ValueError(f"Unable to determine story file format for '{filename}'.")

    @staticmethod
    def _reader_read_from_string(
        reader: "StoryReader", story_string: Text
    ) -> List[StoryStep]:
        """The `StoryReader` interface does not have a method that accepts
        stories as strings, so this additional method is necessary. Returns a
        list of `StoryStep` from a story string.

        Args:
            reader: Story reader instance.
            story_string: Stories in string form.

        Returns:
            List of `StoryStep`, each element representing a story.
        """
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            temp_path = f.name
            f.write(story_string)

        try:
            return reader.read_from_file(temp_path)
        except ValueError as e:
            raise StoryParseError from e
        finally:
            os.remove(temp_path)

    @staticmethod
    def get_story_steps(
        story_string: Text,
        file_format: data.FileFormat,
        domain: Dict[Text, Any] = None,
        filename: Optional[Text] = None,
    ) -> List[StoryStep]:
        """Returns the stories contained inside a stories file.

        Also checks if the intents in the stories are in the provided domain. For each
        intent not present in the domain, a UserWarning is issued.

        Args:
            story_string: String representing the contents of a stories file.
            file_format: Format the stories file uses.
            domain: Domain for defined stories.
            filename: Name of the file where the text contents come from.

        Returns:
            `StoryStep` object for each story contained in the file.
        """
        # Domain is not needed in `StoryFileReader` when parsing stories, but
        # if none is provided there will be a UserWarning for each intent.
        if not domain:
            domain = {}
        else:
            domain = copy.deepcopy(domain)

        domain = RasaDomain.from_dict(domain)

        reader = StoryService._get_reader(
            domain, file_format=file_format, filename=filename
        )

        try:
            return StoryService._reader_read_from_string(reader, story_string)
        except (AttributeError, ValueError) as e:
            raise StoryParseError(
                "Invalid story format. Failed to parse "
                "'{}'\nError: {}".format(story_string, e)
            )

    def _get_stories_as_markdown_string(self, stories: List[Dict[Text, Any]]) -> Text:
        bulk_content = ""
        skipped = set()

        for story in stories:
            if data.format_from_filename(story["filename"]) == data.FileFormat.MARKDOWN:
                bulk_content += "{}\n\n".format(story.get("story"))
            else:
                skipped.add(story["filename"])

        if len(bulk_content):
            bulk_content = bulk_content.strip()
            bulk_content += "\n"

        if skipped:
            logger.warning(
                f"Serialization of stories from non-Markdown to Markdown format is not "
                f"supported. Stories from the following files have been skipped: "
                f"{list(skipped)}."
            )

        return bulk_content

    def _get_stories_as_yaml_string(
        self, project_id: Text, stories: List[Dict[Text, Any]]
    ) -> Text:
        from rasax.community.services.domain_service import DomainService

        domain_service = DomainService(self.session)
        domain = domain_service.get_domain(project_id)

        writer = YAMLStoryWriter()
        steps_list = []

        for story in stories:
            story_format = data.format_from_filename(story["filename"])
            steps_list.extend(
                StoryService.get_story_steps(
                    story["story"],
                    story_format,
                    filename=story["filename"],
                    domain=domain,
                )
            )

        return writer.dumps(steps_list)

    def get_stories_as_string(
        self,
        project_id: Text,
        stories: List[Dict[Text, Any]],
        file_format: data.FileFormat,
    ) -> Text:
        """Concatenate a list of stories into a single file, and return its
        contents as a string.

        Args:
            project_id: Target project ID.
            stories: List of stories as dictionaries.
            file_format: Target output format.

        Returns:
            Contents of a stories file, as a string.
        """
        if file_format == data.FileFormat.MARKDOWN:
            return self._get_stories_as_markdown_string(stories)
        elif file_format == data.FileFormat.YAML:
            return self._get_stories_as_yaml_string(project_id, stories)

        raise ValueError(f"Unknown file format: '{file_format}'.")

    async def _extract_stories_markdown(
        self, story_string: Text, filename: Text, domain: Optional[Dict[Text, Any]],
    ) -> List[Tuple[Text, StoryStep]]:
        """Extract stories from the text contents of a Markdown file.

        Args:
            story_string: Stories in string form (Markdown format).
            filename: Name of stories file.
            domain: Stories domain.

        Returns:
            List of tuples, each tuple containing a story in string form and
            its `StoryStep` representation.
        """
        # split up data into blocks (a new StoryStep begins with #)
        # a split on `#` covers stories beginning with either `##` or `#`
        split_story_string = re.split("(\n|^)##?", story_string)

        # blocks are the non-empty entries of `split_story_string`
        blocks = [s for s in split_story_string if s not in ("", "\n")]

        results = []

        for block in blocks:
            story_text = "".join(("##", block)).strip()

            # Here, we get a list of StorySteps. A StoryStep is a
            # single story block that may or may not contain
            # checkpoints. A story file contains one or more StorySteps
            steps_list = self.get_story_steps(
                story_text, data.FileFormat.MARKDOWN, domain, filename
            )

            if steps_list and steps_list[0].block_name:
                results.append((story_text, steps_list[0]))
            else:
                raise StoryParseError(
                    f"Invalid story format. Failed to parse '{story_text}'"
                )

        return results

    async def _extract_stories_yaml(
        self, story_string: Text, filename: Text, domain: Optional[Dict[Text, Any]],
    ) -> List[Tuple[Text, StoryStep]]:
        """Extract stories from the text contents of a YAML file.

        Args:
            story_string: Stories in string form (YAML format).
            filename: Name of stories file.
            domain: Stories domain.

        Returns:
            List of tuples, each tuple containing a story in string form and
            its `StoryStep` representation.
        """
        writer = YAMLStoryWriter()

        steps_list = self.get_story_steps(
            story_string, data.FileFormat.YAML, domain, filename
        )

        results = []

        for steps in steps_list:
            if not steps.block_name:
                raise StoryParseError(
                    f"Invalid story format. Failed to parse '{story_string}'"
                )

            story_text = writer.dumps([steps])
            results.append((story_text, steps))

        return results

    async def save_stories(
        self,
        story_string: Text,
        team: Text,
        project_id: Text,
        username: Text,
        filename: Optional[Text] = None,
        dump_stories: bool = True,
        add_story_items_to_domain: bool = True,
        file_format: Optional[data.FileFormat] = None,
    ) -> List[Dict[Text, Any]]:
        """Saves stories or rules from string form as individual stories or rules.

        Args:
            story_string: Stories or rules in string form.
            team: User's team.
            project_id: Project ID to assign to new data.
            username: User name.
            filename: Filename to assign to new data created.
            dump_stories: If `True`, schedule a dumping of stories/rules to
                local files.
            add_story_items_to_domain: if `True`, add new story/rule items to
                the domain.
            file_format: Corresponding file format for stories/rules string.

        Raises:
            ValueError: If the file format of `story_string` could not be deduced.

        Returns:
            List of stored stories or rules.
        """
        if not filename and not file_format:
            raise ValueError("Not enough information to determine story format.")

        from rasax.community.services.domain_service import DomainService

        domain_service = DomainService(self.session)
        domain = domain_service.get_domain(project_id)

        if filename:
            file_format = data.format_from_filename(filename)
        else:
            filename = self.assign_filename(team, file_format)

        inserted = []

        if file_format == data.FileFormat.MARKDOWN:
            processed_stories = await self._extract_stories_markdown(
                story_string, filename, domain
            )
        elif file_format == data.FileFormat.YAML:
            processed_stories = await self._extract_stories_yaml(
                story_string, filename, domain
            )
        else:
            raise ValueError(f"Unknown file format: '{file_format}'.")

        for story_text, steps in processed_stories:
            new_story = Story(
                name=steps.block_name,
                story=story_text,
                annotated_at=time.time(),
                user=username,
                filename=filename,
                is_rule=isinstance(steps, RuleStep),
            )

            self.add(new_story)

            self.flush()  # flush to get inserted story id
            if add_story_items_to_domain:
                await self.add_domain_items_for_story(
                    new_story.id, project_id, username, file_format
                )

            inserted.append(new_story.as_dict())

        if inserted:
            if dump_stories:
                background_dump_service.add_story_change(filename)
            return inserted
        else:
            return []

    def get_filenames(self, team: Text) -> List[Text]:
        """Return a list of all values of `filename`."""

        filenames = (
            self.query(Story.filename)
            .join(User)
            .filter(User.team == team)
            .distinct()
            .all()
        )
        return [f for f, in filenames]

    def assign_filename(self, team: Text, file_format: data.FileFormat) -> Text:
        """Finds the filename of the oldest document in the collection.

        Returns config.default_stories_filename if no filename was found.
        """
        oldest_file = (
            self.query(Story.filename)
            .join(User)
            .filter(User.team == team)
            .order_by(Story.id.asc())
            .first()
        )

        file_name = io_utils.get_project_directory()
        if oldest_file:
            file_name = file_name / oldest_file[0]
        else:
            file_name = (
                file_name
                / rasa_x_config.data_dir
                / rasa_x_config.default_stories_filename
            )

        return str(file_name.with_suffix(file_format.value))

    async def save_stories_from_files(
        self,
        story_files: Union[List[Text], Set[Text]],
        team: Text,
        project_id: Text,
        username: Text,
    ) -> List[Dict[Text, Any]]:
        """Save stories from `story_files` to database."""

        from rasax.community.initialise import _read_data  # pytype: disable=pyi-error

        story_blocks = []

        for text_data, path in _read_data(list(story_files)):
            logger.debug(f"Injecting stories from file '{path}' to database.")
            additional_blocks = await self.save_stories(
                text_data,
                team,
                project_id,
                username,
                path,
                dump_stories=False,
                add_story_items_to_domain=False,
            )
            story_blocks.extend(additional_blocks)

        await self.add_domain_items_for_stories(project_id, username)

        return story_blocks

    async def replace_stories(
        self,
        story_string: Text,
        team: Text,
        project_id: Text,
        username: Text,
        filename: Optional[Text] = None,
        dump_stories: bool = True,
        file_format: Optional[data.FileFormat] = None,
        replace_rules: Optional[bool] = None,
    ) -> Optional[List[Optional[Dict[Text, Any]]]]:
        """Delete all existing stories/rules and insert new ones.

        Args:
            story_string: Stories or rules in string form.
            team: User's team.
            project_id: Project ID to assign to new data.
            username: User name.
            filename: Filename to assign to new data created.
            dump_stories: If `True`, schedule a dumping of stories/rules to local files.
            file_format: Corresponding file format for stories/rules string.
            replace_rules: If `True`, remove only rules from DB before
                inserting new ones. If `False`, remove only stories.

        Returns:
            List of stored stories or rules.
        """
        self.delete_all_stories(replace_rules)

        saved_stories = await self.save_stories(
            story_string,
            team,
            project_id,
            username,
            filename,
            dump_stories,
            file_format=file_format,
        )
        return saved_stories

    def fetch_stories(
        self,
        text_query: Optional[Text] = None,
        field_query: Optional[List[Tuple[Text, bool]]] = None,
        id_query: Optional[List[int]] = None,
        filename: Optional[Text] = None,
        distinct: bool = True,
        fetch_rules: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[Text, Any]]:
        """Fetch (search) stories or rules stored in the database.

        Args:
            text_query: Text query for story/rule text.
            field_query: Field query to limit returned fields.
            id_query: If specified, only fetch stories/rules with these IDs.
            filename: Filter results by filename.
            distinct: Ensure results are unique.
            fetch_rules: If `False`, fetch stories only. If `True`, fetch rules only.
            limit: Limit number of results.
            offset: Number to offset results by.

        Returns:
            List of stories or rules in their dictionary representation.
        """
        if text_query:
            query = Story.story.like(f"%{text_query}%")
        else:
            query = True

        if id_query:
            query = and_(query, or_(Story.id == id for id in id_query))

        if filename:
            query = and_(query, Story.filename == filename)

        if fetch_rules is not None:
            query = and_(query, Story.is_rule == fetch_rules)

        columns = common_utils.get_columns_from_fields(field_query)
        stories = self.query(*common_utils.get_query_selectors(Story, columns)).filter(
            query
        )

        if distinct:
            stories = stories.distinct()

        stories = stories.order_by(Story.id.asc()).offset(offset).limit(limit).all()

        if columns:
            results = [
                common_utils.query_result_to_dict(s, field_query) for s in stories
            ]
        else:
            results = [t.as_dict() for t in stories]

        return results

    @staticmethod
    async def visualize_stories(
        stories: List[Dict[Text, Any]], domain: "Domain"
    ) -> Text:
        """Return a Graphviz visualization of one or more stories or rules.

        Args:
            stories: List of stories or rules.
            domain: Stories/rules domain.

        Returns:
            Stories/rules in Graphviz DOT format.
        """
        from networkx.drawing.nx_pydot import to_pydot
        from rasa.shared.core.training_data.visualization import visualize_stories

        parsed_story_steps = []

        for story in stories:
            steps_list = StoryService.get_story_steps(
                story["story"],
                data.format_from_filename(story["filename"]),
                domain.as_dict(),
                story["filename"],
            )
            parsed_story_steps.extend(steps_list)

        graph = await visualize_stories(
            parsed_story_steps, domain, output_file=None, max_history=2
        )

        return to_pydot(graph).to_string()

    def fetch_story(
        self, story_id: Text, fetch_rule: Optional[bool] = None
    ) -> Optional[Dict[Text, Any]]:
        """Fetch a story or a rule.

        Args:
            story_id: ID of story or rule.
            fetch_rule: If `True`, fetch only rules. If `False`, fetch only stories.

        Returns:
            Dictionary representation of a story or rule.
        """
        story = self.query(Story).filter(Story.id == story_id)

        if fetch_rule is not None:
            story = story.filter(Story.is_rule == fetch_rule)

        story = story.first()

        if story is None:
            return None

        return story.as_dict()

    def delete_story(self, _id: Text, delete_rule: Optional[bool] = None) -> bool:
        """Delete a story or a rule.

        Args:
            _id: ID of story or rule.
            delete_rule: When `True`, delete only rules. When `False`, delete
                only stories.

        Returns:
            `True` if the story or rule with given ID was deleted.
        """
        query = self.query(Story.filename).filter(Story.id == _id)

        if delete_rule is not None:
            query = query.filter(Story.is_rule == delete_rule)

        original_story_filename = query.first()

        if original_story_filename:
            original_story_filename = original_story_filename.filename

        delete_result = self.query(Story).filter(Story.id == _id).delete()

        if delete_result:
            background_dump_service.add_story_change(original_story_filename)

        return delete_result

    async def _fetch_domain_items_from_story(
        self, story_id: Text, project_id: Text, file_format: data.FileFormat
    ) -> Optional[Tuple[Set[Text], Set[Text], Set[Text], Set[Text]]]:
        from rasax.community.services.domain_service import DomainService

        domain_service = DomainService(self.session)
        domain = domain_service.get_domain(project_id)

        story = self.fetch_story(story_id)

        if not story:
            return None

        steps = self.get_story_steps(story["story"], file_format, domain)
        story_actions = set()
        story_intents = set()
        story_entities = set()
        story_slots = set()
        for step in steps:
            for e in step.events:
                if (
                    isinstance(e, ActionExecuted)
                    # exclude default actions and utter actions
                    and e.action_name
                    not in rasa.shared.core.constants.DEFAULT_ACTION_NAMES
                    and not e.action_name.startswith(rasa.shared.constants.UTTER_PREFIX)
                ):
                    story_actions.add(e.action_name)
                elif isinstance(e, UserUttered):
                    intent = e.intent
                    entities = e.entities
                    if intent:
                        story_intents.add(intent.get("name"))
                    if entities:
                        entity_names = [e["entity"] for e in entities]
                        story_entities.update(entity_names)
                elif isinstance(e, SlotSet):
                    slot = e.key
                    if slot:
                        story_slots.add(slot)

        return story_actions, story_intents, story_slots, story_entities

    async def fetch_domain_items_from_stories(
        self, project_id: Text
    ) -> Optional[Tuple[Set[Text], Set[Text], Set[Text], Set[Text]]]:
        """Fetch set of actions, intents, slots and entities from all stories.

        Returns a tuple of four sets.
        """
        stories = self.fetch_stories()

        if not stories:
            return None

        actions = set()
        intents = set()
        slots = set()
        entities = set()
        for story in stories:
            story_events = await self._fetch_domain_items_from_story(
                story["id"], project_id, data.format_from_filename(story["filename"])
            )
            actions.update(story_events[0])
            intents.update(story_events[1])
            slots.update(story_events[2])
            entities.update(story_events[3])

        return actions, intents, slots, entities

    async def add_domain_items_for_story(
        self,
        story_id: Union[int, Text],
        project_id: Text,
        username: Text,
        file_format: data.FileFormat,
    ) -> None:
        """Add story items for `story_id` to domain.

        These are actions, intents, slots and entities.
        """
        story_events = await self._fetch_domain_items_from_story(
            story_id, project_id, file_format
        )
        await self._add_story_items_to_domain(project_id, username, story_events)

    async def add_domain_items_for_stories(
        self, project_id: Text, username: Text
    ) -> None:
        """Add story items to domain for all stories in database.

        These are actions, intents, slots and entities.
        """
        story_events = await self.fetch_domain_items_from_stories(project_id)
        if story_events:
            await self._add_story_items_to_domain(project_id, username, story_events)

    async def _add_story_items_to_domain(
        self,
        project_id: Text,
        username: Text,
        story_events: Tuple[Set[Text], Set[Text], Set[Text], Set[Text]],
    ):
        from rasax.community.services.domain_service import DomainService

        domain_service = DomainService(self.session)
        domain_service.add_items_to_domain(
            project_id,
            username,
            actions=story_events[0],
            intents=story_events[1],
            slots=story_events[2],
            entities=story_events[3],
            dump_data=False,
            origin="stories",
        )

    async def update_story(
        self,
        story_id: Text,
        story_string: Text,
        project_id: Text,
        user: Dict[Text, Any],
        file_format: data.FileFormat,
        update_rule: Optional[bool] = None,
    ) -> Optional[Dict[Text, Any]]:
        """Update properties of a story or a rule.

        Args:
            story_id: ID of story or rule.
            story_string: String contents of story or rule to set.
            project_id: Filter by project ID.
            user: User modifying the story or rule.
            file_format: File format for input story/rule string.
            update_rule: If `True`, only search and update rules. If `False`,
                only search and update stories.

        Raises:
            ValueError: If caller attempted to update the contents of a story
                with a rule or vice-versa.

        Returns:
            Updated story/rule in dictionary form, if it was found. Otherwise,
            returns `None`.
        """
        from rasax.community.services.domain_service import DomainService

        domain_service = DomainService(self.session)
        domain = domain_service.get_domain(project_id)

        story_steps = self.get_story_steps(story_string, file_format, domain)
        if not story_steps:
            return None

        story = self.query(Story).filter(Story.id == story_id)
        if update_rule is not None:
            story = story.filter(Story.is_rule == update_rule)

        story = story.first()

        if not story:
            return None

        if story.is_rule != isinstance(story_steps[0], RuleStep):
            raise ValueError(
                "Can't replace contents of a story with a rule, or vice-versa."
            )

        story.user = user[constants.USERNAME_KEY]
        story.annotated_at = time.time()
        story.name = story_steps[0].block_name
        story.story = story_string.strip()

        # Change filename extension, but keep name the same.
        story.filename = str(Path(story.filename).with_suffix(file_format.value))

        background_dump_service.add_story_change(story.filename)

        await self.add_domain_items_for_story(
            story_id, project_id, story.user, file_format
        )

        return story.as_dict()

    def dump_stories_to_file_system(
        self, original_story_filename: Text, project_id: Optional[Text] = None
    ) -> None:
        """Dump Rasa Core stories in database to file."""

        if not original_story_filename:
            logger.error("Failed to dump stories to the file: original file not found")

        logger.debug(f"Dumping stories to file '{original_story_filename}'.")
        stories = self.fetch_stories(None, filename=original_story_filename)

        text = self.get_stories_as_string(
            project_id, stories, data.format_from_filename(original_story_filename)
        )

        io_utils.write_file(original_story_filename, text)

    def delete_all_stories(self, delete_rules: Optional[bool] = None) -> None:
        """Deletes all stories and/or rules.

        Args:
            delete_rules: If `True`, delete only rules. If `False`, delete only stories.
        """
        query = self.query(Story)

        if delete_rules is not None:
            query = query.filter(Story.is_rule == delete_rules)

        query.delete()

    @staticmethod
    def from_request(
        request: "Request", _other_service: "DbService" = None
    ) -> "StoryService":
        """Creates a `StoryService` from an incoming HTTP request's DB session.

        Args:
            request: Incoming HTTP request.
            _other_service: Unused.

        Returns:
            `StoryService` instance with a connection to the DB.
        """
        return StoryService(request[constants.REQUEST_DB_SESSION_KEY])
