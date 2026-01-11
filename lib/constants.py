import logging
from lib.config_retrieval import retrieve_setting

cerboGxEndpoint = retrieve_setting('CERBOGX_IP')
mosquittoEndpoint = retrieve_setting('MOSQUITTO_IP')
systemId0 = retrieve_setting('VRM_PORTAL_ID')
dzEndpoint = retrieve_setting('DZ_URL_PREFIX')
PushOverConfig = {"id": retrieve_setting('PO_USER_ID'), "key": retrieve_setting('PO_API_KEY')}
HOME_ID = retrieve_setting('HOME_ID')
TIBBER_LIVE_MEASUREMENTS_FORCE = retrieve_setting('TIBBER_LIVE_MEASUREMENTS_FORCE')

logging.basicConfig(
    format='%(asctime)s cerbomoticzGx: %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')

"""
Topics we will monitor for PV system updates to domotics system
"""
Topics = dict({
    "system0":
        {
            # ESS Metrics
            "batt_soc":     f"N/{systemId0}/battery/277/Soc",
            "batt_current": f"N/{systemId0}/battery/277/Dc/0/Current",
            # "batt_voltage":   f"N/{systemId0}/battery/277/Dc/0/Voltage",  # Use Shunt Voltage
            "batt_voltage": f"N/{systemId0}/battery/512/Dc/0/Voltage",   # Use LFP Voltage
            "batt_power":   f"N/{systemId0}/battery/277/Dc/0/Power",
            # "batt_discharged_energy": f"N/{systemId0}/battery/277/History/DischargedEnergy",
            # "batt_charged_energy":    f"N/{systemId0}/battery/277/History/ChargedEnergy",
            "modules_online":   f"N/{systemId0}/battery/512/System/NrOfModulesOnline",

            # PV
            "pv_power":         f"N/{systemId0}/system/0/Dc/Pv/Power",
            "pv_current":       f"N/{systemId0}/system/0/Dc/Pv/Current",
            "system_state":     f"N/{systemId0}/system/0/SystemState/State",
            "c2_daily_yield":   f"N/{systemId0}/solarcharger/283/History/Daily/0/Yield",
            "c1_daily_yield":   f"N/{systemId0}/solarcharger/282/History/Daily/0/Yield",

            # AC Out Metrics
            "ac_out_power":     f"N/{systemId0}/vebus/276/Ac/Out/P",

            # AC In Metrics
            "ac_in_connected": f"N/{systemId0}/vebus/276/Ac/ActiveIn/Connected",
            "ac_in_power":  f"N/{systemId0}/vebus/276/Ac/ActiveIn/P",

            # Control
            "ac_power_setpoint":                f"N/{systemId0}/settings/0/Settings/CGwacs/AcPowerSetPoint",
            "max_charge_voltage":               f"N/{systemId0}/settings/0/Settings/SystemSetup/MaxChargeVoltage",
            "minimum_ess_soc":                  f"N/{systemId0}/settings/0/Settings/CGwacs/BatteryLife/MinimumSocLimit",
            "inverter_mode":                    f"N/{systemId0}/vebus/276/Mode",
            "grid_charging_enabled":            f"Tesla/settings/grid_charging_enabled",
            "trigger_ess_charge_scheduling":    f"Cerbomoticzgx/EnergyBroker/RunTrigger",
            "clear_ess_charge_schedule":        f"Cerbomoticzgx/EnergyBroker/ClearSchedule",
            "system_shutdown":                  f"Cerbomoticzgx/system/shutdown",
            "ess_net_metering_enabled":         f"Cerbomoticzgx/system/ess_net_metering_enabled",
            "ess_net_metering_overridden":      f"Cerbomoticzgx/system/ess_net_metering_overridden",   # When this is toggled on, DynESS will not operate with automated buy/sell decisions
            "ess_net_metering_batt_min_soc":    f"Cerbomoticzgx/system/ess_net_metering_batt_min_soc",

            # Tibber
            "tibber_total":                     f"N/{systemId0}/Tibber/home/energy/day/euro_day_total",  # workaround to update dz
            "tibber_day_total":                 f"Tibber/home/energy/day/reward",
            "tibber_last_update":               f"Tibber/home/energy/day/last_update",
            "tibber_price_now":                 f"Tibber/home/price_info/now/total",
            "tibber_cost_highest_today":        f"Tibber/home/price_info/today/highest/0/cost",
            "tibber_cost_highest_today_hr":     f"Tibber/home/price_info/today/highest/0/hour",
            "tibber_cost_highest2_today":       f"Tibber/home/price_info/today/highest/1/cost",
            "tibber_cost_highest2_today_hr":    f"Tibber/home/price_info/today/highest/1/hour",
            "tibber_cost_highest3_today":       f"Tibber/home/price_info/today/highest/2/cost",
            "tibber_cost_highest3_today_hr":    f"Tibber/home/price_info/today/highest/2/hour",
            "tibber_cost_lowest_today":         f"Tibber/home/price_info/today/lowest/0/cost",
            "tibber_cost_lowest2_today":        f"Tibber/home/price_info/today/lowest/1/cost",
            "tibber_cost_lowest3_today":        f"Tibber/home/price_info/today/lowest/2/cost",
            "tibber_export_schedule_status":    f"Tibber/home/price_info/today/tibber_export_schedule_status",

            # Tesla specific metrics
            "tesla_power":                  f"N/{systemId0}/acload/42/Ac/Power",
            "tesla_l1_current":             f"N/{systemId0}/acload/42/Ac/L1/Current",
            "tesla_l2_current":             f"N/{systemId0}/acload/42/Ac/L2/Current",
            "tesla_l3_current":             f"N/{systemId0}/acload/42/Ac/L3/Current",
            "tesla_plug_status":            f"Tesla/vehicle0/plugged_status",
            "tesla_is_home":                f"Tesla/vehicle0/is_home",
            "tesla_is_charging":            f"Tesla/vehicle0/is_charging",
            "tesla_charge_requested":       f"Tesla/vehicle0/control/charge_requested",
            "tesla_battery_soc":            f"Tesla/vehicle0/battery_soc",
            "tesla_battery_soc_setpoint":   f"Tesla/vehicle0/battery_soc_setpoint",

            # Home Connect Appliance topics
            "dryer_state":                  f"Cerbomoticzgx/homeconnect/dryer/state",
            "dishwasher_state":             f"Cerbomoticzgx/homeconnect/dishwasher/state",
        }
})

