#!/usr/bin/python -u
# -*- coding: utf-8 -*-
import argparse
import dbus
import json
import gobject
import logging
import os
import sys
import traceback
from dbus.mainloop.glib import DBusGMainLoop
from lxml import etree


# Victron packages
AppDir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(1, os.path.join(AppDir, 'ext', 'velib_python'))
from logger import setup_logging
from ve_utils import get_vrm_portal_id, exit_on_error
from mqtt_gobject_bridge import MqttGObjectBridge
from vrm_registrator import VrmRegistrator


SoftwareVersion = '1.02'
ServicePrefix = 'com.victronenergy.'
VeDbusInvalid = dbus.Array([], signature=dbus.Signature('i'), variant_level=1)


class DbusMqtt(MqttGObjectBridge):
	def __init__(self, mqtt_server=None, ca_cert=None, user=None, passwd=None, dbus_address=None,
				keep_alive_interval=None, init_broker=False):
		self._dbus_address = dbus_address
		self._dbus_conn = (dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()) \
			if dbus_address == None \
			else dbus.bus.BusConnection(dbus_address)
		self._dbus_conn.add_signal_receiver(self._dbus_name_owner_changed, signal_name='NameOwnerChanged')
		self._connected_to_cloud = False

		# @todo EV Get portal ID from com.victronenergy.system?
		self._system_id = get_vrm_portal_id()
		# Key: D-BUS Service + path, value: topic
		self._topics = {}
		# Key: topic, value: last value seen on D-Bus
		self._values = {}
		# Key: service_type/device_instance, value: D-Bus service name
		self._services = {}
		# Key: short D-Bus service name (eg. 1:31), value: full D-Bus service name (eg. com.victronenergy.settings)
		self._service_ids = {}

		if init_broker:
			self._registrator = VrmRegistrator(self._system_id, SoftwareVersion)
			self._registrator.register()
		else:
			self._registrator = None

		self._dbus_conn.add_signal_receiver(self._on_dbus_value_changed,
			dbus_interface='com.victronenergy.BusItem', signal_name='PropertiesChanged', path_keyword='path',
			sender_keyword='service_id')
		services = self._dbus_conn.list_names()
		for service in services:
			if service.startswith('com.victronenergy.'):
				self._service_ids[self._dbus_conn.get_name_owner(service)] = service
				self._scan_dbus_service(service, publish=False)

		# Bus scan may take a log time, so start keep alive after scan
		self._keep_alive_interval = keep_alive_interval
		self._keep_alive_timer = None

		MqttGObjectBridge.__init__(self, "ve/dbus-mqtt-py", mqtt_server, ca_cert, user, passwd)

	def _publish(self, topic, value, reset=False):
		if self._socket_watch == None:
			return
		if self._keep_alive_interval != None and self._keep_alive_timer == None:
			# Keep alive enabled, but timer ran out, so no publishes except for system serial
			if reset or not topic.endswith('/system/0/Serial'):
				return
		if reset and topic.endswith('/system/0/Serial'):
			return
		# Publish None when service disappears: the topic will no longer show up when subscribing.
		# Clients which are already subscribed will receive a single message with empty payload.
		payload = None if reset else json.dumps(dict(value=value))
		self._client.publish(topic, payload, retain=True)

	def _publish_all(self, reset=False):
		keys = self._values.keys()
		keys.sort()
		for topic in keys:
			value = self._values[topic]
			self._publish(topic, value, reset=reset)

	def _on_connect(self, client, userdata, dict, rc):
		MqttGObjectBridge._on_connect(self, client, userdata, dict, rc)
		logging.info('[Connected] Result code {}'.format(rc))
		self._client.subscribe('R/{}/#'.format(self._system_id), 0)
		self._client.subscribe('W/{}/#'.format(self._system_id), 0)
		if self._registrator != None and self._registrator.client_id != None:
			self._client.subscribe('$SYS/broker/connection/{}/state'.format(self._registrator.client_id), 0)
		# Send all values at once, because values may have changed when we were disconnected.
		self._publish_all()

	def _on_message(self, client, userdata, msg):
		MqttGObjectBridge._on_message(self, client, userdata, msg)
		if msg.topic.startswith('$SYS/broker/connection/'):
			if int(msg.payload) == 1:
				logging.info('[Message] Connected to cloud broker')
				self._connected_to_cloud = True
			elif self._connected_to_cloud:
				# As long as we have connection with the cloud server, we do not have to worry about
				# authentication. After connection loss, we have to authenticate again, which is a nice
				# moment to initialize the broker again in case our remote_password has been reset on the
				# server, or if someone has unlinks VRM page.
				logging.error('[Message] Lost connection with cloud broker')
				self._connected_to_cloud = False
				self._registrator.register()
			return
		refresh_keep_valid = False
		try:
			logging.debug('[Request] {}: {}'.format(msg.topic, str(msg.payload)))
			action, system_id, path = msg.topic.split('/', 2)
			if system_id != self._system_id:
				raise Exception('Unknown system id')
			refresh_keep_valid = True
			topic = 'N/{}/{}'.format(system_id, path)
			if action == 'W':
				self._handle_write(topic, msg.payload)
			elif action == 'R':
				self._handle_read(topic)
		except:
			logging.error('[Request] Error in request: {} {}'.format(msg.topic, msg.payload))
			traceback.print_exc()
		# Make sure we refresh keep-alive even if the handle read/write failed. The client may request a
		# value that is temporarily unavailable.
		if refresh_keep_valid:
			self._refresh_keep_alive()

	def _handle_write(self, topic, payload):
		logging.debug('[Write] Writing {} to {}'.format(payload, topic))
		value = json.loads(payload)['value']
		service, path = self._get_uid_by_topic(topic)
		self._set_dbus_value(service, path, value)

	def _handle_read(self, topic):
		logging.debug('[Read] Topic {}'.format(topic))
		self._get_uid_by_topic(topic)
		value = self._values[topic]
		self._publish(topic, value)

	def _get_uid_by_topic(self, topic):
		action, system_id, service_type, device_instance, path = topic.split('/', 4)
		device_instance = int(device_instance)
		service = self._services.get('{}/{}'.format(service_type, device_instance))
		if service == None:
			raise Exception('Unknown service')
		self._add_item(service, device_instance, path, False)
		return service, '/' + path

	def _dbus_name_owner_changed(self, name, oldowner, newowner):
		if not name.startswith('com.victronenergy.'):
			return
		if newowner != '':
			self._scan_dbus_service(name)
			self._service_ids[newowner] = name
		elif oldowner != '':
			logging.info('[OwnerChange] Service disappeared: {}'.format(name))
			for path, topic in self._topics.items():
				if path.startswith(name + '/'):
					self._publish(topic, None, reset=True)
					del self._topics[path]
					del self._values[topic]
			if name in self._services:
				del self._services[name]
			if oldowner in self._service_ids:
				del self._service_ids[oldowner]

	def _scan_dbus_service(self, service, publish=True):
		try:
			logging.info('[Scanning] service: {}'.format(service))
			try:
				device_instance = int(self._get_dbus_value(service, '/DeviceInstance'))
			except dbus.exceptions.DBusException as e:
				if e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownObject' or \
					e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownMethod':
					device_instance = 0
				else:
					raise
			except TypeError:
				device_instance = 0
			short_service_name = get_short_service_name(service, device_instance)
			self._services[short_service_name] = service
			try:
				items = self._get_dbus_value(service, '/')
			except dbus.exceptions.DBusException as e:
				if e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownObject' or \
					e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownMethod':
					self._introspect(service, device_instance, '/', publish)
					logging.warn('[Scanning] {} does not provide an item listing'.format(service))
					return
				else:
					raise
			for path, value in items.items():
				self._add_item(service, device_instance, path, value=unwrap_dbus_value(value), publish=publish, get_value=False)
		except dbus.exceptions.DBusException,e:
			if e.get_dbus_name() == 'org.freedesktop.DBus.Error.ServiceUnknown' or \
				e.get_dbus_name() == 'org.freedesktop.DBus.Error.Disconnected':
				logger.info("[Scanning] Service disappeared while being scanned: %s", service)
			elif e.get_dbus_name() == 'org.freedesktop.DBus.Error.NoReply':
				logger.info("[Scanning] No response from service during scan: %s", service)
			else:
				raise

	def _introspect(self, service, device_instance, path, publish=True):
		value = self._dbus_conn.call_blocking(service, path, None, 'Introspect', '', [])
		tree = etree.fromstring(value)
		nodes = tree.findall('node')
		if len(nodes) == 0:
			for iface in tree.findall('interface'):
				if iface.attrib.get('name') == 'com.victronenergy.BusItem':
					self._add_item(service, device_instance, path, publish=publish)
		else:
			for child in nodes:
				name = child.attrib.get('name')
				if name != None:
					if path.endswith('/'):
						p = path + name
					else:
						p = path + '/' + name
					self._introspect(service, device_instance, p, publish=publish)

	def _on_dbus_value_changed(self, changes, path=None, service_id=None):
		service = self._service_ids.get(service_id)
		if service == None:
			return
		uid = service + path
		topic = self._topics.get(uid)
		if topic == None:
			for service_short_name, service_name in self._services.items():
				if service_name == service:
					device_instance = service_short_name.split('/')[1]
					self._add_item(service, device_instance, path, publish=False, get_value=False)
					logging.info('New item found: {}{}'.format(service_short_name, path))
					topic = self._topics[uid]
					break
			else:
				return
		value = changes.get("Value")
		if value == None:
			return
		value = unwrap_dbus_value(value)
		self._values[topic] = value
		self._publish(topic, value)

	def _add_item(self, service, device_instance, path, value=None, publish=True, get_value=True):
		if not path.startswith('/'):
			path = '/' + path
		uid = service + path
		r = self._topics.get(uid)
		if r != None:
			return
		if get_value:
			value = self._get_dbus_value(service, path)
		topic = 'N/{}/{}/{}{}'.format(self._system_id, get_service_type(service), device_instance, path)
		self._topics[uid] = topic
		self._values[topic] = value
		if publish:
			self._publish(topic, value)

	def _get_dbus_value(self, service, path):
		value = self._dbus_conn.call_blocking(service, path, None, 'GetValue', '', [])
		return unwrap_dbus_value(value)

	def _set_dbus_value(self, service, path, value):
		value = wrap_dbus_value(value)
		return self._dbus_conn.call_blocking(service, path, None, 'SetValue', 'v', [value])

	def _on_keep_alive_timeout(self):
		logging.info('[KeepAlive] Timer trigger, changes are no longer published')
		self._publish_all(reset=True)
		self._keep_alive_timer = None

	def _refresh_keep_alive(self):
		if self._keep_alive_interval == None:
			return
		restart = False
		if self._keep_alive_timer == None:
			logging.info('[KeepAlive] Received request, publishing restarted')
			restart = True
		else:
			gobject.source_remove(self._keep_alive_timer)
		self._keep_alive_timer = gobject.timeout_add_seconds(
			self._keep_alive_interval, exit_on_error, self._on_keep_alive_timeout)
		if restart:
			# Do this after self._keep_alive_timer is set, because self._publish used it check if it should
			# publish
			self._publish_all()


