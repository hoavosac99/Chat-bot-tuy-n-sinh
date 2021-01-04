import logging
import typing
from typing import Any, Text, List, Optional, Union, Dict

import time

import rasax.community.config as rasa_x_config
from rasax.community.services.event_consumers.event_consumer import EventConsumer

if typing.TYPE_CHECKING:
    from kafka.structs import TopicPartition
    from kafka.consumer.fetcher import ConsumerRecord
    from kafka import KafkaConsumer

logger = logging.getLogger(__name__)


class KafkaEventConsumer(EventConsumer):
    type_name = "kafka"

    def __init__(
        self,
        url: Union[Text, List[Text]],
        topic: Text,
        client_id: Optional[Text] = None,
        group_id: Optional[Text] = None,
        security_protocol: Text = "PLAINTEXT",
        sasl_username: Union[Text, int, None] = None,
        sasl_password: Optional[Text] = None,
        ssl_cafile: Optional[Text] = None,
        ssl_certfile: Optional[Text] = None,
        ssl_keyfile: Optional[Text] = None,
        ssl_check_hostname: bool = False,
        should_run_liveness_endpoint: bool = False,
        **kwargs: Any,
    ):
        """Kafka event consumer.

        Args:
            url: 'host[:port]' string (or list of 'host[:port]'
                strings) that the consumer should contact to bootstrap initial
                cluster metadata. This does not have to be the full node list.
                It just needs to have at least one broker that will respond to a
                Metadata API Request. The default port is 9092. If no servers are
                specified, it will default to `localhost:9092`.
            topic: Topics to subscribe to. If not set, call subscribe() or assign()
                before consuming records
            client_id: A name for this client. This string is passed in each request
                to servers and can be used to identify specific server-side log entries
                that correspond to this client. Also submitted to `GroupCoordinator` for
                logging with respect to consumer group administration.
                Default: ‘kafka-python-{version}’
            group_id: The name of the consumer group to join for dynamic partition
                assignment (if enabled), and to use for fetching and committing offsets.
                If None, auto-partition assignment (via group coordinator) and offset
                commits are disabled. Default: None
            sasl_username: Username for sasl PLAIN authentication.
                Required if `sasl_mechanism` is `PLAIN`.
            sasl_password: Password for sasl PLAIN authentication.
                Required if `sasl_mechanism` is PLAIN.
            ssl_cafile: Optional filename of ca file to use in certificate
                verification. Default: None.
            ssl_certfile: Optional filename of file in pem format containing
                the client certificate, as well as any ca certificates needed to
                establish the certificate's authenticity. Default: None.
            ssl_keyfile: Optional filename containing the client private key.
                Default: None.
            ssl_check_hostname: Flag to configure whether ssl handshake
                should verify that the certificate matches the brokers hostname.
                Default: False.
            security_protocol: Protocol used to communicate with brokers.
                Valid values are: PLAINTEXT, SSL, SASL_PLAINTEXT, SASL_SSL.
                Default: PLAINTEXT.
            should_run_liveness_endpoint: If `True`, runs a simple Sanic server as a
                background process that can be used to probe liveness of this service.
                The service will be exposed at a port defined by the
                `SELF_PORT` environment variable (5673 by default).

        """
        self.url = url
        self.topic = topic
        self.client_id = client_id
        self.group_id = group_id
        self.security_protocol = security_protocol
        self.sasl_username = sasl_username
        self.sasl_password = sasl_password
        self.ssl_cafile = ssl_cafile
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_check_hostname = ssl_check_hostname
        self.consumer: Optional["KafkaConsumer"] = None
        super().__init__(should_run_liveness_endpoint)

    @classmethod
    def from_endpoint_config(
        cls,
        consumer_config: Optional[Dict],
        should_run_liveness_endpoint: bool = not rasa_x_config.LOCAL_MODE,
    ) -> Optional["KafkaEventConsumer"]:
        if consumer_config is None:
            logger.debug(
                "Could not initialise `KafkaEventConsumer` from endpoint config."
            )
            return None

        return cls(
            **consumer_config,
            should_run_liveness_endpoint=should_run_liveness_endpoint,
        )

    def _create_consumer(self) -> None:
        # noinspection PyPackageRequirements
        import kafka

        if self.security_protocol.upper() == "PLAINTEXT":
            self.consumer = kafka.KafkaConsumer(
                self.topic,
                bootstrap_servers=self.url,
                client_id=self.client_id,
                group_id=self.group_id,
                security_protocol="PLAINTEXT",
                ssl_check_hostname=False,
            )
        elif self.security_protocol.upper() == "SASL_PLAINTEXT":
            self.consumer = kafka.KafkaConsumer(
                self.topic,
                bootstrap_servers=self.url,
                client_id=self.client_id,
                group_id=self.group_id,
                security_protocol="SASL_PLAINTEXT",
                sasl_mechanism="PLAIN",
                sasl_plain_username=self.sasl_username,
                sasl_plain_password=self.sasl_password,
                ssl_check_hostname=False,
            )
        elif self.security_protocol.upper() == "SSL":
            self.consumer = kafka.KafkaConsumer(
                self.topic,
                bootstrap_servers=self.url,
                client_id=self.client_id,
                group_id=self.group_id,
                security_protocol="SSL",
                ssl_cafile=self.ssl_cafile,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                ssl_check_hostname=self.ssl_check_hostname,
            )
        elif self.security_protocol.upper() == "SASL_SSL":
            self.consumer = kafka.KafkaConsumer(
                self.topic,
                bootstrap_servers=self.url,
                client_id=self.client_id,
                group_id=self.group_id,
                security_protocol="SASL_SSL",
                sasl_mechanism="PLAIN",
                sasl_plain_username=self.sasl_username,
                sasl_plain_password=self.sasl_password,
                ssl_cafile=self.ssl_cafile,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                ssl_check_hostname=self.ssl_check_hostname,
            )

        else:
            raise ValueError(
                f"Cannot initialise `kafka.KafkaConsumer` "
                f"with security protocol '{self.security_protocol}'."
            )

    def consume(self):
        self._create_consumer()
        logger.info(f"Start consuming topic '{self.topic}' on Kafka url '{self.url}'.")
        while True:
            records: Dict[
                "TopicPartition", List["ConsumerRecord"]
            ] = self.consumer.poll()

            # records contain only one topic, so we can just get all values
            for messages in records.values():
                for message in messages:
                    self.log_event(message.value)

            time.sleep(0.01)
