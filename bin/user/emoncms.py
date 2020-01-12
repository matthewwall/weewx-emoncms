# Copyright 2013-2020 Matthew Wall

"""
Emoncms is a powerful open-source web-app for processing, logging and
visualising energy, temperature and other environmental data.

http://emoncms.org

This is a weewx extension that uploads data to an EmonCMS server.

Minimal Configuration

A read/write token is required.  The default configuration will upload to
emoncms.org.  All weewx variables will be uploaded using weewx names and
default units and formatting.

[StdRESTful]
    [[EmonCMS]]
        token = TOKEN

Customized Configuration

When an input map is specified, only variables in that map will be uploaded.
The 'units' parameter can be used to specify which units should be used for
the input, independent of the local weewx units.

[StdRESTful]
    [[EmonCMS]]
        token = TOKEN
        prefix = weather
        server_url = http://192.168.0.1/emoncms/input/post.json
        [[[inputs]]]
            [[[[barometer]]]]
                units = inHg
                name = barometer_inHg
                format = %.3f
            [[[[outTemp]]]]
                units = degree_F
                name = outTemp_F
                format = %.1f
            [[[[outHumidity]]]]
                name = outHumidity
                format = %03.0f
            [[[[windSpeed]]]]
                units = mph
                name = windSpeed_mph
                format = %.2f
            [[[[windDir]]]]
                format = %03.0f
"""

# FIXME: do pattern matching for many similarly-named channels

# support both python2 and python3.  attempt the python3 import first, then
# fallback to python2.
try:
    import queue as Queue
except ImportError:
    import Queue

import re
import sys
import syslog
import urllib
import urllib2

import weewx
import weewx.restx
import weewx.units
from weeutil.weeutil import to_bool, accumulateLeaves

VERSION = "0.15"

if weewx.__version__ < "3":
    raise weewx.UnsupportedFeature("weewx 3 is required, found %s" %
                                   weewx.__version__)

try:
    # weewx4 logging
    import weeutil.logger
    import logging
    log = logging.getLogger(__name__)
    def logdbg(msg):
        log.debug(msg)
    def loginf(msg):
        log.info(msg)
    def logerr(msg):
        log.error(msg)
except ImportError:
    # old-style weewx logging
    import syslog
    def logmsg(level, msg):
        syslog.syslog(level, 'restx: EmonCMS: %s' % msg)
    def logdbg(msg):
        logmsg(syslog.LOG_DEBUG, msg)
    def loginf(msg):
        logmsg(syslog.LOG_INFO, msg)
    def logerr(msg):
        logmsg(syslog.LOG_ERR, msg)


def _obfuscate(s):
    return ('X'*(len(s)-4) + s[-4:])

def _compat(d, old_label, new_label):
    if old_label in d and not new_label in d:
        d.setdefault(new_label, d[old_label])
        d.pop(old_label)

# some unit labels are rather lengthy.  this reduces them to something shorter.
UNIT_REDUCTIONS = {
    'degree_F': 'F',
    'degree_C': 'C',
    'inch': 'in',
    'mile_per_hour': 'mph',
    'mile_per_hour2': 'mph',
    'km_per_hour': 'kph',
    'km_per_hour2': 'kph',
    'meter_per_second': 'mps',
    'meter_per_second2': 'mps',
    'degree_compass': None,
    'watt_per_meter_squared': 'Wpm2',
    'uv_index': None,
    'percent': None,
    'unix_epoch': None,
    }

# return the units label for an observation
def _get_units_label(obs, unit_system):
    (unit_type, _) = weewx.units.getStandardUnitType(unit_system, obs)
    return UNIT_REDUCTIONS.get(unit_type, unit_type)

# get the template for an observation based on the observation key
def _get_template(obs_key, overrides, append_units_label, unit_system):
    tmpl_dict = dict()
    if append_units_label:
        label = _get_units_label(obs_key, unit_system)
        if label is not None:
            tmpl_dict['name'] = "%s_%s" % (obs_key, label)
    for x in ['name', 'format', 'units']:
        if x in overrides:
            tmpl_dict[x] = overrides[x]
    return tmpl_dict


