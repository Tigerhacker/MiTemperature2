#!/usr/bin/python3 -u
#!/home/openhabian/Python3/Python-3.7.4/python -u
#-u to unbuffer output. Otherwise when calling with nohup or redirecting output things are printed very lately or would even mixup

print("---------------------------------------------")
print("MiTemperature2 / ATC Thermometer version 3.1")
print("---------------------------------------------")

from bluepy import btle
import argparse
import os
import re
from dataclasses import dataclass
from collections import deque
import threading
import time
import signal
import traceback
import math
import logging
import json
import requests

import struct
import socket
from influxdb import InfluxDBClient

HOSTNAME = socket.gethostname()


INFLUXDB_NAME = "DB_NAME"
TARGETS = {
    'A4:C1:00:00:00:00': {'name': 'ATC_1', 'time_snap':5},
    '4C:65:00:00:00:00': {'name': 'MJ_HT_V1_1', 'time_snap': 2},
}
influxClient = InfluxDBClient("127.0.0.1", "8086", "username", "password", INFLUXDB_NAME)


measurements=deque()
#globalBatteryLevel=0
previousMeasurements={}
identicalCounters={}
MQTTClient=None
MQTTTopic=None
receiver=None
subtopics=None
mqttJSONDisabled=False

def unpack_bt_payload(data):
    length = len(data)
    payload = []
    if length > 2:
        i = 0
        while i+2 < length:
            segment_length = int(data[i])
            segment_id = data[i+1]
            segment_data = data[i+2:i+segment_length+1]
            payload.append({
                'len': segment_length,
                'id': segment_id,
                'data': segment_data.hex()
            })
            i += segment_length+1
        return payload
    else:
        print("No data: {}".format(length))
        return None

def unpack_xiaomi_payload(data):
    if len(data) >= 22: 
        uuid = data[6:8].hex()
        product_id = data[10:12].hex()
        mac = data[13:19].hex()

        if uuid == '95fe' and  product_id =='aa01':
            metrics = data[19]
            payload_length = data[21]
            payload_data = data[22:]

            frame_counter = data[12]

            temperature = None
            humidity = None

            payload = {}

            if metrics == 13:
                (temperature, humidity) = struct.unpack("HH", payload_data)
            elif metrics == 10:
                payload['battery'] = data[22]
            elif metrics == 6:
                (humidity, ) = struct.unpack("H", payload_data)
            elif metrics == 4:
                (temperature, ) = struct.unpack("H", payload_data)

            if temperature is not None:
                payload['temperature'] = temperature/10
            if humidity is not None:
                payload['humidity'] = humidity/10

            info = {
                'metadata':{
                    'length': data[0],
                    'id': data[1]
                },
                'metrics': hex(metrics),
                'frame_counter': frame_counter,
                'payload': payload,
                'device': 'MJ_HT_V1'
            }

            return info
    return None

def unpack_pvvx_payload(data):
    if len(data) == 20: 
        uuid = data[3:5].hex()
        mac = data[5:11].hex()

        if uuid == '1a18':
            (temperature, humidity, battery_mv, battery, frame_counter, flags) = struct.unpack("hHHBBB", data[11:])

            payload = {
                'temperature': temperature/100, 
                'humidity': humidity/100, 
                'battery_mv': battery_mv, 
                'battery': battery, 
            }

            info = {
                'metadata':{
                    'length': data[0],
                    'id': data[1]
                },
                'frame_counter': frame_counter,
                'payload': payload,
                'device': 'LYWSD03MMC',
                'flags': flags, 
            }

            return info
    return None

def myMQTTPublish(topic,jsonMessage):
    global subtopics
    if len(subtopics) > 0:
        messageDict = json.loads(jsonMessage)
        for subtopic in subtopics:
            print("Topic:",subtopic)
            MQTTClient.publish(topic + "/" + subtopic,messageDict[subtopic],0)
    if not mqttJSONDisabled:
        MQTTClient.publish(topic,jsonMessage,1)

INFLUX_FIELD_MAPPING = {
    'temperature': float,
    'humidity': float,
    'battery': int,
    'battery_mv': float,
    'rssi': float,
}

