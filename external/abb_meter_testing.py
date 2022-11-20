import pymodbus.exceptions
from pymodbus.client.sync import ModbusTcpClient as ModbusClient
from pymodbus.transaction import *
import sys
from time import sleep

reg = 0x5B16
unit = 100
phases = 3

def phase_regs(n):
    s2 = 0x0002 * (n - 1)
    s4 = 0x0004 * (n - 1)

    return [
        #  (register, topic, length, multiplier, register[index], string formatting)
        #(0x5b00 + s2, '/Ac/L%d/Voltage' % n,        2,  10, 1, '%.1f V'),
        #(0x5b0c + s2, '/Ac/L%d/Current' % n,        2, 100, 1, '%.1f A'),
        #(0x5b16 + s2, '/Ac/L%d/Power' % n,          2, 100, 1, '%.1f W'),
        # (0x5460 + s4, '/Ac/L%d/Energy/Forward' % n, 4, 100, 1, '%.1f kWh'),
        # (0x546c + s4, '/Ac/L%d/Energy/Reverse' % n, 4, 100, 1, '%.1f kWh'),
    ]

def read_device(phases):
    regs = [
        (0x5b14, 'Power',  2, 100, 1, '%.1f W'),
        (0x5000, 'Import', 4, 100, 0, '%.1f kWh'),
        (0x5004, 'Export', 4, 100, 0, '%.1f kWh'),
        #(0x5008, 'Net',    4, 100, 1, '%.1f kWh'),
        #(0x5b2c, 'Frequency',      2, 100, 0, '%.1f Hz'),
    ]

    for n in range(1, phases + 1):
        regs += phase_regs(n)

    return regs


def main():
    # client = ModbusClient('localhost', port=8899, framer=ModbusRtuFramer, timeout=1)
    client = ModbusClient('192.168.1.87', port=502, framer=ModbusSocketFramer, timeout=1)
    client.connect()

    while True:
        try:
            for i in read_device(phases):
                result = client.read_holding_registers(i[0], i[2], unit=100)

                if result:
                    try:
                        # print(f"{result.unit_id}: {result.registers[i[4]] / i[3]} ({i[1]})")
                        print(f"{result.unit_id}: {result.registers} ({i[1]})")
                    except Exception as E:
                        print(f"{E} | {result}")

            print(f"\n")
            sleep(1)

        except pymodbus.exceptions.ConnectionException:
            pass

        except (SystemExit, KeyboardInterrupt):
            print(f"Cleaning up...")
            client.close()
            sys.exit()
