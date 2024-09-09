from pickle import TRUE
import re
import os
import sys
import gatt
import platform
import json
import time
from time import sleep
from mqtt_custom_client import MQTTClientWithSendingFTMSCommands
import threading
import logging

root_folder = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(root_folder)

from lib.ble_helper import convert_incline_to_op_value, service_or_characteristic_found, service_or_characteristic_found_full_match, decode_int_bytes, covert_negative_value_to_valid_bytes
from lib.constants import RESISTANCE_MIN, RESISTANCE_MAX, INCLINE_MIN, INCLINE_MAX, FAN_MIN, FAN_MAX, FTMS_UUID, FTMS_CONTROL_POINT_UUID, FTMS_REQUEST_CONTROL, FTMS_RESET, FTMS_SET_TARGET_RESISTANCE_LEVEL, INCLINE_REQUEST_CONTROL, INCLINE_CONTROL_OP_CODE, INCLINE_CONTROL_SERVICE_UUID, INCLINE_CONTROL_CHARACTERISTIC_UUID, INDOOR_BIKE_DATA_UUID, DEVICE_UNIT_NAMES, FTMS_SET_TARGET_FAN_SPEED

"""
TODO design testing of changes for
    - Test logger
    - Test FTMS connect
    - Test climb

TODO complete first stage code for testing
    - Complete WahooController essential code
        0 FTMS control point request
        0 FTMS reset [Check if this is necessary]
        0 services_resolved
    - Complete WahooController and Climb compatibility
    - Add thread LOCK to Climb

TODO complete wahoo_device.py equivalence
    - Resistance compatibility
    - WahooData

TODO merge other Wahoo devices
    - Fan
    - Add heart monitor data report to WahooData
"""

# setup logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

logger_formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')

logger_file_handler = logging.FileHandler('wahoo.log') # TODO: setup a logging folder and write all logging files to that folder
logger_file_handler.setFormatter(logger_formatter)

logger_stream_handler = logging.StreamHandler() # this will print all logs to the terminal also

logger.addHandler(logger_file_handler)
logger.addHandler(logger_stream_handler)

# define Control Point response type constants
WRITE_SUCCESS, WRITE_FAIL, NOTIFICATION_SUCCESS, NOTIFICATION_FAIL = range(4)

# ======== GATT Device Manager Class ========

# FIXME: make this compatible with all devices
# TODO: find a better method - maybe just note the MAC address (dah)
#       This method requires initialising the device inside the manager which seems to be an unintended use for the manager
#       Just create some MAC address discovery script instead and the MAC addresses can be hardcoded.
"""class WahooManager(gatt.DeviceManager):
    # Subclass gatt.DeviceManager to allow discovery only of HEADWIND devices
    # When the alias begins with the required prefix, connect to the device
    def device_discovered(self, device):
        alias = device.alias()
        if alias is not None and self.prefix is not None and len(alias) >= len(self.prefix) and alias[0:len(self.prefix)] == self.prefix:
            #print("[%s] Discovered, alias = %s" % (device.mac_address, device.alias()))
            device = AnyDevice(mac_address=device.mac_address, manager=self)
            device.connect()
            device.zero_limit = 10
            device.zeroCount = 0
            self.stop_discovery()"""

# ======== GATT Interface Class ========

