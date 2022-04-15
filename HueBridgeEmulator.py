#!/usr/bin/python

from __future__ import print_function

import logging
import os
import signal
import sys
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from time import strftime, sleep
from datetime import datetime, timedelta
from pprint import pprint
from subprocess import check_output
import json, socket, hashlib, urllib2, struct, random
from threading import Thread
from collections import defaultdict
from uuid import getnode as get_mac
from urlparse import urlparse, parse_qs

mac = '%012x' % get_mac()

listen_port = 80  # port real hardware uses is regular http port 80

if os.getenv('HUE_PORT'):
    listen_port = int(os.getenv('HUE_PORT'))

run_service = True

bridge_config = defaultdict(lambda:defaultdict(str))
sensors_state = {}

logger = logging.getLogger('hue')
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)

handler.setLevel(logging.INFO)
logger.setLevel(logging.INFO)

#load config files
def get_config_from_file():
    try:
        with open('config.json', 'r') as fp:
            result = json.load(fp)
            logger.info("config loaded")
            return result
    except Exception:
        logger.exception("config file was not loaded")


def load_config():
    global bridge_config
    bridge_config = get_config_from_file()


def generate_sensors_state():
    for sensor in bridge_config["sensors"]:
        if sensor not in sensors_state and "state" in bridge_config["sensors"][sensor]:
            sensors_state[sensor] = {"state": {}}
            for key in bridge_config["sensors"][sensor]["state"].keys():
                if key in ["lastupdated", "presence", "flag", "dark", "status"]:
                    sensors_state[sensor]["state"].update({key: "2017-01-01T00:00:00"})

generate_sensors_state()

def get_ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", listen_port))
    return s.getsockname()[0]

bridge_config["config"]["ipaddress"] = get_ip_address()
bridge_config["config"]["mac"] = mac[0] + mac[1] + ":" + mac[2] + mac[3] + ":" + mac[4] + mac[5] + ":" + mac[6] + mac[7] + ":" + mac[8] + mac[9] + ":" + mac[10] + mac[11]
bridge_config["config"]["bridgeid"] = mac.upper()

def save_config():
    with open('config.json', 'w') as fp:
        json.dump(bridge_config, fp, sort_keys=True, indent=4, separators=(',', ': '))

