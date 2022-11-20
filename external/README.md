## VenusOS module to support of ABB B21, B23 and B24 kWh meters

###    Installation
        1) drop `abb_b23_teslameter.py` in /opt/victronenergy/dbus-modbus-client/ on your cerboGx / VenusOS device

        2) add an import statement to /opt/victronenergy/dbus-modbus-client/dbus-modbus-client.py after line 23
            (under 'import carlo_gavazzi')

        3) reboot cerbo GX or kill the pid of the dbus-modbus-client.py script (supervise will restart it)

        4) ensure EW-11 rs-485 to network server has modbus enabled under serial protocol settings. If you are
            using some other method to convert the ABB meter modbus to modbusTCP, configure appropriately and
            optionally you can test your settings and connectivity with the included `abb_meter_testing.py` script.

        5) Set up the ABB meter in the cerboGX/VenusOS device under `settings> modbus TCP Devices> Add` menu.

#####    Note:  This module will not survive a firmware upgrade of the Cerbo GX unit.  These steps will need to be performed again after a firmware upgrade.