class GATTInterface(gatt.Device):
    """This class should handle GATT functionality, including:
    * Connection
    * Response logging
    """

    def __init__(self, mac_address, manager, args, managed=True):
        super().__init__(mac_address, manager, managed)

        # Fitness Machine Service device & control point
        self.ftms = None
        self.ftms_control_point = None

        # bike data characteristic
        self.indoor_bike_data = None

        # CLI parser arguments
        self.args = args

    def set_service_or_characteristic(self, service_or_characteristic):

        # find services & characteristics of the KICKR trainer
        if service_or_characteristic_found(FTMS_UUID, service_or_characteristic.uuid):
            self.ftms = service_or_characteristic
            logger.info('FTMS service found')
        elif service_or_characteristic_found(FTMS_CONTROL_POINT_UUID, service_or_characteristic.uuid):
            self.ftms_control_point = service_or_characteristic
            logger.info('CONTROL POINT characteristic found')
        elif service_or_characteristic_found(INDOOR_BIKE_DATA_UUID, service_or_characteristic.uuid):
            self.indoor_bike_data = service_or_characteristic
            logger.info('INDOOR BIKE DATA characteristic found')
            self.indoor_bike_data.enable_notifications()

    # ====== Log connection & characteristic update ======
    # TODO: check if those supers do anything - fairly certain they are virtual methods

    def connect_succeeded(self):
        super().connect_succeeded()
        logger.info("[%s] Connected" % (self.mac_address))

    def connect_failed(self, error):
        super().connect_failed(error)
        logger.debug("[%s] Connection failed: %s" % (self.mac_address, str(error)))
        sys.exit()

    def disconnect_succeeded(self):
        super().disconnect_succeeded()
        logger.info("[%s] Disconnected" % (self.mac_address))

    def characteristic_value_updated(self, characteristic, value):
        logger.debug(f"The updated value for {characteristic.uuid} is: {value}")

    # ====== Control Point Response Methods ======

    def control_point_response(self, characteristic, response_type: int, error = None):
        """Handle responses from indicated control points
        
        virutal method to be implemented by subclass"""
        pass

    def characteristic_write_value_succeeded(self, characteristic):
        logger.debug(f"A new value has been written to {characteristic.uuid}")
        self.control_point_response(characteristic,response_type=WRITE_SUCCESS)

    def characteristic_write_value_failed(self, characteristic, error):
        logger.debug(f"A new value has not been written to {characteristic.uuid} successfully: {str(error)}")
        self.control_point_response(characteristic,response_type=WRITE_FAIL,error=error)

    def characteristic_enable_notification_succeeded(self, characteristic):
        logger.debug(f"The {characteristic.uuid} has been enabled with notification!")
        self.control_point_response(characteristic,response_type=NOTIFICATION_SUCCESS)

    def characteristic_enable_notification_failed(self, characteristic, error):
        logger.debug(f"Cannot enable notification for {characteristic.uuid}: {str(error)}")
        self.control_point_response(characteristic,response_type=NOTIFICATION_FAIL,error=error)  
    
    # ====== Request & Resolve control point
    def services_resolved(self):
        super().services_resolved()

# ======== Wahoo Controller Class ========

