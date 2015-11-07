#!/usr/bin/python
import cgi
import sys
import SocketServer
import BaseHTTPServer
from itertools import cycle, islice
from StringIO import StringIO
from string import Template
import datetime
import time
import json
from urllib2 import urlopen, Request

OFF = 0
ON = 1
VALID_DAYS = [u'Sun', u'Mon', u'Tue', u'Wed', u'Thu', u'Fri', u'Sat']

"""
def get_suntimes():
    tm = time.localtime()
    request = Request('http://new.earthtools.org/sun/-37.8/144.96/%d/%d/10/%d' % (tm.tm_mday, tm.tm_mon, tm.tm_isdst))
    response = urlopen(request)
    timestring = response.read()
    print timestring
    
get_suntimes()
"""
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
    
    def get_mode(self):
        if self.mode == TimerConfig.MODE_AUTO:
            return "AUTO"
        return "MANUAL"
    
    def get_powered(self):
        if self.mode == TimerConfig.MODE_AUTO:
            day, time, current_state = self.get_transitions_from_current().next()
            return {ON:'ON', OFF:'OFF'}[current_state]
        else:
            return {TimerConfig.MODE_MANUAL_OFF:"OFF", TimerConfig.MODE_MANUAL_ON:"ON"}[self.mode]

    def get_transition_list(self):
        for d in VALID_DAYS:
            if d in self.schedule:
                for t,s in self.schedule[d]:
                    yield (d,t,s)
    
    def get_transitions_from_current(self):
        def cycle_with_default_generator(items):
            # This is used to make sure the first item is always valid
            # Even if the configured list hasnt started yet
            yield (u'Sun', '0000', [ON, OFF][items[0][2]])
            x = cycle(items)
            while True:
                yield x.next()
        if self.mode != TimerConfig.MODE_AUTO:
            return None
        current_day, current_time = get_clocks()
        current_day_idx = VALID_DAYS.index(current_day)
        # Need to get the full list of transitions in a single array
        all_changes = list(self.get_transition_list())
        current_idx = None
        for i in range(len(all_changes)):
            day, time, state = all_changes[i]
            if (VALID_DAYS.index(day) < current_day_idx) or \
                ((VALID_DAYS.index(day) == current_day_idx) and int(time, base=10) <= current_time):
                current_idx = i

        if current_idx == None and len(all_changes) > 1:
            return cycle_with_default_generator(all_changes)

        return cycle(all_changes[current_idx:] + all_changes[:current_idx])
            
    def get_next_transitions(self, amount=2):
        return list(islice(self.get_transitions_from_current(), 1, amount + 1))

def load_from_dict(cfg):
    def load_schedule_array(schedule):
        return (str(schedule.keys()[0]), {u'ON': ON, u'OFF': OFF}[schedule.values()[0]])
    config = {}
    for x in cfg:
        addr = x["addr"]
        nick = x["nickname"]
        schedule = {}
        for day, items in x["schedule"].iteritems():
            if day not in VALID_DAYS:
                continue
            schedule[day] = [load_schedule_array(x) for x in items]
        config[addr] = TimerConfig(nick, schedule)
    
    return config

__config = None

def handle_get_state(timer_addr):
    if timer_addr not in __config:
        return None
    config = __config[timer_addr]
    
    power_str = config.get_powered()
    mode_str = config.get_mode()
    
    return "power=%s timer=%s" % (power_str, mode_str)

def handle_do_button(timer_addr):
    if timer_addr not in __config:
        return None
    config = __config[timer_addr]
    config.do_button()
    return handle_get_state(timer_addr)

def get_clocks():
    tm = time.localtime()
    current_time = int("%02d%02d" % (tm.tm_hour, tm.tm_min), base=10)
    today_str = datetime.datetime.now().strftime("%a")
    return (today_str, current_time)

def get_next_change_text(timer_addr):
    if timer_addr not in __config:
        return ""
    d, t, s = __config[timer_addr].get_next_transitions()[0]
    state = {ON:'ON', OFF:'OFF'}[s]
    hr = t[0:2]
    mins = t[2:]
    if d != get_clocks()[0]:
        day_suffix = " on %s" % (d)
    else:
        day_suffix = ""
    return "Timer will turn %s at %s:%s%s" % (state, hr, mins, day_suffix)
    

def get_config(name):
    if name in __config.keys():
        return __config[name]
    return None

def get_html(addr):
    with open('../www/magictimer.html', 'r') as fh:
        html = fh.read()
        temp = Template(html)
        cfg = get_config(addr)
        if cfg == None:
            for k,v in __config.iteritems():
                if v.nickname.lower() == addr.lower():
                    cfg = v
                    addr = k
                    break
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
                CHECK_ON_RADIO=checks["on"],CHECK_OFF_RADIO=checks["off"],CHECK_TIMER_RADIO=checks["timer"])
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
    HOST, PORT = "0.0.0.0", 8080
    if len(sys.argv) > 1:
         PORT = int(sys.argv[1])

    with open('config.json', 'r') as fh:
        jscfg = json.load(fh)
        __config = load_from_dict(jscfg)

    # Create the server, binding to localhost on port 9999
    server = BaseHTTPServer.HTTPServer((HOST, PORT), TimerHttpServer)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
