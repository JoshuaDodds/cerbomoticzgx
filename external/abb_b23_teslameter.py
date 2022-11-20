"""
VenusOS module to support of ABB B21, B23 and B24 kWh meters

    install notes:
        0) on newer versions of firmware > 2.87 you will need to remount the root filesystem as
            writable in order to modify or add files to it:  issue the following command as root:
                mount -o remount,rw /
        1) place this script in /opt/victronenergy/dbus-modbus-client/
        2) add an import statement to /opt/victronenergy/dbus-modbus-client/dbus-modbus-client.py after line 23
            (under 'import carlo_gavazzi')
        3) reboot cerbo GX or kill the pid of the dbus-modbus-client.py script (supervise will restart it)
        4) ensure EW-11 rs-485 to network server has modbus enabled under serial protocol settings
        5) add the device under cerbo GX modbus TCP Device setup

    Note: This module will not survice a firmware upgrade of the Cerbo GX unit.  These steps will need to be
    performed again after a firmware upgrade.
"""
import logging
import device
import probe
from register import *

log = logging.getLogger()

class Reg_u64b(Reg_num):
    def __init__(self, base, *args, **kwargs):
        super(Reg_u64b, self).__init__(base, 4, *args, **kwargs)
        self.coding = ('>Q', '>4H')
        self.scale = float(self.scale)


nr_phases = [1, 3, 3]

phase_configs = [
    '1P',
    '3P.n',
    '3P',
]

class ABB_B2x_Meter(device.EnergyMeter):
    productid = 0xb017
    productname = 'Tesla Power Meter'
    min_timeout = 0.5

    def __init__(self, *args):
        super(ABB_B2x_Meter, self).__init__(*args)

        self.data_regs = None
        self.info_regs = [
            Reg_text(0x8960, 6, '/HardwareVersion'),
            Reg_text(0x8908, 8, '/FirmwareVersion'),
            Reg_u32b(0x8900, '/Serial'),
        ]

    def phase_regs(self, n):
        s2 = 0x0002 * (n - 1)
        s4 = 0x0004 * (n - 1)

        return [
            Reg_u32b(0x5b00 + s2, '/Ac/L%d/Voltage' % n,         10, '%.1f V'),
            Reg_u32b(0x5b0c + s2, '/Ac/L%d/Current' % n,        100, '%.1f A'),
            Reg_s32b(0x5b16 + s2, '/Ac/L%d/Power' % n,          100, '%.1f W'),
            Reg_u64b(0x5460 + s4, '/Ac/L%d/Energy/Forward' % n, 100, '%.1f kWh'),
            Reg_u64b(0x546c + s4, '/Ac/L%d/Energy/Reverse' % n, 100, '%.1f kWh'),
        ]

    def device_init(self):
        self.read_info()

        phases = 3
        # phases = nr_phases[int(self.info['/PhaseConfig'])]

        regs = [
            Reg_s32b(0x5b14, '/Ac/Power',          100, '%.1f W'),
            Reg_u16(0x5b2c, '/Ac/Frequency',       100, '%.1f Hz'),
            Reg_u64b(0x5000, '/Ac/Energy/Forward', 100, '%.1f kWh'),
            Reg_u64b(0x5004, '/Ac/Energy/Reverse', 100, '%.1f kWh'),
        ]

        for n in range(1, phases + 1):
            regs += self.phase_regs(n)

        self.data_regs = regs

    def get_ident(self):
        return 'cg_%s' % self.info['/Serial']


models = {
    16946: {
        'model':    'ABB_B2x',
        'handler':  ABB_B2x_Meter,
    },
}


probe.add_handler(probe.ModelRegister(0x8960, models,
                                      methods=['tcp'],
                                      units=[1]))