class WahooController(GATTInterface):
    """This sub-class should extend the GATTInterface class to also handle:
    * MQTT [or any alternative networking protocols]
    * Individual Wahoo devices
    * Pulling data from devices"""

    def __init__(self, mac_address, manager, args, managed=True):
        super().__init__(mac_address, manager, args, managed)

        # CLI parser arguments
        self.args = args

        # MQTT client
        self.setup_mqtt_client()

        # Wahoo devices
        self.climber = Climber(self,args)
        self.resistance = Resistance(self,args)
        self.fan = HeadwindFan(self,args)

        self.devices = [self.climber, self.resistance, self.fan]

        # Wahoo data handler
        self.wahoo_data = WahooData(self,args)

    # ===== MQTT methods =====
    
    def setup_mqtt_client(self):
        self.mqtt_client = MQTTClientWithSendingFTMSCommands(self.args.broker_address, self.args.username, self.args.password, self)
        self.mqtt_client.setup_mqtt_client()
    
    def on_message(self, msg):
        """Run when a subscribed MQTT topic publishes"""
        logger.info('MQTT message received')
        for device in self.devices:
            device.on_message(msg)

    def subscribe(self, topic: str, SubscribeOptions: int = 0):
        """Subscribe to a MQTT topic"""
        logger.info(f'MQTT Subscribing to topic {topic}')
        self.mqtt_client.subscribe([(topic, SubscribeOptions)])

    def publish(self, topic: str, payload):
        """Publish to a MQTT topic"""
        self.mqtt_client.publish(topic, payload)
    
    def mqtt_data_report_payload(self, device_type, value):
        """Create a standardised payload for MQTT publishing"""
        # TODO: add more json data payload whenever needed later
        return json.dumps({"value": value, "unitName": DEVICE_UNIT_NAMES[device_type], "timestamp": time.time(), "metadata": { "deviceName": platform.node() } })

    # ===== GATT for devices =====

    def set_service_or_characteristic(self, service_or_characteristic):
        super().set_service_or_characteristic(service_or_characteristic)

        # TODO: add flow control by returning True if the service/characteristic was matched and then terminating the search
        for device in self.devices:
            device.set_control_point(service_or_characteristic)

    def characteristic_value_updated(self, characteristic, value):
        super().characteristic_value_updated(characteristic,value)

        if characteristic == self.indoor_bike_data:
            self.wahoo_data.process_data(value)

    def control_point_response(self, characteristic, response_type: int, error = None):
        """forward responses and their types to devices"""

        # TODO: handle responses from own control point
        
        # forward responses
        for device in self.devices:
            device.control_point_response(characteristic, response_type, error)

    # ===== Resolve control point & services/characteristics

    # this is the main process that will be run all time after manager.run() is called
    # FIXME: despite what is stated above - this process is not looped - it runs only once in testing.
    # Maybe it reruns it until all services & characteristics have been resolved?
    # TODO: Double check all the below to ensure it is working and clean it up a bit
    def services_resolved(self):
        super().services_resolved()

        print("[%s] Resolved services" % (self.mac_address))
        for service in self.services:
            print("[%s]\tService [%s]" % (self.mac_address, service.uuid))
            self.set_service_or_characteristic(service)

            for characteristic in service.characteristics:
                self.set_service_or_characteristic(characteristic)
                print("[%s]\t\tCharacteristic [%s]" % (self.mac_address, characteristic.uuid))
                print("The characteristic value is: ", characteristic.read_value())

                # TODO: check if it is necessary to filter by service - if so rewrite set_service_or_characteristic to take a service arg
                """
                if self.ftms == service:
                    # set for FTMS control point for resistance control
                    self.set_service_or_characteristic(characteristic)
                if self.custom_incline_service == service:
                    # set for custom control point for incline control
                    self.set_service_or_characteristic(characteristic)
                """

        # continue if FTMS service is found from the BLE device
        if self.ftms and self.indoor_bike_data:

            # request control point
            self.ftms_control_point.write_value(bytearray([FTMS_REQUEST_CONTROL]))

            # start looping MQTT messages
            self.mqtt_client.loop_start()




# ======== Wahoo Device Classes ========