class EmonCMS(weewx.restx.StdRESTbase):
    def __init__(self, engine, config_dict):
        """This service recognizes standard restful options plus the following:

        Required parameters:

        token: unique token for read-write access to emoncms

        Optional parameters:

        node: integer value for emoncms node
        Default is None
        
        prefix: label that will be pre-pended to each variable
        Default is None

        server_url: URL of the server
        Default is emoncms.org

        append_units_label: should units label be appended to name
        Default is True

        obs_to_upload: Which observations to upload.  Possible values are
        none or all.  When none is specified, only items in the inputs list
        will be uploaded.  When all is specified, all observations will be
        uploaded, subject to overrides in the inputs list.
        Default is all

        inputs: dictionary of weewx observation names with optional upload
        name, format, and units
        Default is None
        """
        super(EmonCMS, self).__init__(engine, config_dict)        
        loginf("service version is %s" % VERSION)
        try:
            site_dict = config_dict['StdRESTful']['EmonCMS']
            site_dict = accumulateLeaves(site_dict, max_level=1)
            site_dict['token']
        except KeyError, e:
            logerr("Data will not be uploaded: Missing option %s" % e)
            return

        # for backward compatibility: 'url' is now 'server_url'
        _compat(site_dict, 'url', 'server_url')
        # for backward compatibility: 'station' is now 'prefix'
        _compat(site_dict, 'station', 'prefix')

        site_dict.setdefault('node', 0)
        site_dict.setdefault('append_units_label', True)
        site_dict.setdefault('augment_record', True)
        site_dict.setdefault('obs_to_upload', 'all')
        site_dict['append_units_label'] = to_bool(site_dict.get('append_units_label'))
        site_dict['augment_record'] = to_bool(site_dict.get('augment_record'))

        usn = site_dict.get('unit_system', None)
        if usn is not None:
            site_dict['unit_system'] = weewx.units.unit_constants[usn]

        if 'inputs' in config_dict['StdRESTful']['EmonCMS']:
            site_dict['inputs'] = dict(config_dict['StdRESTful']['EmonCMS']['inputs'])

        # if we are supposed to augment the record with data from weather
        # tables, then get the manager dict to do it.  there may be no weather
        # tables, so be prepared to fail.
        try:
            if site_dict.get('augment_record'):
                _manager_dict = weewx.manager.get_manager_dict_from_config(
                    config_dict, 'wx_binding')
                site_dict['manager_dict'] = _manager_dict
        except weewx.UnknownBinding:
            pass

        self.archive_queue = Queue.Queue()
        self.archive_thread = EmonCMSThread(self.archive_queue, **site_dict)
        self.archive_thread.start()
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

        if 'prefix' in site_dict:
            loginf("prefix is %s" % site_dict['prefix'])
        if 'node' in site_dict and site_dict['node'] is not None:
            loginf("node is %s" % site_dict['node'])
        if usn is not None:
            loginf("desired unit system is %s" % usn)
        loginf("Data will be uploaded with token=%s" %
               _obfuscate(site_dict['token']))

    def new_archive_record(self, event):
        self.archive_queue.put(event.record)

class EmonCMSThread(weewx.restx.RESTThread):

    _DEFAULT_SERVER_URL = 'http://emoncms.org/input/post.json'

    def __init__(self, queue, token,
                 prefix=None, node=None, unit_system=None, augment_record=True,
                 inputs=dict(), obs_to_upload='all', append_units_label=True,
                 server_url=_DEFAULT_SERVER_URL, skip_upload=False,
                 manager_dict=None,
                 post_interval=None, max_backlog=sys.maxint, stale=None,
                 log_success=True, log_failure=True,
                 timeout=60, max_tries=3, retry_wait=5):
        super(EmonCMSThread, self).__init__(queue,
                                            protocol_name='EmonCMS',
                                            manager_dict=manager_dict,
                                            post_interval=post_interval,
                                            max_backlog=max_backlog,
                                            stale=stale,
                                            log_success=log_success,
                                            log_failure=log_failure,
                                            max_tries=max_tries,
                                            timeout=timeout,
                                            retry_wait=retry_wait)
        self.token = token
        self.prefix = prefix
        self.node = node
        self.upload_all = True if obs_to_upload.lower() == 'all' else False
        self.append_units_label = append_units_label
        self.inputs = inputs
        self.server_url = server_url
        self.skip_upload = to_bool(skip_upload)
        self.unit_system = unit_system
        self.augment_record = augment_record
        self.templates = dict()

    def process_record(self, record, dbm):
        if self.augment_record and dbm:
            record = self.get_record(record, dbm)
        if self.unit_system is not None:
            record = weewx.units.to_std_system(record, self.unit_system)
        url = self.get_url(record)
        if self.skip_upload:
            loginf("skipping upload")
            return
        req = urllib2.Request(url)
        req.add_header("User-Agent", "weewx/%s" % weewx.__version__)
        self.post_with_retries(req)

    def check_response(self, response):
        txt = response.read()
        if txt != 'ok' :
            raise weewx.restx.FailedPost("Server returned '%s'" % txt)

    def get_url(self, record):
        # if there is a prefix, prepend it to every variable name
        prefix = ''
        if self.prefix is not None:
            prefix = '%s_' % urllib.quote_plus(self.prefix)

        # if uploading everything, we must check the upload variables list
        # every time since variables may come and go in a record.  use the
        # inputs to override any generic template generation.
        if self.upload_all:
            for f in record:
                if f not in self.templates:
                    self.templates[f] = _get_template(f,
                                                      self.inputs.get(f, {}),
                                                      self.append_units_label,
                                                      record['usUnits'])

        # otherwise, create the list of upload variables once, based on the
        # user-specified list of inputs.
        elif not self.templates:
            for f in self.inputs:
                self.templates[f] = _get_template(f, self.inputs[f],
                                                  self.append_units_label,
                                                  record['usUnits'])

        # loop through the templates, populating them with data from the
        # record.  append each to an array that we use to build the url.
        data = []
        for k in self.templates:
            try:
                v = float(record.get(k))
                name = self.templates[k].get('name', k)
                fmt = self.templates[k].get('format', '%s')
                to_units = self.templates[k].get('units')
                if to_units is not None:
                    (from_unit, from_group) = weewx.units.getStandardUnitType(
                        record['usUnits'], k)
                    from_t = (v, from_unit, from_group)
                    v = weewx.units.convert(from_t, to_units)[0]
                s = fmt % v
                data.append('%s%s:%s' % (prefix, name, s))
            except (TypeError, ValueError):
                pass

        # assemble the complete url from the pieces
        parts = []
        parts.append(self.server_url)
        parts.append('?apikey=%s' % self.token)
        parts.append('&time=%s' % record['dateTime'])
        if self.node is not None:
            parts.append('&node=%s' % self.node)
        parts.append('&json={%s}' % ','.join(data))
        url = ''.join(parts)
        if weewx.debug >= 2:
            logdbg('url: %s' % re.sub(r"apikey=[^\&]*", "apikey=XXX", url))
        return url
