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
from lib.constants import RESISTANCE_MIN, RESISTANCE_MAX, INCLINE_MIN, INCLINE_MAX, FTMS_UUID, RESISTANCE_LEVEL_RANGE_UUID, INCLINATION_RANGE_UUID, FTMS_CONTROL_POINT_UUID, FTMS_REQUEST_CONTROL, FTMS_RESET, FTMS_SET_TARGET_RESISTANCE_LEVEL, INCLINE_REQUEST_CONTROL, INCLINE_CONTROL_OP_CODE, INCLINE_CONTROL_SERVICE_UUID, INCLINE_CONTROL_CHARACTERISTIC_UUID, INDOOR_BIKE_DATA_UUID, DEVICE_UNIT_NAMES

"""
TODO design testing of changes for
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

TODO merge other Wahoo devices
    - Fan
    - Add heart monitor data report to WahooData

TODO next stage
    - report errors from device clients upstream back to game
        0 just send the log message with time to a MQTT topic
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

# a sleep time to wait for a characteristic.writevalue() action to be completed
WRITEVALUE_WAIT_TIME = 0.1 # TODO: If this doesn't work well, it needs to change this short sleep mechainism to a async process mechainism for sending consequetive BLE commands (eg., threading control)

# define Control Point response type constants
WRITE_SUCCESS, WRITE_FAIL, NOTIFICATION_SUCCESS, NOTIFICATION_FAIL = range(4)

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
        elif service_or_characteristic_found(FTMS_CONTROL_POINT_UUID, service_or_characteristic.uuid):
            self.ftms_control_point = service_or_characteristic
        elif service_or_characteristic_found(INDOOR_BIKE_DATA_UUID, service_or_characteristic.uuid):
            self.indoor_bike_data = service_or_characteristic
            self.indoor_bike_data.enable_notification()

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
        logger.debug(f"The updated value for {characteristic.uuid} is:", value)

    # ====== Control Point Response Methods ======

    def control_point_response(self, characteristic, response_type: int, error = None):
        """Handle responses from indicated control points
        
        virutal method to be implemented by child"""
        pass

    def characteristic_write_value_succeeded(self, characteristic):
        logger.debug(f"WRITE SUCCESS : {characteristic.uuid}")
        self.control_point_response(characteristic,response_type=WRITE_SUCCESS)

    def characteristic_write_value_failed(self, characteristic, error):
        logger.debug(f"WRITE FAIL : {characteristic.uuid} : {str(error)}")
        self.control_point_response(characteristic,response_type=WRITE_FAIL,error=error)

    def characteristic_enable_notification_succeeded(self, characteristic):
        logger.debug(f"NOTIFICATION ENABLED : {characteristic.uuid}")
        self.control_point_response(characteristic,response_type=NOTIFICATION_SUCCESS)

    def characteristic_enable_notification_failed(self, characteristic, error):
        logger.debug(f"NOTIFICATION ENABLING FAILED : {characteristic.uuid} : {str(error)}")
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
        for device in self.devices:
            device.on_message(msg)

    def subscribe(self, topic: str, SubscribeOptions: int = 0):
        """Subscribe to a MQTT topic"""
        self.mqtt_client.subscribe((topic,SubscribeOptions))

    def publish(self, topic: str, payload):
        """Publish to a MQTT topic"""
        self.mqtt_client.publish(topic, payload)
    
    def mqtt_data_report_payload(self, device_type, value):
        """Create a standardised payload for MQTT publishing"""
        # TODO: add more json data payload whenever needed later
        return json.dumps({"value": value, "unitName": DEVICE_UNIT_NAMES[device_type], "timestamp": time.time(), "metadata": { "deviceName": platform.node() } })

    # ===== GATT for devices =====

    def set_service_or_characteristic(self, service_or_characteristic):
        super().set_service_or_characteristic(self, service_or_characteristic)

        # TODO: add flow control by returning True if the service/characteristic was matched and then terminating the search
        for device in self.devices:
            device.set_service_or_characteristic(service_or_characteristic)

    def characteristic_value_updated(self, characteristic, value):
        super().set_service_or_characteristic(self,characteristic,value)

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

    def __init__(self, controller: WahooController, args):

        # device controller
        self.controller = controller

        # CLI parser arguments
        self.args = args

        # command topic
        self.command_topic = None

        # report topic
        self.report_topic = None

        # device control point service & characteristic
        self.control_point_service = None
        self.control_point = None

        # constants for threading
        self._TIMEOUT = 10

    def set_control_point(self, service_or_characteristic):
        """Set UUID of passed service/characteristic if it is a required control point service/characteristic

        To be implemented by subclass"""
        pass

    def on_message(self, msg):
        """Receive subscribed MQTT messages

        To be implemented by subclass"""
        pass

    def control_point_response(self, characteristic, response_type: int, error = None):
        """Handle responses from control point. Responses should be used for flow control of threading.
        
        To be implemented by subclass"""
        pass

class Climber(WahooDevice):
    """Handles control of the KICKR Climb"""

    def __init__(self, controller: WahooController, args):
        super().__init__(self,controller,args)

        # device variable
        self._incline = 0
        self._new_incline = None

        # command topic
        self.command_topic = self.args.incline_command_topic
        self.controller.subscribe(self.command_topic)

        # report topic
        self.report_topic = self.args.incline_report_topic

        # threading
        self.terminate_write = False
        self.write_timeout_count = 0
        self.write_thread = None

    def set_control_point(self, service_or_characteristic):
        
        # find the custom KICKR climb control point service & characteristic
        if service_or_characteristic_found_full_match(INCLINE_CONTROL_SERVICE_UUID, service_or_characteristic.uuid):
            self.control_point_service = service_or_characteristic
        elif service_or_characteristic_found_full_match(INCLINE_CONTROL_CHARACTERISTIC_UUID, service_or_characteristic.uuid):
            self.control_point = service_or_characteristic
            # TODO: check whether this requires that the FTMS control point has already successfully been requested control of
            self.control_point.enable_notification() 

    def on_message(self, msg):
        """Receive MQTT messages"""
        
        # check if it is the incline topic
        if bool(re.search("/incline", msg.topic, re.IGNORECASE)):

            # convert, validate, and write the new value
            value = str(msg.payload, 'utf-8')
            if bool(re.search("[-+]?\d+$", value)):
                value = float(value)
                if INCLINE_MIN <= value <= INCLINE_MAX and value % 0.5 == 0:
                    self.incline = value
                else:
                    logger.debug(f'INCLINE MQTT COMMAND FAIL : value must be in range 19 to -10 with 0.5 resolution : {value}')
            else:
                logger.debug(f'INCLINE MQTT COMMAND FAIL : non-numeric value sent : {value}')

    def report(self):
        """Report a successful incline write"""
        payload = self.controller.mqtt_data_report_payload('incline',self._incline)
        self.controller.publish(self.report_topic,payload)

    def control_point_response(self, characteristic, response_type: int, error = None):
        """Handle responses from the control point"""

        # if the response is not from the relevant control point then return
        if characteristic.uuid != self.control_point.uuid: return

        # on successful write terminate the thread and update the internal incline value
        if response_type == WRITE_SUCCESS: 
            # TODO: LOCK this property at start of method
            self.terminate_write = True
            self._incline = self._new_incline
            self.report()
            logger.debug(f'INCLINE WRITE SUCCESS: {self._incline}')
        
        # on failed write try writing again until timeout
        elif response_type == WRITE_FAIL:
            self.write_timeout_count += 1
            self.write_thread.start()
            logger.debug(f'INCLINE WRITE FAILED: {self._new_incline}')

        # TODO: Add check that we are notifying the correct characteristic - handling error responses
        # on successful enabling of notification on control point log
        elif response_type == NOTIFICATION_SUCCESS:
            logger.debug('INCLINE NOTIFICATION SUCCESS')

        # on fail to enable notification on control point try again
        elif response_type == NOTIFICATION_FAIL:
            logger.debug('INCLINE NOTIFICATION FAILED')
            self.control_point.enable_notification() 

    @property
    def incline(self,val):
        """Try to write a new incline value until timeout, success or new value to be written"""

        # define the new value internally
        self._new_incline = val

        # terminate any current threads
        # TODO: LOCK this property at start of method
        self.terminate_write = True
        self.write_thread.join()

        # setup & start new thread
        self.terminate_write = False
        self.write_timeout_count = 0
        self.write_thread = threading.Thread(name='write_new_incline',target=self.write_new_incline,args=(self._new_incline))
        self.write_thread.start()
        
    def write_new_incline(self):
        """Attempt to write the new incline value until successful or forced to terminate"""

        # write the new value until termination or timeout
        if not (self.terminate_write and self.write_timeout_count >= self._TIMEOUT):
            self.control_point.write_value(bytearray([INCLINE_CONTROL_OP_CODE] + convert_incline_to_op_value(self._new_incline)))


class Resistance(WahooDevice):
    """Handles control of the Resistance aspect of the KICKR Smart Trainer"""

    def __init__(self, controller: WahooController, args):
        super().__init__(self,controller,args)

        # device variable
        self._resistance = 0

        # command topic
        self.command_topic = self.args.resistance_command_topic
        self.controller.subscribe(self.command_topic)

        # report topic
        self.report_topic = self.args.resistance_report_topic

    def set_control_point(self, service_or_characteristic):

        # setup aliases for the FTMS control point service & characteristic
        self.control_point_service = self.controller.ftms
        self.control_point = self.controller.ftms_control_point
    
    def on_message(self, msg):
        """Receive MQTT messages"""
        pass

    # =========================================

    # TODO: add response reactions to Resistance class
    """def characteristic_write_value_succeeded(self, characteristic):

        # set new resistance or inclination and notify to MQTT if the async write value action is succeeded
        if service_or_characteristic_found(FTMS_CONTROL_POINT_UUID, characteristic.uuid):
            if self.new_resistance is not None:
                self.set_new_resistance()"""
    
    # ==========================================

class HeadwindFan(WahooDevice):
    """Handles control of the KICKR Headwind Smart Bluetooth Fan"""

    def __init__(self, controller: WahooController, args):
        super().__init__(self,controller,args)
        
        # device variable
        self._fan_power = 0

    def on_message(self, msg):
        """Receive MQTT messages"""
        pass

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




