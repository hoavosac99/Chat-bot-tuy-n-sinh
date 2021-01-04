import json
import logging
from typing import Dict, Text, Any, Optional, Union, List

import typing
from ruamel.yaml.compat import ordereddict
from sanic.request import Request
from sqlalchemy import and_

import rasax.community.config as rasa_x_config
import rasax.community.constants as constants
import rasax.community.utils.config as config_utils
import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
import rasax.community.utils.yaml as yaml_utils
from rasax.community.database.admin import Environment, Project, LocalPassword
from rasax.community.database.service import DbService
from rasax.community.services import background_dump_service
from rasax.community.services.data_service import DataService
from rasax.community.services.domain_service import DomainService
from rasax.community.services.story_service import StoryService

if typing.TYPE_CHECKING:
    from rasax.community.services.stack_service import StackService

logger = logging.getLogger(__name__)


def default_environments_config_local(rasa_port: Union[int, Text]) -> Dict[Text, Any]:
    stack_config = ordereddict(
        [
            (
                constants.RASA_PRODUCTION_ENVIRONMENT,
                ordereddict(
                    [
                        ("url", f"http://localhost:{rasa_port}"),
                        ("token", rasa_x_config.rasa_token),
                    ]
                ),
            ),
            (
                constants.RASA_DEVELOPMENT_ENVIRONMENT,
                ordereddict(
                    [
                        ("url", f"http://stuff:{rasa_port}"),
                        ("token", rasa_x_config.rasa_token),
                    ]
                ),
            ),
            (
                constants.RASA_WORKER_ENVIRONMENT,
                ordereddict(
                    [
                        ("url", f"http://localhost:{rasa_port}"),
                        ("token", rasa_x_config.rasa_token),
                    ]
                ),
            ),
        ]
    )

    return {"environments": ordereddict([("rasa", stack_config)])}


def default_stack_config() -> Dict[Text, Union[Text, List[Dict[Text, Text]]]]:
    # TODO: Find a way to keep this in sync with the defaults that Rasa OSS
    # chooses?

    pipeline = [
        {"name": "WhitespaceTokenizer"},
        {"name": "RegexFeaturizer"},
        {"name": "LexicalSyntacticFeaturizer"},
        {"name": "CountVectorsFeaturizer"},
        {
            "name": "CountVectorsFeaturizer",
            "analyzer": "char_wb",
            "min_ngram": 1,
            "max_ngram": 4,
        },
        {"name": "DIETClassifier", "epochs": 100},
        {"name": "EntitySynonymMapper"},
        {"name": "ResponseSelector", "epochs": 100},
    ]

    policies = [
        {"name": "MemoizationPolicy"},
        {"name": "TEDPolicy", "max_history": 5, "epochs": 100},
        {"name": "RulePolicy"},
    ]

    return ordereddict(
        [("language", "en"), ("pipeline", pipeline), ("policies", policies)]
    )


class ProjectException(Exception):
    """Exception raised for errors related to projects."""

    def __init__(self, username: Text):
        self.message = username

    def __str__(self) -> Text:
        return self.message


