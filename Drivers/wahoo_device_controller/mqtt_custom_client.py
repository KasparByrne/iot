#!/usr/bin/env python3

import re
import os
import sys

root_folder = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(root_folder)

from lib.mqtt_client import MQTTClient
from lib.constants import RESISTANCE_MIN, RESISTANCE_MAX, INCLINE_MIN, INCLINE_MAX

# define a custom MQTT Client to be able send BLE FTMS commands while receiving command messages from a MQTT command topic
class MQTTClientWithSendingFTMSCommands(MQTTClient):
    def __init__(self, broker_address, username, password, device):
        super().__init__(broker_address, username, password)
        self.device = device

    def on_message(self, client, userdata, msg):
        super().on_message(self,client,userdata, msg)
        self.device.on_message(msg)