#!/usr/bin/env python3

import os
import sys
import gatt
from argparse import ArgumentParser
from wahoo_controller import WahooController
 
root_folder = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(root_folder)

# define CLI parse arguments
parser = ArgumentParser(description="Wahoo Device Controller")

# BLE connection params
parser.add_argument('--mac_address', dest='mac_address', type=str, help="The Wahoo Kickr BLE Device's unique mac address", default=os.getenv('KICKR_MAC_ADDRESS'))

# MQTT connection params
parser.add_argument('--broker_address', dest='broker_address', type=str, help='The MQTT broker address', default=os.getenv('MQTT_HOSTNAME'))
parser.add_argument('--username', dest='username', type=str, help='username', default=os.getenv('MQTT_USERNAME'))
parser.add_argument('--password', dest='password', type=str, help='password', default=os.getenv('MQTT_PASSWORD'))
parser.add_argument('--port', dest='port', type=int, help='port', default=os.getenv('MQTT_PORT'))

DEVICE_ID = os.getenv('DEVICE_ID')

parser.add_argument('--incline_command_topic', dest='incline_command_topic', type=str, help='a MQTT topic that will send incline control commands to this driver', default=f'bike/{DEVICE_ID}/incline/control')
parser.add_argument('--incline_report_topic', dest='incline_report_topic', type=str, help='a MQTT topic that will receieve the current incline levels data from this driver', default=f'bike/{DEVICE_ID}/incline/report')
parser.add_argument('--resistance_command_topic', dest='resistance_command_topic', type=str, help='a MQTT topic that will send resistance control commands to this driver', default=f'bike/{DEVICE_ID}/resistance/control')
parser.add_argument('--resistance_report_topic', dest='resistance_report_topic', type=str, help='a MQTT topic that will receieve the current resistance levels data from this driver', default=f'bike/{DEVICE_ID}/resistance/report')
parser.add_argument('--fan_command_topic', dest='fan_command_topic', type=str, help='a MQTT topic that will send fan control commands to this driver', default=f'bike/{DEVICE_ID}/fan/control')
parser.add_argument('--fan_report_topic', dest='fan_report_topic', type=str, help='a MQTT topic that will receieve the current incline or resistance levels data from this driver', default=f'bike/{DEVICE_ID}/fan/report')
parser.add_argument('--speed_report_topic', dest='speed_report_topic', type=str, help='a MQTT topic that will receive the current instantaneous speed data in m/s from this driver', default=f'bike/{DEVICE_ID}/speed')
parser.add_argument('--cadence_report_topic', dest='cadence_report_topic', type=str, help='a MQTT topic that will receive the current instantaneous cadence data in rpm from this driver', default=f'bike/{DEVICE_ID}/cadence')
parser.add_argument('--power_report_topic', dest='power_report_topic', type=str, help='a MQTT topic that will receive the current instantaneous power data in W from this driver', default=f'bike/{DEVICE_ID}/power')

args = parser.parse_args()

print("Connecting to the BLE device...")
manager = gatt.DeviceManager(adapter_name='hci0')

# initiate and connect a WahooDevice with a given BLE mac_address
device = WahooController(manager=manager, mac_address=args.mac_address, args=args)
device.connect()

try:
    print("Running the device manager now...")
    # run the device manager in the main thread forever
    manager.run()
except KeyboardInterrupt:
    print ('Exit the program.')
    manager.stop()
    sys.exit(0)