class SettingsService(DbService):
    @staticmethod
    def from_request(
        request: Request, other_service: "DbService" = None
    ) -> "SettingsService":
        """Constructs Service object from the incoming request"""
        return SettingsService(request[constants.REQUEST_DB_SESSION_KEY])

    def init_project(self, team: Text, project_id: Text):
        """Create project for `project_id`.

        Raise `ProjectException` if a project under that name already exists.
        """

        if self.get(team, project_id):
            raise ProjectException(f"Project '{project_id}' already exists.")

        new_project = Project(
            project_id=project_id, team=team, config=json.dumps(default_stack_config())
        )
        self.add(new_project)

        return new_project.as_dict()

    def get(
        self, team: Text, project_id: Text
    ) -> Optional[Dict[Text, Union[Text, Dict]]]:
        project = self._get_project(team, project_id)

        if project:
            return project.as_dict()

    def get_config(
        self, team: Text, project_id: Text
    ) -> Optional[Dict[Text, Union[Text, Dict]]]:
        """Get model config associated with project."""

        project = self._get_project(team, project_id)

        if project:
            return project.get_model_config()

    def _get_project(self, team: Text, project_id: Text) -> Optional[Project]:
        return (
            self.query(Project)
            .filter(and_(Project.project_id == project_id, Project.team == team))
            .first()
        )

    def dump_config(
        self, team: Text, project_id: Text, filename: Optional[Text] = None
    ) -> None:
        """Dump domain to `filename` in yml format.

        Args:
            team: Team whose config should be dumped.
            project_id: Project whose config should be dumped.
            filename: Name of the file the config should be dumped into.
        """

        project = self._get_project(team, project_id)
        if project:
            if not filename:
                default_path = str(
                    io_utils.get_project_directory() / rasa_x_config.default_config_path
                )
                filename = json.loads(project.config).get("path") or default_path
            yaml_utils.dump_yaml_to_file(
                filename=filename, content=project.get_model_config()
            )

    def save_config(
        self,
        team: Text,
        project_id: Text,
        stack_config: Dict,
        config_path: Optional[Text] = None,
        should_dump: bool = True,
    ) -> None:
        logger.debug(stack_config)

        if config_path:
            stack_config["path"] = config_path

        project = self._get_project(team, project_id)
        project.config = json.dumps(stack_config)

        if should_dump:
            background_dump_service.add_model_configuration_change(team, project_id)

    @staticmethod
    def _inspect_config(stack_config: Dict) -> None:
        """Confirm Rasa config has all the mandatory keys."""

        missing_keys = [
            k
            for k in constants.RASA_CONFIG_MANDATORY_KEYS
            if k not in stack_config or stack_config[k] is None
        ]
        if missing_keys:
            raise config_utils.InvalidConfigError(
                "The config file is missing mandatory parameters: "
                "'{}'. Add missing parameters to config file and try again."
                "".format("', '".join(missing_keys))
            )

    def inspect_and_load_yaml_config(self, config_yaml: Text) -> Dict:
        stack_config = yaml_utils.load_yaml(config_yaml)
        self._inspect_config(stack_config)

        return stack_config

    def inspect_and_save_yaml_config_from_request(
        self, request_body: bytes, team: Text, project_id: Text
    ) -> Text:
        """Inspect and save yaml config from `request_body`."""

        config_yaml = io_utils.convert_bytes_to_string(request_body)
        stack_config = self.inspect_and_load_yaml_config(config_yaml)

        self.save_config(team, project_id, stack_config)

        return config_yaml

    def inject_environments_config_from_file(self, project_id: Text, filename: Text):
        """Inject a deployment environments configuration file at `filename`
        into the db.
        """

        try:
            _config = yaml_utils.read_yaml_file(filename)
            self.inspect_environments_config(_config)
            self.save_environments_config(project_id, _config)
            logger.debug(
                "Successfully injected deployment environments "
                "configuration from file '{}'.".format(filename)
            )
        except (ValueError, FileNotFoundError) as e:
            current_config = self.get_environments_config(project_id)
            logger.warning(
                "Could not inject deployment environments "
                "configuration from file '{}'. Details:\n{}\nYou may "
                "still use Rasa X if you currently have a "
                "working configuration. Your current configuration "
                "is:\n{}".format(filename, e, json.dumps(current_config, indent=2))
            )

    @staticmethod
    def inspect_environments_config(env_config: Dict[Text, Any]) -> None:
        """Inspect deployment environments config.

        Raise ValueError if config does not contain entries for worker or
        production services. Raise ValueError if services other than production
        or worker are added and Rasa Enterprise is not installed.
        """

        rasa_config = env_config.get("rasa")

        if not rasa_config:
            raise ValueError("Environment config needs to have key `rasa`.")

        if (
            constants.RASA_PRODUCTION_ENVIRONMENT not in rasa_config
            or constants.RASA_WORKER_ENVIRONMENT not in rasa_config
        ):
            raise ValueError(
                "Environment needs to contain entries for production "
                "and worker services."
            )

        for k, v in rasa_config.items():
            if k in (
                constants.RASA_PRODUCTION_ENVIRONMENT,
                constants.RASA_WORKER_ENVIRONMENT,
            ):
                continue
            elif not common_utils.is_enterprise_installed():
                raise ValueError(
                    f"Rasa X only allows for a production environment and "
                    f"a worker environment. To use more environments, please "
                    f"contact us at {constants.HI_RASA_EMAIL} for a "
                    f"Rasa Enterprise license."
                )

    def save_environments_config(
        self, project_id: Text, environments_config: Dict[Text, Any]
    ) -> Dict[Text, Dict[Text, Dict[Text, Text]]]:
        for name, env_config in environments_config.get("rasa", {}).items():
            # delete possibly existing config
            self.delete_environment_config(name, project_id)

            env = Environment(
                name=name,
                project=project_id,
                url=env_config["url"],
                token=env_config["token"],
            )
            self.add(env)

        return self.get_environments_config(project_id)

    def delete_environment_config(self, name: Text, project_id: Text) -> None:
        to_delete = (
            self.query(Environment)
            .filter(Environment.name == name)
            .filter(Environment.project == project_id)
            .first()
        )
        if to_delete:
            self.delete(to_delete)

    def get_environments_config(self, project_id: Text) -> Dict[Text, Any]:
        envs = self.query(Environment).filter(Environment.project == project_id).all()
        envs = [e.as_dict() for e in envs]

        return {"environments": {"rasa": {list(e)[0]: e[list(e)[0]] for e in envs}}}

    def get_stack_service(
        self, environment: Text, project_id: Text = rasa_x_config.project_name
    ) -> Optional["StackService"]:
        return self.stack_services(project_id).get(environment, None)

    def stack_services(
        self, project_id: Text = rasa_x_config.project_name
    ) -> Dict[Text, "StackService"]:
        """Create StackServices for all Stack servers."""
        from rasax.community.services.stack_service import RasaCredentials
        from rasax.community.services.stack_service import StackService

        environments_config = self.get_environments_config(project_id).get(
            "environments", {}
        )

        _stack_services = {}
        for k, v in environments_config.get("rasa", {}).items():
            credentials = RasaCredentials(url=v["url"], token=v.get("token"))
            _stack_services[k] = StackService(
                credentials,
                DataService(self.session),
                StoryService(self.session),
                DomainService(self.session),
                self,
            )

        return _stack_services

    def save_community_user_password(self, password: Text) -> None:
        """Save Rasa X password in local mode.

        Overwrite existing local password, or create a new one if none is found.
        """

        local_password = self.query(LocalPassword).first()
        if local_password:
            local_password.password = password
        else:
            local_password = LocalPassword(password=password)
            self.add(local_password)

    def get_community_user_password(self) -> Optional[Text]:
        """Fetch Rasa X password in local mode."""

        local_password = self.query(LocalPassword).first()
        if local_password:
            return local_password.password

        return None