class WahooDevice:
    """A virtual class for Wahoo devices"""

    def __init__(self, name: str, controller: WahooController, command_topic: str, report_topic: str):
        """
        Create a Wahoo Device

        Parameters
        ----------
        name : str 
            Name of the device for logging. Will be automatically converted to all upper case.
        controller : WahooController 
            The WahooController object which will handle the device.
        command_topic : str
            MQTT topic used to control device. Should be passed using ArgumentParser.
        report_topic : str
            MQTT topic used for device to report current status. Should be passed using ArgumentParser.
        """

        # device controller
        self.controller = controller

        # device name for logging
        self._name = name.upper()

        # command topic
        self.command_topic = command_topic
        self.controller.subscribe(self.command_topic)

        # report topic
        self.report_topic = report_topic

        # device control point service & characteristic
        self.control_point_service = None
        self.control_point = None

        # hold value while writing
        self._internal_value = None # internal device value for tracking physical device value
        self._new_internal_value = None # human readable value for logging
        self._new_write_value = None # byte array value to be written to control point

        # threading variables
        self._TIMEOUT = 10
        self.terminate_write = False
        self.write_timeout_count = 0
        self.write_thread = None

        # log device status & info
        logger.info(f'{self._name} initialised')
        logger.debug(f'{self._name} command topic is {self.command_topic}')

    def set_control_point(self, service_or_characteristic):
        """Set UUID of passed service/characteristic if it is a required control point service/characteristic

        To be implemented by subclass"""
        pass

    def on_message(self, msg):
        """Receive subscribed MQTT messages

        To be implemented by subclass"""
        pass

    # TODO: change this to report overall status of device
    def report(self):
        """Report a successful value write"""
        payload = self.controller.mqtt_data_report_payload(self._name.lower(),self._internal_value)
        self.controller.publish(self.report_topic,payload)

    def control_point_response(self, characteristic, response_type: int, error = None):
        """Handle responses from the control point"""

        # return if the device's control point has not been set
        if self.control_point is None: return

        # if the response is not from the relevant control point then return
        if characteristic.uuid != self.control_point.uuid: return

        # on successful write terminate the thread and update the internal device value
        if response_type == WRITE_SUCCESS: 
            # TODO: LOCK this property at start of method
            self.terminate_write = True
            self._internal_value = self._new_internal_value

            # report & log a successful write
            self.report()
            logger.debug(f'{self._name} WRITE SUCCESS: {self._new_internal_value}') 
        
        # on failed write try writing again until timeout
        elif response_type == WRITE_FAIL:
            self.write_timeout_count += 1
            self.write_thread.start()
            logger.debug(f'{self._name} WRITE FAILED: {self._new_internal_value}')

        # TODO: Add check that we are notifying the correct characteristic - handling error responses
        # on successful enabling of notification on control point log
        elif response_type == NOTIFICATION_SUCCESS:
            logger.debug(f'{self._name} NOTIFICATION SUCCESS')

        # on fail to enable notification on control point try again
        elif response_type == NOTIFICATION_FAIL:
            logger.debug(f'{self._name} NOTIFICATION FAILED')
            self.control_point.enable_notifications() 

    def write_value(self,val):
        """Try to write a new device value until timeout, success or recieve new value to be written"""

        # define the new value internally
        self._new_write_value = val

        # terminate any current threads
        # TODO: LOCK this property at start of method
        self.terminate_write = True
        if self.write_thread != None: self.write_thread.join()

        # setup & start new thread
        self.terminate_write = False
        self.write_timeout_count = 0
        self.write_thread = threading.Thread(name=f'{self._name}_write',target=self.write_process)
        self.write_thread.start()
        
    def write_process(self):
        """Attempt to write the new device value until successful or forced to terminate"""

        # write the new value until termination or timeout
        if not (self.terminate_write and self.write_timeout_count >= self._TIMEOUT):
            self.control_point.write_value(self._new_write_value)

class Climber(WahooDevice):
    """Handles control of the KICKR Climb"""

    def __init__(self, controller: WahooController, args):
        super().__init__('CLIMBER',controller,args.incline_command_topic,args.incline_report_topic)

    def set_control_point(self, service_or_characteristic):
        
        # find the custom KICKR climb control point service & characteristic
        if service_or_characteristic_found_full_match(INCLINE_CONTROL_SERVICE_UUID, service_or_characteristic.uuid):
            self.control_point_service = service_or_characteristic
            logger.debug(f'{self._name} service found')

        elif service_or_characteristic_found_full_match(INCLINE_CONTROL_CHARACTERISTIC_UUID, service_or_characteristic.uuid):
            self.control_point = service_or_characteristic
            logger.debug(f'{self._name} characteristic found')
            self.control_point.enable_notifications() 

    def on_message(self, msg):
        """Receive MQTT messages"""
        
        # check if it is the incline topic
        if bool(re.search("/incline", msg.topic, re.IGNORECASE)):

            logger.info(f'{self._name} MQTT message received')
            logger.debug(f'Received Climber Message: {msg}')

            # convert, validate, and write the new value
            value = str(msg.payload, 'utf-8')

            # TODO: add error checking and reporting for converting from str to dict
            value = json.loads(value)
            value = value['incline']
            if INCLINE_MIN <= value <= INCLINE_MAX and value % 0.5 == 0:
                self._new_internal_value = value
                self.write_value(bytearray([INCLINE_CONTROL_OP_CODE] + convert_incline_to_op_value(self._new_internal_value)))
            else:
                logger.debug(f'{self._name} MQTT COMMAND FAIL : value must be in range 19 to -10 with 0.5 resolution : {value}')
                # TODO: report error
