import os


# URL to Rasa license agreement
RASA_TERMS_URL = (
    "https://storage.cloud.google.com/rasa-x-releases/"
    "rasa_x_ce_license_agreement.pdf"
)

WELCOME_PAGE_URL = "https://rasa.com/product/welcome"

# Key in global config file which contains whether the user agreed to the Rasa license
CONFIG_FILE_TERMS_KEY = "terms_accepted"

# Key in global config file which contains whether the user agreed to tracking
CONFIG_FILE_TELEMETRY_KEY = "metrics"
CONFIG_TELEMETRY_ID = "rasa_user_id"
CONFIG_TELEMETRY_ENABLED = "enabled"
CONFIG_TELEMETRY_DATE = "date"
CONFIG_TELEMETRY_WELCOME_SHOWN = "welcome_shown"

# Names of env variables that can be used to create an initial Rasa X user
ENV_RASA_X_USERNAME = "RASA_X_USERNAME"
ENV_RASA_X_PASSWORD = "RASA_X_PASSWORD"

API_URL_PREFIX = "/api"

COMMUNITY_PROJECT_NAME = "default"
COMMUNITY_TEAM_NAME = "rasa"
COMMUNITY_USERNAME = "me"

DEFAULT_CHANNEL_NAME = "rasa"
SHARE_YOUR_BOT_CHANNEL_NAME = "Tester"

JWT_METHOD = "RS256"

REQUEST_DB_SESSION_KEY = "db_session"

DEFAULT_GIT_REPOSITORY_DIRECTORY = "/app/git"

EVENT_CONSUMER_SEPARATION_ENV = "RUN_EVENT_CONSUMER_AS_SEPARATE_SERVICE"

DATABASE_MIGRATION_SEPARATION_ENV = "RUN_DATABASE_MIGRATION_AS_SEPARATE_SERVICE"

RASA_PRODUCTION_ENVIRONMENT = "production"

DEFAULT_RASA_ENVIRONMENT = RASA_PRODUCTION_ENVIRONMENT

RASA_WORKER_ENVIRONMENT = "worker"

RASA_DEVELOPMENT_ENVIRONMENT = "development"

USERNAME_KEY = "username"

TEAM_KEY = "team"

ROLES_KEY = "roles"

RASA_X_DOCKERHUB_TAGS_URL = "https://hub.docker.com/v2/repositories/rasa/rasa-x/tags"
RASA_X_CHANGELOG = "https://rasa.com/docs/rasa-x/changelog/rasa-x-changelog/"

RESPONSE_NAME_KEY = "response_name"

# environment variable defining the SQL database URL
ENV_DB_URL = "DB_URL"

INVALID_RASA_VERSION = "0.0.0"

UNAVAILABLE_MODEL_NAME = "unavailable"

CONVERSATION_STATUS_UNREAD = "unread"

CONVERSATION_STATUS_REVIEWED = "reviewed"

CONVERSATION_STATUS_SAVED_FOR_LATER = "saved_for_later"

HI_RASA_EMAIL = "hi@rasa.com"

DEFAULT_REQUEST_TIMEOUT = 60 * 5  # 5 minutes

# rasa config constants
DEFAULT_RASA_DATA_PATH = "data"
DEFAULT_RASA_DOMAIN_PATH = "domain.yml"
DEFAULT_RASA_CONFIG_PATH = "config.yml"
RASA_CONFIG_MANDATORY_KEYS = ["policies", "pipeline", "language"]
GLOBAL_USER_RASA_CONFIG_PATH = os.path.expanduser("~/.config/rasa/global.yml")
