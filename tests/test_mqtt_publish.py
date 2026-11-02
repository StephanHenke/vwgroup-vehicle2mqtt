import unittest
from unittest.mock import MagicMock

from vw_euda_mqtt.config import MqttConfig
from vw_euda_mqtt.mqtt import MqttPublisher


class PublishAcknowledgementTests(unittest.TestCase):
    def test_publish_timeout_is_not_success(self):
        publisher = MqttPublisher(MqttConfig(host="mqtt.example.local"))
        publisher.client = MagicMock()
        info = publisher.client.publish.return_value
        info.rc = 0
        info.is_published.return_value = False
        with self.assertRaises(RuntimeError):
            publisher.publish("test/battery/soc", 53)
        info.wait_for_publish.assert_called_once_with(timeout=10)