def wrap_dbus_value(value):
	if value == None:
		return VeDbusInvalid
	if isinstance(value, float):
		return dbus.Double(value, variant_level=1)
	if isinstance(value, bool):
		return dbus.Boolean(value, variant_level=1)
	if isinstance(value, int):
		return dbus.Int32(value, variant_level=1)
	if isinstance(value, str):
		return dbus.String(value, variant_level=1)
	if isinstance(value, list):
		return dbus.Array([wrap_dbus_value(x) for x in value], variant_level=1)
	if isinstance(value, long):
		return dbus.Int64(value, variant_level=1)
	if isinstance(value, dict):
		return dbus.Dictionary({(wrap_dbus_value(k), wrap_dbus_value(v)) for k,v in value.items()}, variant_level=1)
	return value


def unwrap_dbus_value(val):
	''''Converts D-Bus values back to the original type. For example if val is of type DBus.Double,
	a float will be returned.'''
	if isinstance(val, dbus.Double):
		return float(val)
	if isinstance(val, (dbus.Byte, dbus.Int16, dbus.UInt16, dbus.Int32, dbus.UInt32, dbus.Int64, dbus.UInt64)):
		return int(val)
	if isinstance(val, dbus.Array):
		v = [unwrap_dbus_value(x) for x in val]
		return None if len(v) == 0 else v
	if isinstance(val, (dbus.Signature, dbus.String)):
		return unicode(val)
	if isinstance(val, dbus.ByteArray):
		return "".join([str(x) for x in val])
	if isinstance(val, (list, tuple)):
		return [unwrap_dbus_value(x) for x in val]
	if isinstance(val, (dbus.Dictionary, dict)):
		return dict([(unwrap_dbus_value(x), unwrap_dbus_value(y)) for x,y in val.items()])
	if isinstance(val, dbus.Boolean):
		return bool(val)
	return val


