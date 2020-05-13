import time
import logging
import datetime
import functools

OUT_OF_RANGE = '_______'
OUT_OF_LIMIT = '.......'
NA = 'N/A'
DISABLED = ''
UNITS = ('K', 'C', 'F', 'S')
NACK = 'NACK'

#Delta read back tolerance
DELTA_RB = 0.0000001 #6 decimals precision
#setpoints seem to have some kind of preset values when not in K or S units
DELTA_RB_SETPT = 0.0001

TYPES = ['OFF', 'PID', 'MAN', 'TABLE', 'RAMPP', 'RAMPT']
RANGES = ['HI', 'MID', 'LOW']


def to_int(text):
    if text in (OUT_OF_LIMIT, OUT_OF_RANGE, NA):
        return None
    return int(text)


def to_float(text):
    if text in (OUT_OF_LIMIT, OUT_OF_RANGE, NA):
        return None
    return float(text)


def to_float_unit(text):
    return to_float(text[:-1])


def to_on_off(text):
    return text.upper() == 'ON'


def from_name(text):
    return '"{}"'.format(text)


def to_date(text):
    month, day, year = [int(i) for i in text.strip('"').split('/')]
    return datetime.date(year, month, day)


def to_time(text):
    hh, mm, ss = [int(i) for i in text.strip('"').split(':')]
    return datetime.time(hh, mm, ss)


class CryoConError(Exception):
    pass


class _Property:

    def __init__(self, prefix, name, fget=lambda x: x, fset=lambda x: x):
        self.cmd = ':{} {{}}:{}'.format(prefix.upper(), name.upper())
        self.fget = fget
        self.fset = fset

    def __get__(self, obj, owner=None):
        if self.fget is None:
            raise AttributeError("can't set attribute")
        cmd = self.cmd.format(obj.id) + '?'
        return obj.ctrl._query(cmd, self.fget)

    def __set__(self, obj, value):
        if self.fset is None:
            raise AttributeError("can't set attribute")
        cmd = '{} {}'.format(self.cmd.format(obj.id), self.fset(value))
        reply = obj.ctrl._command(cmd)


channel_property = functools.partial(_Property, 'INPUT')
loop_property = functools.partial(_Property, 'LOOP')


class Channel:

    name = channel_property('nam', fset=from_name)
    temperature = channel_property('temp', to_float)
    unit = channel_property('unit')
    minimum = channel_property('min', to_float)
    maximum = channel_property('max', to_float)
    variance = channel_property('vari', to_float)
    slope = channel_property('slop', to_float)
    offset = channel_property('offs', to_float)
    alarm = channel_property('alar')

    def __init__(self, channel, ctrl):
        self.id = channel
        self.ctrl = ctrl

    def clear_alarm(self):
        self.ctrl._command(':INPUT A:ALAR:CLE')



class Loop:

    source = loop_property('source')
    type = loop_property('typ')
    error = loop_property('err')
    rate = loop_property('rate', to_float)
    set_point = loop_property('setpt', to_float_unit)
    p_gain = loop_property('pga', to_float)
    i_gain = loop_property('iga', to_float)
    d_gain = loop_property('dga', to_float)
    manual_output_power = loop_property('pman', to_float)
    load = loop_property('load', to_int)
    max_output_power = loop_property('maxp', to_float)
    max_set_point = loop_property('maxs', to_float_unit)
    output_voltage = loop_property('vsen', to_float_unit, None) # in V
    output_current = loop_property('isen', to_float_unit, None) # in A
    output_load_resistance = loop_property('lsen', to_float, None)
    temperature = loop_property('htrh', to_float_unit, None) # in degC
    autotune_status = loop_property('aut:stat', str, None)

    def __init__(self, nb, ctrl):
        self.id = nb
        self.ctrl = ctrl

    def _query(self, cmd, func=lambda x: x):
        cmd = ':LOOP {}:{}?'.format(self.id, cmd)
        return self.ctrl._query(cmd, func)

    def _command(self, cmd, value):
        cmd = ':LOOP {}:{} {}'.format(self.id, cmd, value)
        self.ctrl._command(cmd)

    @property
    def output_power(self):
        return self._query('OUTPWR', to_float)

    @output_power.setter
    def output_power(self, power):
        if self.type != 'MAN':
            raise CryoConError('Loop must be in manual mode to set output power')
        self._query('OUTPWR {}'.format(power))
        rb = self.output_power
        if abs(rb - power) > DELTA_RB:
            raise CryoConError(
                'Written power {!r} differs from the one read back from '
                'instrument {!r}'.format(power, rb))

    @property
    def range(self):
        return self._query('RANGE')

    @range.setter
    def range(self, rng):
        if self.id != 1:
            raise IndexError('Can only set range for loop 1')
        if rng.upper() not in RANGES:
            raise ValueError('Invalid loop range {!r}. Valid ranges are: {}'.
                             format(rng, ','.join(RANGES)))
        self._query('RANGE {}'.format(rng))


