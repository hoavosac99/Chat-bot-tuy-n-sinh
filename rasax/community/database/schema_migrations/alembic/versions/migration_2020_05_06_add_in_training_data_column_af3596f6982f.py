"""Add the column `in_training_data` to the table `message_log`.

Reason:
Old state: We used a subquery to determine which messages in the NLU inbox were already
in the training data. The more logs we had, the more expensive this subquery got.

New state: By adding a column `in_training_data`, we can omit the subquery and simply
check the boolean value of the column. This column is bulk updated whenever we insert
training data. Furthermore, the field is updated whenever a training data example
is saved or deleted.

Revision ID: af3596f6982f
Revises: ac3fba1c2b86

"""

from alembic import op
import sqlalchemy as sa

import rasax.community.database.schema_migrations.alembic.utils as migration_utils

from sqlalchemy.orm import Session

# revision identifiers, used by Alembic.
revision = "af3596f6982f"
down_revision = "ac3fba1c2b86"
branch_labels = None
depends_on = None

COLUMN_NAME = "in_training_data"
TABLE_NAME = "message_log"

NEW_INDEX_NAME = "message_log_hash_idx"

# Index to speed up querying suggestions
SUGGESTION_INDEX_NAME = "message_log_suggestion_idx"


def upgrade():
    _create_in_training_data_column()
    _initialise_in_training_data_column()

    _create_index_on_log_hash()
    _create_index_on_suggestion_columns()


def _create_in_training_data_column() -> None:
    migration_utils.create_column(
        "message_log", sa.Column(COLUMN_NAME, sa.Boolean(), default=False)
    )


def _initialise_in_training_data_column() -> None:
    from rasax.community.services.logs_service import LogsService

    bind = op.get_bind()
    session = Session(bind=bind)

    # Use reflected `Table`s since ORM might be ahead of current database state
    message_log = migration_utils.get_reflected_table(TABLE_NAME, session)
    nlu_training_data = migration_utils.get_reflected_table(
        "nlu_training_data", session
    )
    LogsService(session).bulk_update_in_training_data_column(
        message_log, nlu_training_data
    )


def _create_index_on_log_hash() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.create_index(NEW_INDEX_NAME, ["hash"])


def _create_index_on_suggestion_columns() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.create_index(SUGGESTION_INDEX_NAME, ["archived", "in_training_data"])


def downgrade():
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_index(NEW_INDEX_NAME)
        batch_op.drop_index(SUGGESTION_INDEX_NAME)

    migration_utils.drop_column(TABLE_NAME, COLUMN_NAME)