def get_service_type(service_name):
	if not service_name.startswith(ServicePrefix):
		raise Exception('No victron service')
	return service_name.split('.')[2]


def get_service_base_name(service_name):
	if not service_name.startswith(ServicePrefix):
		raise Exception('No victron service')
	return '.'.join(service_name.split('.')[0:3])


def get_short_service_name(service, device_instance):
	return '{}/{}'.format(get_service_type(service), device_instance)


def get_random_string(size=32):
	'''Creates a random (hex) string which contains 'size' characters.'''
	return ''.join("{0:02x}".format(ord(b)) for b in open('/dev/urandom', 'rb').read(size/2))


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Publishes values from the D-Bus to an MQTT broker')
	parser.add_argument('-d', '--debug', help='set logging level to debug', action='store_true')
	parser.add_argument('-q', '--mqtt-server', nargs='?', default=None, help='name of the mqtt server')
	parser.add_argument('-u', '--mqtt-user', default=None, help='mqtt user name')
	parser.add_argument('-P', '--mqtt-password', default=None, help='mqtt password')
	parser.add_argument('-c', '--mqtt-certificate', default=None, help='path to CA certificate used for SSL communication')
	parser.add_argument('-b', '--dbus', default=None, help='dbus address')
	parser.add_argument('-k', '--keep-alive', default=60, help='keep alive interval in seconds', type=int)
	parser.add_argument('-i', '--init-broker', action='store_true', help='Tries to setup communication with VRM MQTT broker')
	args = parser.parse_args()

	print("-------- dbus_mqtt, v{} is starting up --------".format(SoftwareVersion))
	logger = setup_logging(args.debug)

	# This allows us to use gobject code in new threads
	# gobject.threads_init()
	mainloop = gobject.MainLoop()
	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)
	keep_alive_interval = args.keep_alive if args.keep_alive > 0 else None
	handler = DbusMqtt(
		mqtt_server=args.mqtt_server, ca_cert=args.mqtt_certificate, user=args.mqtt_user,
		passwd=args.mqtt_password, dbus_address=args.dbus, keep_alive_interval=keep_alive_interval,
		init_broker=args.init_broker)
	# Start and run the mainloop
	mainloop.run()