def ssdp_search():
    SSDP_ADDR = '239.255.255.250'
    SSDP_PORT = 1900
    MSEARCH_Interval = 2
    multicast_group_c = SSDP_ADDR
    multicast_group_s = (SSDP_ADDR, SSDP_PORT)
    server_address = ('', SSDP_PORT)
    Response_message = 'HTTP/1.1 200 OK\r\nHOST: 239.255.255.250:1900\r\nEXT:\r\nCACHE-CONTROL: max-age=100\r\nLOCATION: http://' + get_ip_address() + ':' + str(listen_port) + '/description.xml\r\nSERVER: Linux/3.14.0 UPnP/1.0 IpBridge/1.16.0\r\nhue-bridgeid: ' + mac.upper() + '\r\nST: urn:schemas-upnp-org:device:basic:1\r\nUSN: uuid:2f402f80-da50-11e1-9b23-' + mac
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(server_address)

    group = socket.inet_aton(multicast_group_c)
    mreq = struct.pack('4sL', group, socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print("starting ssdp...")

    while run_service:
              data, address = sock.recvfrom(1024)
              if data[0:19]== 'M-SEARCH * HTTP/1.1':
                   if data.find("ssdp:all") != -1:
                          sleep(random.randrange(0, 3))
                          print("Sending M Search response")
                          sock.sendto(Response_message, address)
              sleep(1)
    sock.close()


def scheduler_processor():
    while run_service:
        try:
            process_rules()
        except Exception:
            logger.exception("Error during processing rules.")
        rules_processor(True)
        sleep(1)


def process_rules():
    for schedule in bridge_config["schedules"].keys():
        if bridge_config["schedules"][schedule]["status"] == "enabled":
            if bridge_config["schedules"][schedule]["localtime"].startswith("W"):
                pices = bridge_config["schedules"][schedule]["localtime"].split('/T')
                if int(pices[0][1:]) & (1 << 6 - datetime.today().weekday()):
                    if pices[1] == datetime.now().strftime("%H:%M:%S"):
                        print("execute schedule: " + schedule)
                        sendRequest(bridge_config["schedules"][schedule]["command"]["address"],
                                    bridge_config["schedules"][schedule]["command"]["method"],
                                    json.dumps(bridge_config["schedules"][schedule]["command"]["body"]))
            elif bridge_config["schedules"][schedule]["localtime"].startswith("PT"):
                if bridge_config["schedules"][schedule]["starttime"] == datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"):
                    print("execute timmer: " + schedule)
                    sendRequest(bridge_config["schedules"][schedule]["command"]["address"],
                                bridge_config["schedules"][schedule]["command"]["method"],
                                json.dumps(bridge_config["schedules"][schedule]["command"]["body"]))
                    bridge_config["schedules"][schedule]["status"] = "disabled"
            else:
                if bridge_config["schedules"][schedule]["localtime"] == datetime.now().strftime("%Y-%m-%dT%H:%M:%S"):
                    print("execute schedule: " + schedule)
                    sendRequest(bridge_config["schedules"][schedule]["command"]["address"],
                                bridge_config["schedules"][schedule]["command"]["method"],
                                json.dumps(bridge_config["schedules"][schedule]["command"]["body"]))
    if (datetime.now().strftime("%M:%S") == "00:00"):  # auto save configuration every hour
        save_config()


def rules_processor(scheduler=False):
    bridge_config["config"]["localtime"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") #required for operator dx to address /config/localtime
    for rule in bridge_config["rules"].keys():
        if bridge_config["rules"][rule]["status"] == "enabled":
            execute = True
            for condition in bridge_config["rules"][rule]["conditions"]:
                url_pices = condition["address"].split('/')
                if condition["operator"] == "eq":
                    if condition["value"] == "true":
                        if not bridge_config[url_pices[1]][url_pices[2]][url_pices[3]][url_pices[4]]:
                            execute = False
                    elif condition["value"] == "false":
                        if bridge_config[url_pices[1]][url_pices[2]][url_pices[3]][url_pices[4]]:
                            execute = False
                    else:
                        if not int(bridge_config[url_pices[1]][url_pices[2]][url_pices[3]][url_pices[4]]) == int(condition["value"]):
                            execute = False
                elif condition["operator"] == "gt":
                    if not int(bridge_config[url_pices[1]][url_pices[2]][url_pices[3]][url_pices[4]]) > int(condition["value"]):
                        execute = False
                elif condition["operator"] == "lt":
                    if int(not bridge_config[url_pices[1]][url_pices[2]][url_pices[3]][url_pices[4]]) < int(condition["value"]):
                        execute = False
                elif condition["operator"] == "dx":
                    if not sensors_state[url_pices[2]][url_pices[3]][url_pices[4]] == datetime.now().strftime("%Y-%m-%dT%H:%M:%S"):
                        execute = False
                elif condition["operator"] == "ddx":
                    if not scheduler:
                        execute = False
                    else:
                        ddx_time = datetime.strptime(condition["value"],"PT%H:%M:%S")
                        if not (datetime.strptime(sensors_state[url_pices[2]][url_pices[3]][url_pices[4]],"%Y-%m-%dT%H:%M:%S") + timedelta(hours=ddx_time.hour, minutes=ddx_time.minute, seconds=ddx_time.second)) == datetime.now().replace(microsecond=0):
                            execute = False
                elif condition["operator"] == "in":
                    periods = condition["value"].split('/')
                    if condition["value"][0] == "T":
                        timeStart = datetime.strptime(periods[0], "T%H:%M:%S").time()
                        timeEnd = datetime.strptime(periods[1], "T%H:%M:%S").time()
                        now_time = datetime.now().time()
                        if timeStart < timeEnd:
                            if not timeStart <= now_time <= timeEnd:
                                execute = False
                        else:
                            if not (timeStart <= now_time or now_time <= timeEnd):
                                execute = False

            if execute:
                print("rule " + rule + " is triggered")
                for action in bridge_config["rules"][rule]["actions"]:
                    Thread(target=sendRequest, args=["/api/" + bridge_config["rules"][rule]["owner"] + action["address"], action["method"], json.dumps(action["body"])]).start()

def sendRequest(url, method, data, time_out=3):
    if not url.startswith( 'http://' ):
        url = "http://127.0.0.1" + url
    opener = urllib2.build_opener(urllib2.HTTPHandler)
    request = urllib2.Request(url, data=data)
    request.add_header("Content-Type",'application/json')
    request.get_method = lambda: method
    response = opener.open(request, timeout=time_out).read()
    return response


def sendLightRequest(light, data):
    print("Update light " + light + " with " + json.dumps(data))


def update_group_stats(light): #set group stats based on lights status in that group
    for group in bridge_config["groups"]:
        if light in bridge_config["groups"][group]["lights"]:
            for key, value in bridge_config["lights"][light]["state"].items():
                if key not in ["on", "reachable"]:
                    bridge_config["groups"][group]["action"][key] = value
            any_on = False
            all_on = True
            bri = 0
            for group_light in bridge_config["groups"][group]["lights"]:
                if bridge_config["lights"][light]["state"]["on"] == True:
                    any_on = True
                else:
                    all_on = False
                bri += bridge_config["lights"][light]["state"]["bri"]
            avg_bri = bri / len(bridge_config["groups"][group]["lights"])
            bridge_config["groups"][group]["state"] = {"any_on": any_on, "all_on": all_on, "bri": avg_bri, "lastupdated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")}


def scan_for_lights(): #scan for ESP8266 lights and strips
    print(json.dumps([{"success": {"/lights": "Searching for new devices"}}], sort_keys=True, indent=4, separators=(',', ': ')))


def description():
    return """<root xmlns=\"urn:schemas-upnp-org:device-1-0\">
<specVersion>
<major>1</major>
<minor>0</minor>
</specVersion>
<URLBase>http://""" + get_ip_address() + """:""" + str(listen_port) + """/</URLBase>
<device>
<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
<friendlyName>Philips hue</friendlyName>
<manufacturer>Royal Philips Electronics</manufacturer>
<manufacturerURL>http://www.philips.com</manufacturerURL>
<modelDescription>Philips hue Personal Wireless Lighting</modelDescription>
<modelName>Philips hue bridge 2015</modelName>
<modelNumber>BSB002</modelNumber>
<modelURL>http://www.meethue.com</modelURL>
<serialNumber>""" + mac.upper() + """</serialNumber>
<UDN>MYUUID</UDN>
<presentationURL>index.html</presentationURL>
<iconList>
<icon>
<mimetype>image/png</mimetype>
<height>48</height>
<width>48</width>
<depth>24</depth>
<url>hue_logo_0.png</url>
</icon>
<icon>
<mimetype>image/png</mimetype>
<height>120</height>
<width>120</width>
<depth>24</depth>
<url>hue_logo_3.png</url>
</icon>
</iconList>
</device>
</root>"""

class S(BaseHTTPRequestHandler):
    def _set_headers(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

    def do_GET(self):
        self._set_headers()
        if self.path == '/description.xml':
            self.wfile.write(description())
        else:
            url_pices = self.path.split('/')
            if url_pices[2] in bridge_config["config"]["whitelist"]: #if username is in whitelist
                bridge_config["config"]["UTC"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
                bridge_config["config"]["localtime"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                if len(url_pices) == 3: #print entire config
                    self.wfile.write(json.dumps(bridge_config))
                elif len(url_pices) == 4: #print specified object config
                    self.wfile.write(json.dumps(bridge_config[url_pices[3]]))
                elif len(url_pices) == 5:
                    if url_pices[4] == "new": #return new lights and sensors only
                        self.wfile.write(json.dumps({"lastscan": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}))
                    else:
                        self.wfile.write(json.dumps(bridge_config[url_pices[3]][url_pices[4]]))
                elif len(url_pices) == 6:
                    self.wfile.write(json.dumps(bridge_config[url_pices[3]][url_pices[4]][url_pices[5]]))
            elif (url_pices[2] == "nouser" or url_pices[2] == "config"): #used by applications to discover the bridge
                self.wfile.write(json.dumps({"name": bridge_config["config"]["name"],"datastoreversion": 59, "swversion": bridge_config["config"]["swversion"], "apiversion": bridge_config["config"]["apiversion"], "mac": bridge_config["config"]["mac"], "bridgeid": bridge_config["config"]["bridgeid"], "factorynew": False, "modelid": bridge_config["config"]["modelid"]}))
            else: #user is not in whitelist
                self.wfile.write(json.dumps([{"error": {"type": 1, "address": self.path, "description": "unauthorized user" }}]))


    def do_POST(self):
        self._set_headers()
        print("in post method")
        self.data_string = self.rfile.read(int(self.headers['Content-Length']))
        post_dictionary = json.loads(self.data_string)
        url_pices = self.path.split('/')
        print(self.path)
        print(self.data_string)
        if len(url_pices) == 4: #data was posted to a location
            if url_pices[2] in bridge_config["config"]["whitelist"]:
                if ((url_pices[3] == "lights" or url_pices[3] == "sensors") and not bool(post_dictionary)):
                    #if was a request to scan for lights of sensors
                    Thread(target=scan_for_lights).start()
                    sleep(7) #give no more than 7 seconds for light scanning (otherwise will face app disconnection timeout)
                    self.wfile.write(json.dumps([{"success": {"/" + url_pices[3]: "Searching for new devices"}}]))
                else: #create object
                    # find the first unused id for new object
                    i = 1
                    while (str(i)) in bridge_config[url_pices[3]]:
                        i += 1
                    if url_pices[3] == "scenes":
                        post_dictionary.update({"lightstates": {}, "version": 2, "picture": "", "lastupdated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
                    elif url_pices[3] == "groups":
                        post_dictionary.update({"action": {"on": False}, "state": {"any_on": False, "all_on": False}})
                    elif url_pices[3] == "schedules":
                        post_dictionary.update({"created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})
                        if post_dictionary["localtime"].startswith("PT"):
                            timmer = post_dictionary["localtime"][2:]
                            (h, m, s) = timmer.split(':')
                            d = timedelta(hours=int(h), minutes=int(m), seconds=int(s))
                            post_dictionary.update({"starttime": (datetime.utcnow() + d).strftime("%Y-%m-%dT%H:%M:%S")})
                        if not "status" in post_dictionary:
                            post_dictionary.update({"status": "enabled"})
                    elif url_pices[3] == "rules":
                        post_dictionary.update({"owner": url_pices[2]})
                        if not "status" in post_dictionary:
                            post_dictionary.update({"status": "enabled"})
                    elif url_pices[3] == "sensors":
                        if post_dictionary["modelid"] == "PHWA01":
                            post_dictionary.update({"state": {"status": 0}})
                    generate_sensors_state()
                    bridge_config[url_pices[3]][str(i)] = post_dictionary
                    print(json.dumps([{"success": {"id": str(i)}}], sort_keys=True, indent=4, separators=(',', ': ')))
                    self.wfile.write(json.dumps([{"success": {"id": str(i)}}], sort_keys=True, indent=4, separators=(',', ': ')))
            else:
                self.wfile.write(json.dumps([{"error": {"type": 1, "address": self.path, "description": "unauthorized user" }}],sort_keys=True, indent=4, separators=(',', ': ')))
                print(json.dumps([{"error": {"type": 1, "address": self.path, "description": "unauthorized user" }}],sort_keys=True, indent=4, separators=(',', ': ')))
        elif "devicetype" in post_dictionary: #this must be a new device registration
                #create new user hash
                s = hashlib.new('ripemd160', post_dictionary["devicetype"][0]        ).digest()
                username = s.encode('hex')
                bridge_config["config"]["whitelist"][username] = {"last use date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),"create date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),"name": post_dictionary["devicetype"]}
                self.wfile.write(json.dumps([{"success": {"username": username}}], sort_keys=True, indent=4, separators=(',', ': ')))
                print(json.dumps([{"success": {"username": username}}], sort_keys=True, indent=4, separators=(',', ': ')))
        self.end_headers()
        save_config()

    def do_PUT(self):
        self._set_headers()
        print("in PUT method")
        self.data_string = self.rfile.read(int(self.headers['Content-Length']))
        put_dictionary = json.loads(self.data_string)
        url_pices = self.path.split('/')
        if url_pices[2] in bridge_config["config"]["whitelist"]:
            if len(url_pices) == 4:
                bridge_config[url_pices[3]].update(put_dictionary)
                response_location = "/" + url_pices[3] + "/"
            if len(url_pices) == 5:
                if url_pices[3] == "schedules":
                    if "status" in put_dictionary and put_dictionary["status"] == "enabled" and bridge_config["schedules"][url_pices[4]]["localtime"].startswith("PT"):
                        if "localtime" in put_dictionary:
                            timmer = put_dictionary["localtime"][2:]
                        else:
                            timmer = bridge_config["schedules"][url_pices[4]]["localtime"][2:]
                        (h, m, s) = timmer.split(':')
                        d = timedelta(hours=int(h), minutes=int(m), seconds=int(s))
                        put_dictionary.update({"starttime": (datetime.utcnow() + d).strftime("%Y-%m-%dT%H:%M:%S")})
                elif url_pices[3] == "scenes":
                    if "storelightstate" in put_dictionary:
                        for light in bridge_config["scenes"][url_pices[4]]["lightstates"]:
                            bridge_config["scenes"][url_pices[4]]["lightstates"][light]["on"] = bridge_config["lights"][light]["state"]["on"]
                            bridge_config["scenes"][url_pices[4]]["lightstates"][light]["bri"] = bridge_config["lights"][light]["state"]["bri"]
                            if "xy" in bridge_config["scenes"][url_pices[4]]["lightstates"][light]:
                                del bridge_config["scenes"][url_pices[4]]["lightstates"][light]["xy"]
                            elif "ct" in bridge_config["scenes"][url_pices[4]]["lightstates"][light]:
                                del bridge_config["scenes"][url_pices[4]]["lightstates"][light]["ct"]
                            elif "hue" in bridge_config["scenes"][url_pices[4]]["lightstates"][light]:
                                del bridge_config["scenes"][url_pices[4]]["lightstates"][light]["hue"]
                                del bridge_config["scenes"][url_pices[4]]["lightstates"][light]["sat"]
                            if bridge_config["lights"][light]["state"]["colormode"] in ["ct", "xy"]:
                                bridge_config["scenes"][url_pices[4]]["lightstates"][light][bridge_config["lights"][light]["state"]["colormode"]] = bridge_config["lights"][light]["state"][bridge_config["lights"][light]["state"]["colormode"]]
                            elif bridge_config["lights"][light]["state"]["colormode"] == "hs":
                                bridge_config["scenes"][url_pices[4]]["lightstates"][light]["hue"] = bridge_config["lights"][light]["state"]["hue"]
                                bridge_config["scenes"][url_pices[4]]["lightstates"][light]["sat"] = bridge_config["lights"][light]["state"]["sat"]

                if url_pices[3] == "sensors":
                    for key, value in put_dictionary.items():
                        bridge_config[url_pices[3]][url_pices[4]][key].update(value)
                else:
                    bridge_config[url_pices[3]][url_pices[4]].update(put_dictionary)
                response_location = "/" + url_pices[3] + "/" + url_pices[4] + "/"
            if len(url_pices) == 6:
                if url_pices[3] == "groups": #state is applied to a group
                    if "scene" in put_dictionary: #if group is 0 and there is a scene applied
                        for light in bridge_config["scenes"][put_dictionary["scene"]]["lights"]:
                            bridge_config["lights"][light]["state"].update(bridge_config["scenes"][put_dictionary["scene"]]["lightstates"][light])
                            if "xy" in bridge_config["scenes"][put_dictionary["scene"]]["lightstates"][light]:
                                bridge_config["lights"][light]["state"]["colormode"] = "xy"
                            elif "ct" in bridge_config["scenes"][put_dictionary["scene"]]["lightstates"][light]:
                                bridge_config["lights"][light]["state"]["colormode"] = "ct"
                            elif "hue" or "sat" in bridge_config["scenes"][put_dictionary["scene"]]["lightstates"][light]:
                                bridge_config["lights"][light]["state"]["colormode"] = "hs"
                            Thread(target=sendLightRequest, args=[light, bridge_config["scenes"][put_dictionary["scene"]]["lightstates"][light]]).start()
                            update_group_stats(light)
                    elif "bri_inc" in put_dictionary:
                        bridge_config["groups"][url_pices[4]]["action"]["bri"] += int(put_dictionary["bri_inc"])
                        if bridge_config["groups"][url_pices[4]]["action"]["bri"] > 254:
                            bridge_config["groups"][url_pices[4]]["action"]["bri"] = 254
                        elif bridge_config["groups"][url_pices[4]]["action"]["bri"] < 1:
                            bridge_config["groups"][url_pices[4]]["action"]["bri"] = 1
                        bridge_config["groups"][url_pices[4]]["state"]["bri"] = bridge_config["groups"][url_pices[4]]["action"]["bri"]
                        del put_dictionary["bri_inc"]
                        put_dictionary.update({"bri": bridge_config["groups"][url_pices[4]]["action"]["bri"]})
                        for light in bridge_config["groups"][url_pices[4]]["lights"]:
                            bridge_config["lights"][light]["state"].update(put_dictionary)
                            Thread(target=sendLightRequest, args=[light, put_dictionary]).start()
                    elif url_pices[4] == "0":
                        for light in bridge_config["lights"].keys():
                            bridge_config["lights"][light]["state"].update(put_dictionary)
                            Thread(target=sendLightRequest, args=[light, put_dictionary]).start()
                        for group in bridge_config["groups"].keys():
                            bridge_config["groups"][group][url_pices[5]].update(put_dictionary)
                            if "on" in put_dictionary:
                                bridge_config["groups"][group]["state"]["any_on"] = put_dictionary["on"]
                                bridge_config["groups"][group]["state"]["all_on"] = put_dictionary["on"]
                    else: # the state is applied to particular group (url_pices[4])
                        if "on" in put_dictionary:
                            bridge_config["groups"][url_pices[4]]["state"]["any_on"] = put_dictionary["on"]
                            bridge_config["groups"][url_pices[4]]["state"]["all_on"] = put_dictionary["on"]
                        for light in bridge_config["groups"][url_pices[4]]["lights"]:
                                bridge_config["lights"][light]["state"].update(put_dictionary)
                                Thread(target=sendLightRequest, args=[light, put_dictionary]).start()
                elif url_pices[3] == "lights": #state is applied to a light
                    Thread(target=sendLightRequest, args=[url_pices[4], put_dictionary]).start()
                    for key in put_dictionary.keys():
                        if key in ["ct", "xy"]: #colormode must be set by bridge
                            bridge_config["lights"][url_pices[4]]["state"]["colormode"] = key
                        elif key in ["hue", "sat"]:
                            bridge_config["lights"][url_pices[4]]["state"]["colormode"] = "hs"
                    update_group_stats(url_pices[4])
                if not url_pices[4] == "0": #group 0 is virtual, must not be saved in bridge configuration
                    try:
                        bridge_config[url_pices[3]][url_pices[4]][url_pices[5]].update(put_dictionary)
                    except KeyError:
                        bridge_config[url_pices[3]][url_pices[4]][url_pices[5]] = put_dictionary
                if url_pices[3] == "sensors" and url_pices[5] == "state":
                    for key in put_dictionary.keys():
                        sensors_state[url_pices[4]]["state"].update({key: datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})
                    if "flag" in put_dictionary: #if a scheduler change te flag of a logical sensor then process the rules.
                        rules_processor()
                response_location = "/" + url_pices[3] + "/" + url_pices[4] + "/" + url_pices[5] + "/"
            if len(url_pices) == 7:
                try:
                    bridge_config[url_pices[3]][url_pices[4]][url_pices[5]][url_pices[6]].update(put_dictionary)
                except KeyError:
                    bridge_config[url_pices[3]][url_pices[4]][url_pices[5]][url_pices[6]] = put_dictionary
                bridge_config[url_pices[3]][url_pices[4]][url_pices[5]][url_pices[6]] = put_dictionary
                response_location = "/" + url_pices[3] + "/" + url_pices[4] + "/" + url_pices[5] + "/" + url_pices[6] + "/"
            response_dictionary = []
            for key, value in put_dictionary.items():
                response_dictionary.append({"success":{response_location + key: value}})
            self.wfile.write(json.dumps(response_dictionary,sort_keys=True, indent=4, separators=(',', ': ')))
            print(json.dumps(response_dictionary, sort_keys=True, indent=4, separators=(',', ': ')))
        else:
            self.wfile.write(json.dumps([{"error": {"type": 1, "address": self.path, "description": "unauthorized user" }}],sort_keys=True, indent=4, separators=(',', ': ')))

    def do_DELETE(self):
        self._set_headers()
        url_pices = self.path.split('/')
        if url_pices[2] in bridge_config["config"]["whitelist"]:
            del bridge_config[url_pices[3]][url_pices[4]]
            self.wfile.write(json.dumps([{"success": "/" + url_pices[3] + "/" + url_pices[4] + " deleted."}]))

def run(server_class=HTTPServer, handler_class=S):
    server_address = ('', listen_port)
    httpd = server_class(server_address, handler_class)
    print('Starting httpd on %d...' % listen_port)
    httpd.serve_forever()


def exit_handler(*args):
    logger.error("Signal received. Stopping service!")
    global run_service
    run_service = False

def reload_config_handler(*args):
    logger.info("Reloading config")
    load_config()


if __name__ == "__main__":
    try:
        load_config()
        signal.signal(signal.SIGTERM, exit_handler)
        signal.signal(signal.SIGINT, exit_handler)
        signal.signal(signal.SIGUSR1, reload_config_handler)
        if os.getenv('RUN_SSDP') != 'n':
            Thread(target=ssdp_search).start()
        if os.getenv('RUN_RULES') != 'n':
            Thread(target=scheduler_processor).start()
        while run_service:
            try:
                logger.info('Starting HTTP server...')
                run()
            except Exception:
                logger.exception("Error during processing request")

    except Exception:
        logger.exception("server stopped")
    finally:
        run_service = False
        save_config()
        print('config saved')
