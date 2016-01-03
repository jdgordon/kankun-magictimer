#!/usr/bin/python
import cgi
import sys
import SocketServer
import BaseHTTPServer
from itertools import cycle, islice, chain
from StringIO import StringIO
from string import Template
import datetime
import time
import json
import calendar
from urllib2 import urlopen, Request
from collections import namedtuple

from flask import Flask, request
app = Flask(__name__)

class State:
    off, on = range(2)
    def __init__(self, val):
        if isinstance(val, unicode) or isinstance(val, str):
            self._val = {u'OFF': self.off , u'ON': self.on}[val]
        else:
            self._val = val
    @property
    def name(self):
        return [u'OFF', u'ON'][self._val]
    
    @property
    def value(self):
        return self._val
    
    def __invert__(self):
        if self._val == self.off:
            return State(self.on)
        return State(self.off)
    
    def __repr__(self):
        return "%s:(%d/%s)" % (self.__class__.__name__, self._val, self.name)

VALID_DAYS = list(calendar.day_abbr)

class SunTimeDiff(object):
    def __init__(self, config_str):
        x = config_str.split()
        assert len(x) == 3 and x[0].lower() in ['$sunset', '$sunrise'] \
            and x[1] in ['+', '-']

        self.sunstate = x[0].lower()
        op = x[1]
        mins = x[2]
        self.timediff = datetime.timedelta(minutes=int(op + mins))

__suntimes_cache = {}
def get_suntimes(day):
    global __suntimes_cache
    if day in __suntimes_cache:
        return __suntimes_cache[day]

    location_dict = __config["location"]

    tm = time.localtime()
    date_str = '%d-%02d-%02d' % (day.year, day.month, day.day)
    url = 'http://api.sunrise-sunset.org/json?lat=%s&lng=%s&date=%s' % \
        (location_dict["lat"], location_dict["long"], date_str)
    request = Request(url)
    response = urlopen(request)
    time_dict = json.loads(response.read())
    if time_dict["status"] != "OK":
        return None
    times = {}
    for x in ["sunset", "sunrise"]:
        utc_time = time.strptime(date_str + " " + time_dict["results"][x], "%Y-%m-%d %I:%M:%S %p")
        times["$" + x] = calendar.timegm(utc_time)
    __suntimes_cache[day] = times
    return times

TransitionInfo = namedtuple('TransitionInfo', ['datetime', 'state'])

class TimerConfig:
    MODE_AUTO = 0
    MODE_MANUAL_ON = 1
    MODE_MANUAL_OFF = 2
    MODE_COUNT = 3

    def __init__(self, nickname, schedule = None):
        self.schedule = schedule
        self.nickname = nickname
        self.mode = TimerConfig.MODE_AUTO
    
    def do_button(self):
        self.mode = (self.mode + 1) % TimerConfig.MODE_COUNT
    
    def set_mode(self, mode):
        self.mode = {"ON": TimerConfig.MODE_MANUAL_ON,
                     "OFF": TimerConfig.MODE_MANUAL_OFF,
                     "AUTO": TimerConfig.MODE_AUTO}[mode.upper()]
    def get_mode(self):
        if self.mode == TimerConfig.MODE_AUTO:
            return "AUTO"
        return "MANUAL"
    
    def get_powered(self):
        if self.mode == TimerConfig.MODE_AUTO:
            time, current_state = self.get_transitions_from_current().next()
            return current_state.name
        else:
            return {TimerConfig.MODE_MANUAL_OFF:"OFF", TimerConfig.MODE_MANUAL_ON:"ON"}[self.mode]

    def get_transition_list(self):
        """
            Returns a generator of tuples in the form:
            ( datetime.datetime object, ON/OFF)
        """
        def get_item_key(obj_list):
            key = obj_list.datetime
            if (isinstance(key, SunTimeDiff)):
                suntimes = get_suntimes(start_day)
                dt = datetime.datetime.fromtimestamp(suntimes[key.sunstate]) + key.timediff
                return "%02d%02d" % (dt.hour, dt.minute)
            return key

        timediff = datetime.timedelta(days=1)
        start_day = datetime.date.today()
        # This first needs to find the first day >= "today" that exists in the schedule
        for d in list(islice(cycle(calendar.day_abbr), start_day.weekday(), start_day.weekday() + 7)):
            if d in self.schedule and len(self.schedule[d]) > 0:
                dt = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
                first = sorted(self.schedule[d], key=get_item_key)[0]
                yield TransitionInfo(dt, ~first[1])
                break
            start_day += timediff
        while True:
            d = calendar.day_abbr[start_day.weekday()]
            if d in self.schedule:
                for t,s in sorted(self.schedule[d], key=get_item_key):
                    real_time = get_item_key(TransitionInfo(t, State("ON"))) # fixme, put the datetime obj instead of the string
                    hr = int(real_time[0:2], base=10)
                    mins = int(real_time[2:], base=10)
                    yield TransitionInfo(datetime.datetime.combine(start_day, datetime.time(hr, mins)), s)
            start_day += timediff
    
    def get_transitions_from_current(self):
        """Returns a generator with the first being the current state"""
        if self.mode != TimerConfig.MODE_AUTO:
            return None
        transitions = self.get_transition_list()
        idx = 0
        now = datetime.datetime.now()
        while now > transitions.next()[0]:
            idx += 1
        return islice(self.get_transition_list(), idx - 1, None)
            
    def get_next_transitions(self, amount=2):
        return list(islice(self.get_transitions_from_current(), 1, 1 + amount))

