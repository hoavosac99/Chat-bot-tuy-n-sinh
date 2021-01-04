# -*- coding: utf-8 -*-
from setuptools import setup

packages = \
['rasax',
 'rasax.community',
 'rasax.community.api',
 'rasax.community.api.blueprints',
 'rasax.community.api.blueprints.conversations',
 'rasax.community.api.blueprints.core',
 'rasax.community.api.blueprints.nlu',
 'rasax.community.database',
 'rasax.community.database.events',
 'rasax.community.database.schema_migrations',
 'rasax.community.database.schema_migrations.alembic',
 'rasax.community.database.schema_migrations.alembic.versions',
 'rasax.community.services',
 'rasax.community.services.event_consumers',
 'rasax.community.services.integrated_version_control',
 'rasax.community.utils']

package_data = \
{'': ['*'], 'rasax.community': ['interface/NOTE']}

install_requires = \
['GitPython>=3.1.3,<4.0.0',
 'SQLAlchemy>=1.3.19,<2.0.0',
 'aiohttp>=3.6,<4.0',
 'alembic>=1.0.10,<2.0.0',
 'apscheduler>=3.6,<4.0',
 'attrs>=19.3,<20.0',
 'cryptography>=2.7,<3.0',
 'isodate>=0.6,<0.7',
 'jsonschema>=3.2,<4.0',
 'kafka-python>=1.4,<2.0',
 'pika>=1.1.0,<2.0.0',
 'psycopg2-binary>=2.8.2,<3.0.0',
 'python-dateutil>=2.8,<2.9',
 'questionary>=1.5.1,<1.6.0',
 'rasa==2.0.0rc3',
 'requests>=2.23,<3.0',
 'ruamel.yaml>=0.16,<0.17',
 'sanic-cors>=0.10.0b1,<0.11.0',
 'sanic-jwt>=1.3.2,<1.4.0',
 'sanic>=19.12.2,<20.0.0',
 'setuptools>=41.0.0',
 'ujson>=1.35,<2.0']

setup_kwargs = {
    'name': 'rasa-x',
    'version': '0.33.0rc1',
    'description': 'Machine learning framework to automate text- and voice-based conversations: NLU, dialogue management, connect to Slack, Facebook, and more - Create chatbots and voice assistants',
    'long_description': '# Rasa X\n\nRasa X is a freely available, closed source project.\n',
    'author': 'Rasa Technologies GmbH',
    'author_email': 'hi@rasa.com',
    'maintainer': 'Tom Bocklisch',
    'maintainer_email': 'tom@rasa.com',
    'url': 'https://rasa.com',
    'packages': packages,
    'package_data': package_data,
    'install_requires': install_requires,
    'python_requires': '>=3.6,<3.9',
}


setup(**setup_kwargs)