"""
Topics we are able to write to
"""
TopicsWritable = dict({
    "system0":
        {
            # Control
            "ac_power_setpoint":    f"W/{systemId0}/settings/0/Settings/CGwacs/AcPowerSetPoint",
            "max_charge_voltage":   f"W/{systemId0}/settings/0/Settings/SystemSetup/MaxChargeVoltage",
            "minimum_ess_soc":      f"W/{systemId0}/settings/0/Settings/CGwacs/BatteryLife/MinimumSocLimit",
            "inverter_mode":        f"N/{systemId0}/vebus/276/Mode",
            "system_shutdown":      f"Cerbomoticzgx/system/shutdown",
        }
})


mqtt_msg_value_conversion = dict({
    "system_state": lambda value: SystemState[value],
    "batt_current": lambda value: round(value, 2),
    "pv_power":     lambda value: f"{round(value)};1",
    "pv_current":   lambda value: round(value),
    "batt_soc":     lambda value: round(value, 2),
    "batt_voltage": lambda value: round(value, 2),
    "tesla_power":  lambda value: f"{round(value)};1",
    "batt_power":   lambda value: f"{round(value)};1",
})

"""
DomoticZ Device ID's to update
"""
DzDevices = dict({
    "system0":
        {
            "batt_soc": "587",
            "batt_current": "593",
            "batt_voltage": "586",
            "pv_power": "592",
            "pv_current": "591",
            "system_state": "622",
            "tibber_total": "626",
            "tesla_power": "627",
            "batt_power": "628",
        },
    "vehicle0":
        {
            "vehicle_status": "624",
        }
})

"""
DomoticZ Rest API updating endpoints
"""
DzEndpoints = dict({
    "system0": {
        str(f"{Topics['system0']['batt_soc']}"):        f"{dzEndpoint}{DzDevices['system0']['batt_soc']}&nvalue=0&svalue=",
        str(f"{Topics['system0']['batt_current']}"):    f"{dzEndpoint}{DzDevices['system0']['batt_current']}&nvalue=0&svalue=",
        str(f"{Topics['system0']['batt_voltage']}"):    f"{dzEndpoint}{DzDevices['system0']['batt_voltage']}&nvalue=0&svalue=",
        str(f"{Topics['system0']['pv_power']}"):        f"{dzEndpoint}{DzDevices['system0']['pv_power']}&nvalue=0&svalue=",
        str(f"{Topics['system0']['pv_current']}"):      f"{dzEndpoint}{DzDevices['system0']['pv_current']}&nvalue=0&svalue=",
        str(f"{Topics['system0']['system_state']}"):    f"{dzEndpoint}{DzDevices['system0']['system_state']}&nvalue=0&svalue=",
        str(f"{Topics['system0']['tibber_total']}"):    f"{dzEndpoint}{DzDevices['system0']['tibber_total']}&svalue=",
        str(f"{Topics['system0']['tesla_power']}"):     f"{dzEndpoint}{DzDevices['system0']['tesla_power']}&nvalue=0&svalue=",
        str(f"{Topics['system0']['batt_power']}"):      f"{dzEndpoint}{DzDevices['system0']['batt_power']}&nvalue=0&svalue=",
    },
    "vehicle0": {
        # Endpoints fed by ev_charge_controller.py
        str(f"vehicle_status"):  f"{dzEndpoint}{DzDevices['vehicle0']['vehicle_status']}&nvalue=0&svalue=",
    }
})

"""
Integer to human readable system state mappings
"""
SystemState = dict({
    0: "Off",
    1: "Low Accu Power",
    2: "VE.Bus Fault",
    3: "Bulk Charging",
    4: "Absorption Charging",
    5: "Float Charging",
    6: "Storage Mode",
    7: "Equalisation Charging",
    8: "Pass-through Mode",
    9: "Inverting",
    10: "Assisting",
    252: "External Control",
    256: "Discharging",
    257: "Sustain",
    259: "Scheduled Charging",
})

"""
python time weekday to victron weekday numbering conversion table
"""
PythonToVictronWeekdayNumberConversion = dict({
    0: 1,
    1: 2,
    2: 3,
    3: 4,
    4: 5,
    5: 6,
    6: 0,
})

def retrieve_mqtt_subcribed_topics(sysid=None):
    if not sysid:
        sysid = "system0"

    for value in Topics[sysid].keys():
        yield Topics[sysid][value]