def load_from_dict(cfg):
    def load_schedule_array(schedule):
        for k, v in schedule.iteritems():
            state = State(v)
            if k.split()[0] in ['$sunset', '$sunrise']:
                yield TransitionInfo(SunTimeDiff(k), state)
            else:
                yield TransitionInfo(k, state)
    timers = {}
    for x in cfg["timers"]:
        addr = x["addr"]
        nick = x["nickname"]
        schedule = {}
        for day, items in x["schedule"].iteritems():
            if day not in VALID_DAYS:
                continue
            schedule[day] = []
            for j in items:
                schedule[day] += list(load_schedule_array(j))
        timers[addr] = TimerConfig(nick, schedule)

    return {"timers": timers, "location": cfg["location"]}

__config = None

@app.route('/api/0.1/<timer_addr>')
def handle_get_state(timer_addr):
    if timer_addr not in __config["timers"]:
        return None
    config = __config["timers"][timer_addr]
    
    power_str = config.get_powered()
    mode_str = config.get_mode()
    
    return "power=%s timer=%s" % (power_str, mode_str)

@app.route('/api/0.1/<timer_addr>/button')
def handle_do_button(timer_addr):
    if timer_addr not in __config["timers"]:
        return None
    config = __config["timers"][timer_addr]
    config.do_button()
    return handle_get_state(timer_addr)

def get_next_change_text(timer_addr):
    if timer_addr not in __config["timers"]:
        return ""
    time, s = __config["timers"][timer_addr].get_next_transitions()[0]
    state = s.name
    hr = time.hour
    mins = time.minute
    d = calendar.day_abbr[time.weekday()]
    if time.weekday() != datetime.date.today().weekday():
        day_suffix = " on %s" % (d)
    else:
        day_suffix = ""
    return "Timer will turn %s at %s:%s%s" % (state, hr, mins, day_suffix)
    

def get_config(name):
    if name in __config["timers"].keys():
        return __config["timers"][name]
    return None
    
def find_config_from_nick(name):
    for k,v in __config["timers"].iteritems():
        if v.nickname.lower() == name.lower():
            return (v,k)