"""            if bool(re.search("[-+]?\d+$", value)):
                value = float(value)
                if INCLINE_MIN <= value <= INCLINE_MAX and value % 0.5 == 0:
                    self._new_internal_value = value
                    self.write_value(bytearray([INCLINE_CONTROL_OP_CODE] + convert_incline_to_op_value(self._new_internal_value)))
                else:
                    logger.debug(f'{self._name} MQTT COMMAND FAIL : value must be in range 19 to -10 with 0.5 resolution : {value}')
                    # TODO: report error
            else:
                logger.debug(f'{self._name} MQTT COMMAND FAIL : non-numeric value sent : {value}')
                # TODO: report error"""


class Resistance(WahooDevice):
    """Handles control of the Resistance aspect of the KICKR Smart Trainer"""

    def __init__(self, controller: WahooController, args):
        super().__init__('Resistance',controller,args.resistance_command_topic,args.resistance_report_topic)

    def set_control_point(self, service_or_characteristic):

        # setup aliases for the FTMS control point service & characteristic
        self.control_point_service = self.controller.ftms
        self.control_point = self.controller.ftms_control_point
    
    def on_message(self, msg):
        """Receive MQTT messages"""

        # check if it is the resistance topic
        if bool(re.search("/resistance", msg.topic, re.IGNORECASE)):

            logger.info(f'{self._name} MQTT message received')

            # convert, validate, and write the new value
            value = str(msg.payload, 'utf-8')
            if bool(re.search("[-+]?\d+$", value)):
                value = float(value)
                if RESISTANCE_MIN <= value <= RESISTANCE_MAX:
                    self._new_internal_value = value
                    self.write_value(bytearray([FTMS_SET_TARGET_RESISTANCE_LEVEL, int(self._new_internal_value)])) # FIXME: Should be able to write as a float but temp rounding to int
                else:
                    logger.debug(f'{self._name} MQTT COMMAND FAIL : value must be an integer between 0 and 100 : {value}')
                    # TODO: report error
            else:
                logger.debug(f'{self._name} MQTT COMMAND FAIL : non-numeric value sent : {value}')
                # TODO: report error


# TODO: fan driver just writes values and hopes. Need to investigate fan for correct values - use bluetoothctl GATT operations
class HeadwindFan(WahooDevice):
    """Handles control of the KICKR Headwind Smart Bluetooth Fan"""

    def __init__(self, controller: WahooController, args):
        super().__init__('Fan',controller,args.fan_command_topic,args.fan_report_topic)

    def set_control_point(self, service_or_characteristic):
        pass

    def on_message(self, msg):
        """This needs fixing """
            
        # check if it is the fan topic
        if bool(re.search("/fan", msg.topic, re.IGNORECASE)):

            logger.info(f'{self._name} MQTT message received')

            # convert, validate, and write the new value
            value = str(msg.payload, 'utf-8')
            if bool(re.search("[-+]?\d+$", value)):
                value = float(value)
                if FAN_MIN <= value <= FAN_MAX and value % 1 == 0: # FIXME: replace 1 with correct resolution value
                    self._new_internal_value = value
                    self.write_value(bytearray([0x02, self._new_internal_value])) # FIXME: replace 0x02 with a value we are sure is correct
                else:
                    logger.debug(f'{self._name} MQTT COMMAND FAIL : value must be in range 0 to 100 with UNKNOWN resolution : {value}') # FIXME: replace UNKNOWN with correct value
                    # TODO: report error
            else:
                logger.debug(f'{self._name} MQTT COMMAND FAIL : non-numeric value sent : {value}')
                # TODO: report error