class CryoCon:

    comm_error_retry_period = 3

    class Group:

        def __init__(self, ctrl):
            self.ctrl = ctrl
            self.cmds = ['']
            self.funcs = []

        def append(self, cmd, func):
            cmds = self.cmds[-1]
            ## maximum of 255 characters per command
            if len(cmds) + len(cmd) > 250:
                cmds = ''
                self.cmds.append(cmds)
            if self.cmds:
                cmds += ';'
            cmds += cmd
            self.cmds[-1] = cmds
            self.funcs.append(func)

        def query(self):
            reply = ';'.join([self.ctrl._ask(request) for request in self.cmds])
            replies = (msg.strip() for msg in reply.split(';'))
            replies = [func(text) for func, text in zip(self.funcs, replies)]
            self.replies = replies

    def __init__(self, conn, channels='ABCD', loops=(1,2,3,4)):
        self._conn = conn
        self._last_comm_error = None, 0  # (error, timestamp)
        self.channels = {channel:Channel(channel, self) for channel in channels}
        self.loops = {loop:Loop(loop, self) for loop in loops}
        self.group = None

    def __getitem__(self, key):
        try:
            return self.channels[key]
        except KeyError:
            return self.loops[key]

    def __enter__(self):
        self.group = self.Group(self)
        return self.group

    def __exit__(self, exc_type, exc_value, traceback):
        group = self.group
        self.group = None
        group.query()

    def _ask(self, cmd):
        now = time.time()
        last_err, last_ts = self._last_comm_error
        if now < (last_ts + self.comm_error_retry_period):
            raise last_err
        query = cmd.endswith('?')
        try:
            cmd += '\n'
            cmd_raw = cmd.encode()
            reply = None
            logging.info('REQ: %r', cmd)
            if query:
                reply = self._conn.write_readline(cmd_raw).strip().decode()
                logging.info('REP: %r', reply)
                if reply == NACK:
                    raise CryoConError('Command {!r} not acknowledged'.format(cmd))
            else:
                self._conn.write(cmd_raw)
            self._last_comm_error = None, 0
            return reply
        except OSError as comm_error:
            self._last_comm_error = comm_error, time.time()
            raise
        except Exception:
            self._last_comm_error = None, 0
            raise

    def _query(self, cmd, func=lambda x: x):
        if self.group is None:
            return func(self._ask(cmd))
        else:
            self.group.append(cmd, func)

    def _command(self, cmd):
        reply = self._ask(cmd)
        assert not reply

    @property
    def idn(self):
        return self._query(':*IDN?')

    @property
    def name(self):
        return self._query(':SYSTEM:NAME?')

    @name.setter
    def name(self, name):
        self._command(':SYSTEM:NAME "{}"'.format(name))

    @property
    def hw_revision(self):
        return self._query(':SYSTEM:HWR?')

    @property
    def fw_revision(self):
        return self._query(':SYSTEM:FWR?')

    @property
    def control(self):
        return self._query(':CONTROL?', to_on_off)

    @control.setter
    def control(self, onoff):
        cmd = 'CONTROL' if onoff in (True, 'on', 'ON') else 'STOP'
        self._command(cmd)

    @property
    def lockout(self):
        return self._query(':SYSTEM:LOCKOUT?', to_on_off)

    @lockout.setter
    def lockout(self, onoff):
        value = 'ON' if onoff in (True, 'on', 'ON') else 'OFF'
        self._command(':SYSTEM:LOCKOUT {}'.format(value))

    @property
    def led(self):
        return self._query(':SYSTEM:REMLED?', to_on_off)

    @led.setter
    def led(self, onoff):
        value = 'ON' if onoff in (True, 'on', 'ON') else 'OFF'
        self._command(':SYSTEM:REMLED {}'.format(value))

    @property
    def display_filter_time(self):
        return self._query(':SYSTEM:DISTC?', to_float)

    @display_filter_time.setter
    def display_filter_time(self, value):
        assert value in (0.5, 1, 2, 4, 8, 16, 32 or 64)
        self._command(':SYSTEM:DISTC {}'.format(value))

    @property
    def date(self):
        return self._query(':SYSTEM:DATE?', to_date)

    @date.setter
    def date(self, date):
        if isinstance(date, datetime.date):
            date = date.strftime('"%m/%d/%Y"')
        if not date.startswith('"'):
            date = '"{}"'.format(date)
        self._command(':SYSTEM:DATE {}'.format(date))

    @property
    def time(self):
        return self._query(':SYSTEM:TIME?', to_time)

    @time.setter
    def time(self, time):
        if isinstance(time, datetime.time):
            time = time.strftime('"%H:%M:%S"')
        if not time.startswith('"'):
            time = '"{}"'.format(time)
        self._command(':SYSTEM:TIME {}'.format(time))