@app.route('/<addr>', methods=['GET', 'POST'])
def get_html(addr):
    cfg = get_config(addr)
    print cfg
    if not cfg:
        cfg, addr = find_config_from_nick(addr)
    if not cfg:
        return None
    if request.method == 'POST':
        cfg.set_mode(request.form.get('force'))
    with open('../www/magictimer.html', 'r') as fh:
        html = fh.read()
        temp = Template(html)
        if not cfg:
            return ""
        current = cfg.get_powered()
        next_text = ""
        mode = cfg.mode
        if mode == TimerConfig.MODE_AUTO:
            checks = {"on":"", "off":"", "timer":"checked"}
            next_text = get_next_change_text(addr)
        elif mode == TimerConfig.MODE_MANUAL_ON:
            checks = {"on":"checked", "off":"", "timer":""}
            next_text = "Timer is forced to ON, change setting below"
        else:
            checks = {"on":"", "off":"checked", "timer":""}
            next_text = "Timer is forced to OFF, change setting below"
        reply = temp.substitute(TIMER_NICKNAME=cfg.nickname,
                CURRENT_STATE=current, NEXT_STATE_CHANGE_TEXT=next_text,
                CHECK_ON_RADIO=checks["on"],CHECK_OFF_RADIO=checks["off"],CHECK_TIMER_RADIO=checks["timer"], ADDR=addr)
    return reply
        
        
class TimerHttpServer(BaseHTTPServer.BaseHTTPRequestHandler):

    def do_POST(self):
        ctype, pdict = cgi.parse_header(self.headers.getheader('content-type'))
        if ctype == 'multipart/form-data':
            postvars = cgi.parse_multipart(self.rfile, pdict)
        elif ctype == 'application/x-www-form-urlencoded':
            length = int(self.headers.getheader('content-length'))
            postvars = cgi.parse_qs(self.rfile.read(length), keep_blank_values=1)
        else:
            postvars = {}
        status = 500
        reply = ""
        args = self.path[1:].split("/")
        if len(args) < 3:
            status = 400
        elif args[0] != "api" or args[1] != "0.1":
            status = 400
        else:
            if args[2] == 'config' and "addr" in postvars:
                cfg = get_config(postvars["addr"][0])
                if cfg and "force" in postvars:
                    cfg.mode = {"none":TimerConfig.MODE_AUTO,
                                "on":TimerConfig.MODE_MANUAL_ON, 
                                "off":TimerConfig.MODE_MANUAL_OFF
                            }[postvars["force"][0].lower()]
                if cfg and "nickname" in postvars:
                    cfg.nickname = postvars["nickname"][0]
                reply = get_html(postvars["addr"][0])
                status = 200
        self.send_response(status)
        self.end_headers()
        self.wfile.write(reply)
    
    def do_GET(self):
        status = 500
        reply = ""
        args = self.path[1:].split("/")
        content_type = "text/plain"

        if len(args) == 1 and args[0] == '':
            reply = get_html('00:15:61:ee:bb:d2')
            content_type = "text/html"
            status = 200
        elif len(args) == 1:
            reply = get_html(args[0])
            content_type = "text/html"
            status = 200
        elif len(args) < 3:
            status = 400
        elif args[0] != "api" or args[1] != "0.1":
            status = 400
        else:
            command = args[2]
            if len(args) > 3 and args[3] == 'button':
                result = handle_do_button(args[2])
            result = handle_get_state(args[2])
            if result:
                status = 200
                reply = result
        self.send_response(status)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Length", len(reply))
        self.end_headers()
        self.wfile.write(reply)

if __name__ == "__main__":
    with open('config.json', 'r') as fh:
        jscfg = json.load(fh)
        __config = load_from_dict(jscfg)
    if len(sys.argv) > 1:
        app.run(debug=True)
    else:
        app.run(host='0.0.0.0', port=8100)
    """
    HOST, PORT = "0.0.0.0", 8080
    if len(sys.argv) > 1:
         PORT = int(sys.argv[1])

    with open('config.json', 'r') as fh:
        jscfg = json.load(fh)
        __config = load_from_dict(jscfg)

    #test = get_config("00:15:61:cc:85:e6")
    #print test.get_next_transitions()
    # Create the server, binding to localhost on port 9999
    server = BaseHTTPServer.HTTPServer((HOST, PORT), TimerHttpServer)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    """