"""
Class to handle data from Wahoo devices. All data is streamed through the KICKR smart trainer so best handled by a single class.
All code copied over from old code so it could use a clean up. 
A lot of the processed data is not relevant - most are for Wahoo devices we do not have - but someone put the effort into developing
those bits so might as well keep it in case we use it in the future.
I think things could be cleaned up a lot on the pull_value method but need better undertsanding of handling bit data.
"""

class WahooData:

    def __init__(self, controller: WahooController, args):
        
        # device controller
        self.controller = controller

        # CLI parser arguments
        self.args = args

        # track if bike is new data
        self.idle = False

        # data flags
        """Many of these are for Wahoo devices that we are not using/do not have"""
        self.flag_instantaneous_speed = None
        self.flag_average_speed = None
        self.flag_instantaneous_cadence = None
        self.flag_average_cadence = None
        self.flag_total_distance = None
        self.flag_resistance_level = None
        self.flag_instantaneous_power = None
        self.flag_average_power = None
        self.flag_expended_energy = None
        self.flag_heart_rate = None
        self.flag_metabolic_equivalent = None
        self.flag_elapsed_time = None
        self.flag_remaining_time = None

        # data values
        self.instantaneous_speed = None
        self.average_speed = None
        self.instantaneous_cadence = None
        self.average_cadence = None
        self.total_distance = None
        self.resistance_level = None
        self.instantaneous_power = None
        self.average_power = None
        self.expended_energy_total = None
        self.expended_energy_per_hour = None
        self.expended_energy_per_minute = None
        self.heart_rate = None
        self.metabolic_equivalent = None
        self.elapsed_time = None
        self.remaining_time = None

    def process_data(self, value):
        self.reported_data(value)
        self.pull_value(value)
        self.publish_data()

    def reported_data(self, value):
        """Check the received bit data for which data was reported"""
        self.flag_instantaneous_speed = not((value[0] & 1) >> 0)
        self.flag_average_speed = (value[0] & 2) >> 1
        self.flag_instantaneous_cadence = (value[0] & 4) >> 2
        self.flag_average_cadence = (value[0] & 8) >> 3
        self.flag_total_distance = (value[0] & 16) >> 4
        self.flag_resistance_level = (value[0] & 32) >> 5
        self.flag_instantaneous_power = (value[0] & 64) >> 6
        self.flag_average_power = (value[0] & 128) >> 7
        self.flag_expended_energy = (value[1] & 1) >> 0
        self.flag_heart_rate = (value[1] & 2) >> 1
        self.flag_metabolic_equivalent = (value[1] & 4) >> 2
        self.flag_elapsed_time = (value[1] & 8) >> 3
        self.flag_remaining_time = (value[1] & 16) >> 4

    def pull_value(self, value):
        """Get the reported data from the bit data"""
        offset = 2

        if self.flag_instantaneous_speed:
            self.instantaneous_speed = float((value[offset+1] << 8) + value[offset]) / 100.0 * 5.0 / 18.0
            offset += 2
            logger.info(f"Instantaneous Speed: {self.instantaneous_speed} m/s")

        if self.flag_average_speed:
            self.average_speed = float((value[offset+1] << 8) + value[offset]) / 100.0 * 5.0 / 18.0
            offset += 2
            logger.info(f"Average Speed: {self.average_speed} m/s")

        if self.flag_instantaneous_cadence:
            self.instantaneous_cadence = float((value[offset+1] << 8) + value[offset]) / 10.0
            offset += 2
            logger.info(f"Instantaneous Cadence: {self.instantaneous_cadence} rpm")

        if self.flag_average_cadence:
            self.average_cadence = float((value[offset+1] << 8) + value[offset]) / 10.0
            offset += 2
            logger.info(f"Average Cadence: {self.average_cadence} rpm")

        if self.flag_total_distance:
            self.total_distance = int((value[offset+2] << 16) + (value[offset+1] << 8) + value[offset])
            offset += 3
            logger.info(f"Total Distance: {self.total_distance} m")

        if self.flag_resistance_level:
           self.resistance_level = int((value[offset+1] << 8) + value[offset])
           offset += 2
           logger.info(f"Resistance Level: {self.resistance_level}")

        if self.flag_instantaneous_power:
            self.instantaneous_power = int((value[offset+1] << 8) + value[offset])
            offset += 2
            logger.info(f"Instantaneous Power: {self.instantaneous_power} W")

        if self.flag_average_power:
            self.average_power = int((value[offset+1] << 8) + value[offset])
            offset += 2
            logger.info(f"Average Power: {self.average_power} W")

        if self.flag_expended_energy:
            expended_energy_total = int((value[offset+1] << 8) + value[offset])
            offset += 2
            if expended_energy_total != 0xFFFF:
                self.expended_energy_total = expended_energy_total
                logger.info(f"Expended Energy: {self.expended_energy_total} kCal total")

            expended_energy_per_hour = int((value[offset+1] << 8) + value[offset])
            offset += 2
            if expended_energy_per_hour != 0xFFFF:
                self.expended_energy_per_hour = expended_energy_per_hour
                logger.info(f"Expended Energy: {self.expended_energy_per_hour} kCal/hour")

            expended_energy_per_minute = int(value[offset])
            offset += 1
            if expended_energy_per_minute != 0xFF:
                self.expended_energy_per_minute = expended_energy_per_minute
                logger.info(f"Expended Energy: {self.expended_energy_per_minute} kCal/min")

        if self.flag_heart_rate:
            self.heart_rate = int(value[offset])
            offset += 1
            logger.info(f"Heart Rate: {self.heart_rate} bpm")

        if self.flag_metabolic_equivalent:
            self.metabolic_equivalent = float(value[offset]) / 10.0
            offset += 1
            logger.info(f"Metabolic Equivalent: {self.metabolic_equivalent} METS")

        if self.flag_elapsed_time:
            self.elapsed_time = int((value[offset+1] << 8) + value[offset])
            offset += 2
            logger.info(f"Elapsed Time: {self.elapsed_time} seconds")

        if self.flag_remaining_time:
            self.remaining_time = int((value[offset+1] << 8) + value[offset])
            offset += 2
            logger.info(f"Remaining Time: {self.remaining_time} seconds")

        if offset != len(value):
            logger.error("ERROR: Payload was not parsed correctly")
            return
        
    def publish_data(self):
        """Publish if data or log that there was no relevant data"""

        if self.instantaneous_speed > 0:
            self.idle = False
            self.publish()
        else:
            if self.idle:
                logger.info('Bike currently idle, no data published')
            else:
                self.publish()
                self.idle = True

    def publish(self):
        """Publish data"""

        if self.flag_instantaneous_speed:
            self.controller.publish(self.args.speed_report_topic, self.controller.mqtt_data_report_payload('speed', self.instantaneous_speed))
        if self.flag_instantaneous_cadence:
            self.controller.mqtt_client.publish(self.args.cadence_report_topic, self.controller.mqtt_data_report_payload('cadence', self.instantaneous_cadence))
        if self.flag_instantaneous_power:
            self.controller.mqtt_client.publish(self.args.power_report_topic, self.controller.mqtt_data_report_payload('power', self.instantaneous_power))