def buildInfluxJSON(data, time_snap=None, name=None):
    timestamp = data['timestamp']
    if time_snap is not None:
        timestamp = timestamp - (timestamp % time_snap)
    payload = {
        "measurement": "temp_hum",
        "tags": {
            "id": data['mac'].upper(),
            "device": data['device'],
            "frame_counter": int(data['frame_counter']),
            "reciever": data['reciever'],
        },
        "fields": {
            'rssi': float(data['rssi']),
        },
        "time": int(timestamp),
    }
    if name is not None:
        payload['tags']['name'] = name
    measurements = data['payload']
    for m in measurements.keys():
        dataType = INFLUX_FIELD_MAPPING[m]
        payload['fields'][m] = dataType(measurements[m])

    return payload

def signal_handler(sig, frame):
    if args.atc:
        disable_le_scan(sock)	
    os._exit(0)
        
def watchDog_Thread():
    global unconnectedTime
    global connected
    global pid
    while True:
        logging.debug("watchdog_Thread")
        logging.debug("unconnectedTime : " + str(unconnectedTime))
        logging.debug("connected : " + str(connected))
        logging.debug("pid : " + str(pid))
        now = int(time.time())
        if (unconnectedTime is not None) and ((now - unconnectedTime) > 60): #could also check connected is False, but this is more fault proof
            pstree=os.popen("pstree -p " + str(pid)).read() #we want to kill only bluepy from our own process tree, because other python scripts have there own bluepy-helper process
            logging.debug("PSTree: " + pstree)
            try:
                bluepypid=re.findall(r'bluepy-helper\((.*)\)',pstree)[0] #Store the bluepypid, to kill it later
            except IndexError: #Should not happen since we're now connected
                logging.debug("Couldn't find pid of bluepy-helper")
            os.system("kill " + bluepypid)
            logging.debug("Killed bluepy with pid: " + str(bluepypid))
            unconnectedTime = now #reset unconnectedTime to prevent multiple killings in a row
        time.sleep(5)
    

# Export to influxDB
def thread_SendingData():
    global previousMeasurements
    global measurements

    payload_buffer = []
    while True:
        while True:
            try:
                mea = measurements.popleft()
                payload_buffer.append( buildInfluxJSON(mea, TARGETS[mea['mac']].get('time_snap'), TARGETS[mea['mac']].get('name') ) )
            except IndexError:
                break

        # print(payload_buffer)
        print("{}: Items: {}".format( time.strftime("%Y-%m-%d %H:%M:%S"), len(payload_buffer)) )

        # Send buffer
        try:
            if args.export:
                influxClient.write_points(payload_buffer, time_precision='s')
            # else:
            #     print(payload_buffer)
            payload_buffer = [] # only empty on success
        except Exception as e:
            # print("!Send failed")
            print("{}: Items: {} Send failed".format( time.strftime("%Y-%m-%d %H:%M:%S"), len(payload_buffer)) )
            print(e)
        # print("\n\n\n")

        time.sleep(10)


sock = None #from ATC 
lastBLEPaketReceived = 0
BLERestartCounter = 1
def keepingLEScanRunning(): #LE-Scanning gets disabled sometimes, especially if you have a lot of BLE connections, this thread periodically enables BLE scanning again
    global BLERestartCounter
    while True:
        time.sleep(1)
        now = time.time()
        if now - lastBLEPaketReceived > args.watchdogtimer:
            print("Watchdog: Did not receive any BLE Paket within", int(now - lastBLEPaketReceived), "s. Restarting BLE scan. Count:", BLERestartCounter)
            disable_le_scan(sock)
            enable_le_scan(sock, filter_duplicates=False)
            BLERestartCounter += 1
            print("")
            time.sleep(5) #give some time to take effect


def calibrateHumidity2Points(humidity, offset1, offset2, calpoint1, calpoint2):
    #offset1=args.offset1
    #offset2=args.offset2
    #p1y=args.calpoint1
    #p2y=args.calpoint2
    p1y=calpoint1
    p2y=calpoint2
    p1x=p1y - offset1
    p2x=p2y - offset2
    m = (p1y - p2y) * 1.0 / (p1x - p2x) # y=mx+b
    #b = (p1x * p2y - p2x * p1y) * 1.0 / (p1y - p2y)
    b = p2y - m * p2x #would be more efficient to do this calculations only once
    humidityCalibrated=m*humidity + b
    if (humidityCalibrated > 100 ): #with correct calibration this should not happen
        humidityCalibrated = 100
    elif (humidityCalibrated < 0):
        humidityCalibrated = 0
    humidityCalibrated=int(round(humidityCalibrated,0))
    return humidityCalibrated


mode="round"
class MyDelegate(btle.DefaultDelegate):
    def __init__(self, params):
        btle.DefaultDelegate.__init__(self)
        # ... initialise here
    
    def handleNotification(self, cHandle, data):
        global measurements
        try:
            measurement = Measurement(0,0,0,0,0,0,0,0)
            if args.influxdb == 1:
                measurement.timestamp = int((time.time() // 10) * 10)
            else:
                measurement.timestamp = int(time.time())
            temp=int.from_bytes(data[0:2],byteorder='little',signed=True)/100
            #print("Temp received: " + str(temp))
            if args.round:
                #print("Temperatur unrounded: " + str(temp
                
                if args.debounce:
                    global mode
                    temp*=10
                    intpart = math.floor(temp)
                    fracpart = round(temp - intpart,1)
                    #print("Fracpart: " + str(fracpart))
                    if fracpart >= 0.7:
                        mode="ceil"
                    elif fracpart <= 0.2: #either 0.8 and 0.3 or 0.7 and 0.2 for best even distribution
                        mode="trunc"
                    #print("Modus: " + mode)
                    if mode=="trunc": #only a few times
                        temp=math.trunc(temp)
                    elif mode=="ceil":
                        temp=math.ceil(temp)
                    else:
                        temp=round(temp,0)
                    temp /=10.
                    #print("Debounced temp: " + str(temp))
                else:
                    temp=round(temp,1)
            humidity=int.from_bytes(data[2:3],byteorder='little')
            print("Timestamp: {}".format(measurement.timestamp))
            print("Temperature: " + str(temp))
            print("Humidity: " + str(humidity))
            voltage=int.from_bytes(data[3:5],byteorder='little') / 1000.
            print("Battery voltage:",voltage,"V")
            measurement.temperature = temp
            measurement.humidity = humidity
            measurement.voltage = voltage
            measurement.sensorname = args.name
            #if args.battery:
                #measurement.battery = globalBatteryLevel
            batteryLevel = min(int(round((voltage - 2.1),2) * 100), 100) #3.1 or above --> 100% 2.1 --> 0 %
            measurement.battery = batteryLevel
            print("Battery level:",batteryLevel)
                

            if args.offset:
                humidityCalibrated = humidity + args.offset
                print("Calibrated humidity: " + str(humidityCalibrated))
                measurement.calibratedHumidity = humidityCalibrated

            if args.TwoPointCalibration:
                humidityCalibrated= calibrateHumidity2Points(humidity,args.offset1,args.offset2, args.calpoint1, args.calpoint2)
                print("Calibrated humidity: " + str(humidityCalibrated))
                measurement.calibratedHumidity = humidityCalibrated

            if args.callback or args.httpcallback:
                measurements.append(measurement)

            if(args.mqttconfigfile):
                if measurement.calibratedHumidity == 0:
                    measurement.calibratedHumidity = measurement.humidity
                jsonString=buildJSONString(measurement)
                myMQTTPublish(MQTTTopic,jsonString)
                #MQTTClient.publish(MQTTTopic,jsonString,1)


        except Exception as e:
            print("Fehler")
            print(e)
            print(traceback.format_exc())
        
# Initialisation  -------

def connect():
    #print("Interface: " + str(args.interface))
    p = btle.Peripheral(adress,iface=args.interface)	
    val=b'\x01\x00'
    p.writeCharacteristic(0x0038,val,True) #enable notifications of Temperature, Humidity and Battery voltage
    p.writeCharacteristic(0x0046,b'\xf4\x01\x00',True)
    p.withDelegate(MyDelegate("abc"))
    return p

def buildJSONString(measurement):
    jsonstr = '{"temperature": ' + str(measurement.temperature) + ', "humidity": ' + str(measurement.humidity) + ', "voltage": ' + str(measurement.voltage) \
        + ', "calibratedHumidity": ' + str(measurement.calibratedHumidity) + ', "battery": ' + str(measurement.battery) \
        + ', "timestamp": '+ str(measurement.timestamp) +', "sensor": "' + measurement.sensorname + '", "rssi": ' + str(measurement.rssi) \
        + ', "receiver": "' + receiver  + '"}'
    return jsonstr

# def MQTTOnConnect(client, userdata, flags, rc):
#     print("MQTT connected with result code "+str(rc))

# def MQTTOnPublish(client,userdata,mid):
#     print("MQTT published, Client:",client," Userdata:",userdata," mid:", mid)

# def MQTTOnDisconnect(client, userdata,rc):
#     print("MQTT disconnected, Client:", client, "Userdata:", userdata, "RC:", rc)	

# Main loop --------
parser=argparse.ArgumentParser(allow_abbrev=False)
parser.add_argument("--device","-d", help="Set the device MAC-Address in format AA:BB:CC:DD:EE:FF",metavar='AA:BB:CC:DD:EE:FF')
# parser.add_argument("--battery","-b", help="Get estimated battery level, in ATC-Mode: Get battery level from device", metavar='', type=int, nargs='?', const=1)
parser.add_argument("--count","-c", help="Read/Receive N measurements and then exit script", metavar='N', type=int)
parser.add_argument("--interface","-i", help="Specifiy the interface number to use, e.g. 1 for hci1", metavar='N', type=int, default=0)
parser.add_argument("--unreachable-count","-urc", help="Exit after N unsuccessful connection tries", metavar='N', type=int, default=0)
# parser.add_argument("--mqttconfigfile","-mcf", help="specify a configurationfile for MQTT-Broker")


# rounding = parser.add_argument_group("Rounding and debouncing")
# rounding.add_argument("--round","-r", help="Round temperature to one decimal place",action='store_true')
# rounding.add_argument("--debounce","-deb", help="Enable this option to get more stable temperature values, requires -r option",action='store_true')

# offsetgroup = parser.add_argument_group("Offset calibration mode")
# offsetgroup.add_argument("--offset","-o", help="Enter an offset to the reported humidity value",type=int)

# complexCalibrationGroup=parser.add_argument_group("2 Point Calibration")
# complexCalibrationGroup.add_argument("--TwoPointCalibration","-2p", help="Use complex calibration mode. All arguments below are required",action='store_true')
# complexCalibrationGroup.add_argument("--calpoint1","-p1", help="Enter the first calibration point",type=int)
# complexCalibrationGroup.add_argument("--offset1","-o1", help="Enter the offset for the first calibration point",type=int)
# complexCalibrationGroup.add_argument("--calpoint2","-p2", help="Enter the second calibration point",type=int)
# complexCalibrationGroup.add_argument("--offset2","-o2", help="Enter the offset for the second calibration point",type=int)

# callbackgroup = parser.add_argument_group("Callback related arguments")
# callbackgroup.add_argument("--callback","-call", help="Pass the path to a program/script that will be called on each new measurement")
# callbackgroup.add_argument("--httpcallback","-http", help="Pass the URL to a program/script that will be called on each new measurement")
# callbackgroup.add_argument("--name","-n", help="Give this sensor a name reported to the callback script")
# callbackgroup.add_argument("--skipidentical","-skip", help="N consecutive identical measurements won't be reported to callbackfunction",metavar='N', type=int, default=0)
# callbackgroup.add_argument("--influxdb","-infl", help="Optimize for writing data to influxdb,1 timestamp optimization, 2 integer optimization",metavar='N', type=int, default=0)

atcgroup = parser.add_argument_group("ATC mode related arguments")
atcgroup.add_argument("--atc","-a", help="Read the data of devices with custom ATC firmware flashed, use --battery to get battery level additionaly in percent",action='store_true')
atcgroup.add_argument("--watchdogtimer","-wdt",metavar='X', type=int, help="Re-enable scanning after not receiving any BLE packet after X seconds")
atcgroup.add_argument("--devicelistfile","-df",help="Specify a device list file giving further details to devices")
atcgroup.add_argument("--onlydevicelist","-odl", help="Only read devices which are in the device list file",action='store_true')
atcgroup.add_argument("--rssi","-rs", help="Report RSSI via callback",action='store_true')

atcgroup.add_argument("--export","-e", help="Export data to InfluxDB",action='store_true')
atcgroup.add_argument("--verbose","-v", help="Print out data as it is recieved",action='store_true')
atcgroup.add_argument("--vverbose","-vv", help="Print out all advertisments seen",action='store_true')

args=parser.parse_args()

# if args.devicelistfile or args.mqttconfigfile:
#     import configparser

if args.device:
    if re.match("[0-9a-fA-F]{2}([:]?)[0-9a-fA-F]{2}(\\1[0-9a-fA-F]{2}){4}$",args.device):
        adress=args.device
    else:
        print("Please specify device MAC-Address in format AA:BB:CC:DD:EE:FF")
        os._exit(1)
elif not args.atc:
    parser.print_help()
    os._exit(1)

# if args.TwoPointCalibration:
#     if(not(args.calpoint1 and args.offset1 and args.calpoint2 and args.offset2)):
#         print("In 2 Point calibration you have to enter 4 points")
#         os._exit(1)
#     elif(args.offset):
#         print("Offset calibration and 2 Point calibration can't be used together")
#         os._exit(1)
# if not args.name:
#     args.name = args.device

# if args.callback or args.httpcallback:
#     dataThread = threading.Thread(target=thread_SendingData)
#     dataThread.start()

if args.export:
    dataThread = threading.Thread(target=thread_SendingData)
    dataThread.start()
else:
    print("WARNING: EXPORT DISABLED, use --export to send data to influxDB")


signal.signal(signal.SIGINT, signal_handler)	

if args.device: 

    p=btle.Peripheral()
    cnt=0

    connected=False
    #logging.basicConfig(level=logging.DEBUG)
    logging.basicConfig(level=logging.ERROR)
    logging.debug("Debug: Starting script...")
    pid=os.getpid()	
    bluepypid=None
    unconnectedTime=None
    connectionLostCounter=0

    watchdogThread = threading.Thread(target=watchDog_Thread)
    watchdogThread.start()
    logging.debug("watchdogThread started")

    while True:
        try:
            if not connected:
                #Bluepy sometimes hangs and makes it even impossible to connect with gatttool as long it is running
                #on every new connection a new bluepy-helper is called
                #we now make sure that the old one is really terminated. Even if it hangs a simple kill signal was sufficient to terminate it
                # if bluepypid is not None:
                    # os.system("kill " + bluepypid)
                    # print("Killed possibly remaining bluepy-helper")
                # else:
                    # print("bluepy-helper couldn't be determined, killing not allowed")
                        
                print("Trying to connect to " + adress)
                p=connect()
                # logging.debug("Own PID: "  + str(pid))
                # pstree=os.popen("pstree -p " + str(pid)).read() #we want to kill only bluepy from our own process tree, because other python scripts have there own bluepy-helper process
                # logging.debug("PSTree: " + pstree)
                # try:
                    # bluepypid=re.findall(r'bluepy-helper\((.*)\)',pstree)[0] #Store the bluepypid, to kill it later
                # except IndexError: #Should not happen since we're now connected
                    # logging.debug("Couldn't find pid of bluepy-helper")				
                connected=True
                unconnectedTime=None
                
            # if args.battery:
                    # if(cnt % args.battery == 0):
                        # print("Warning the battery option is deprecated, Aqara device always reports 99 % battery")
                        # batt=p.readCharacteristic(0x001b)
                        # batt=int.from_bytes(batt,byteorder="little")
                        # print("Battery-Level: " + str(batt))
                        # globalBatteryLevel = batt
                
                
            if p.waitForNotifications(2000):
                # handleNotification() was called
                
                cnt += 1
                if args.count is not None and cnt >= args.count:
                    print(str(args.count) + " measurements collected. Exiting in a moment.")
                    p.disconnect()
                    time.sleep(5)
                    #It seems that sometimes bluepy-helper remains and thus prevents a reconnection, so we try killing our own bluepy-helper
                    pstree=os.popen("pstree -p " + str(pid)).read() #we want to kill only bluepy from our own process tree, because other python scripts have there own bluepy-helper process
                    bluepypid=0
                    try:
                        bluepypid=re.findall(r'bluepy-helper\((.*)\)',pstree)[0] #Store the bluepypid, to kill it later
                    except IndexError: #Should normally occur because we're disconnected
                        logging.debug("Couldn't find pid of bluepy-helper")
                    if bluepypid != 0:
                        os.system("kill " + bluepypid)
                        logging.debug("Killed bluepy with pid: " + str(bluepypid))
                    os._exit(0)
                print("")
                continue
        except Exception as e:
            print("Connection lost")
            connectionLostCounter +=1
            if connected is True: #First connection abort after connected
                unconnectedTime=int(time.time())
                connected=False
            if args.unreachable_count != 0 and connectionLostCounter >= args.unreachable_count:
                print("Maximum numbers of unsuccessful connections reaches, exiting")
                os._exit(0)
            time.sleep(1)
            logging.debug(e)
            logging.debug(traceback.format_exc())		
            
        print ("Waiting...")
        # Perhaps do something else here

elif args.atc:
    print("Script started in ATC Mode")
    print("----------------------------")
    print("In this mode all devices within reach are read out, unless a devicelistfile and --onlydevicelist is specified.")
    print("Also --name Argument is ignored, if you require names, please use --devicelistfile.")
    print("In this mode rounding and debouncing are not available, since ATC firmware sends out only one decimal place.")
    print("ATC mode usually requires root rights. If you want to use it with normal user rights, \nplease execute \"sudo setcap cap_net_raw,cap_net_admin+eip $(eval readlink -f `which python3`)\"")
    print("You have to redo this step if you upgrade your python version.")
    print("----------------------------")

    import sys
    import bluetooth._bluetooth as bluez

    from bluetooth_utils import (toggle_device,
                                enable_le_scan, parse_le_advertising_events,
                                disable_le_scan, raw_packet_to_str)

    advCounter=dict() 
    sensors = dict()
    if args.devicelistfile:
        #import configparser
        if not os.path.exists(args.devicelistfile):
            print ("Error specified device list file '",args.devicelistfile,"' not found")
            os._exit(1)
        sensors = configparser.ConfigParser()
        sensors.read(args.devicelistfile)
        #Convert macs in devicelist file to Uppercase
        sensorsnew={}
        for key in sensors:
            sensorsnew[key.upper()] = sensors[key]
        sensors = sensorsnew

    if args.onlydevicelist and not args.devicelistfile:
        print("Error: --onlydevicelist requires --devicelistfile <devicelistfile>")
        os._exit(1)

    dev_id = args.interface  # the bluetooth device is hci0
    toggle_device(dev_id, True)
    
    try:
        sock = bluez.hci_open_dev(dev_id)
    except:
        print("Cannot open bluetooth device %i" % dev_id)
        raise

    enable_le_scan(sock, filter_duplicates=False)

    try:
        prev_data = None

        def le_advertise_packet_handler(mac, adv_type, data, rssi):
            now = time.time()
            global lastBLEPaketReceived
            # if args.watchdogtimer:
            #     lastBLEPaketReceived = now
            lastBLEPaketReceived = now
            data_unpacked = None
            # data_str = raw_packet_to_str(data)

            # print("Saw: {}".format(mac))

            if mac in TARGETS.keys():
                data_unpacked = unpack_xiaomi_payload(data)
                if data_unpacked is None:
                    data_unpacked = unpack_pvvx_payload(data)

                if args.verbose:
                    print(time.strftime("%Y-%m-%d %H:%M:%S"))
                    print(TARGETS[mac].get('name'))
                    print("{}: adv_type:{} data:0x{} len:{}".format(mac, adv_type, data.hex(), len(data)))
                    print("RSSI: {}".format(rssi))
                    print(data_unpacked)
                    print("\n")
                elif args.vverbose:
                    print("{}: adv_type:{} data:0x{} len:{}".format(mac, adv_type, data.hex(), len(data)))
            elif args.vverbose:
                print("{}: adv_type:{} data:0x{} len:{}".format(mac, adv_type, data.hex(), len(data)))

            if data_unpacked is not None: #only if device advertisment could be parsed by parser
                advNumber = data_unpacked['frame_counter']
                if mac in advCounter:
                    lastAdvNumber = advCounter[mac]
                else:
                    lastAdvNumber = None
                if lastAdvNumber == None or lastAdvNumber != advNumber:
                    data_unpacked['mac'] = mac
                    data_unpacked['rssi'] = rssi
                    data_unpacked['reciever'] = HOSTNAME
                    data_unpacked['timestamp'] = now

                    advCounter[mac] = advNumber

                    if args.export:
                        measurements.append(data_unpacked)


        if  args.watchdogtimer:
            keepingLEScanRunningThread = threading.Thread(target=keepingLEScanRunning)
            keepingLEScanRunningThread.start()
            logging.debug("keepingLEScanRunningThread started")


        # Blocking call (the given handler will be called each time a new LE
        # advertisement packet is detected)
        parse_le_advertising_events(sock,
                                    handler=le_advertise_packet_handler,
                                    debug=False)
    except KeyboardInterrupt:
        disable_le_scan(sock)
