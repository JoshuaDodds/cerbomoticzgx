CerbomoticzGx
========================
## Introduction 
Do you have one or more of the following devices in your home?

- [x] Solar panels (the more the better)
- [x] A Tesla vehicle (optional)
- [x] Victron Energy Equipment (Cerbo GX compatible Inverters/Chargers, MPPT charge controllers, etc.)
- [x] Home Energy Storage System with canbus or serial control and working well with your Victron system
- [x] an ABB B21/23/24 Kilowatt meter (optional)
- [x] a Domoticz based Home Automation system (optional)

If so, this project might be something you will find interesting. Have a look at what this project offers by 
reading more below.  Also, many of the cool features the modules in this project offer are visualized and controllable 
via a by a frontend React based Dashboard which you can find over here: [venus-nextgen Energy Dashboard](https://github.com/JoshuaDodds/venus-nextgen)

## Features
This project is a series of modules which aim to integrate, automate and control the following systems and components.

- Victron Energy Equipment (Cerbo GX controlled Inverters, Solar MPPT charge controllers, etc.)
- Victron compatible LFP based Energy Storage Systems
- Tesla Electric Vehicles
- Tibber Smart Energy Supplier (hourly spot rate electricity supplier) API integration
- ABB B21/23/24 kWh meters
- Domoticz Home Automation System 


Current Features include:
- monitors a number of metrics from a Victron Energy CerboGX controlled system and reports these metrics back to
a Domoticz server via its REST API for monitoring and historic tracking
- Modular - Individual modules can be enabled or disabled in the ```.env``` file    
- Included a custom module which can be installed on a cerbo gx to read out ABB B2x kWh meters
- EV Charge Controller - Tesla vehicle charging at lowest rates or using only excess solar energy
- Grid Assisted vehicle charging mode for when you need to just charge at full rate regardless of cost
- Energy Broker module which attempts to buy energy at the lowest possible rate in a 48 hour period and store this in your home battery
- Tibber graphing module to generate visuals of the upcoming electricity prices (Thanks to [Tibberios](https://github.com/Lef-F/tibberios))
- Tibber API integration to constantly monitor current energy rates, daily consumption and production, forecasted pricing, etc (Thanks to [Tibber.py](https://github.com/BeatsuDev/tibber.py))
- deep integration with Victron system for monitoring and control via the cerbo Gx MQTT broker
- Creates, exports, and updates a number of custom metrics to the victron MQTT broker for consumption by the [venus-nextgen Energy Dashboard](https://github.com/JoshuaDodds/venus-nextgen)

Configuration for your CerboGX IP Address, VRM instance ID, and Domoticz IP/Port are configured in 
the ```.env``` configuration file. 

Note: The name of this project is a nod to both Victron Energy & the Domoticz project.


### Installation
```pip install -r requirements.txt```

### Configuration
- Read the ```.env``` file carefully and adjust as needed.
- Carefully read through lib/contstants.py and adjust to fit your situation

### Running from CLI
```python3 main.py```

### Docker Container
If you will be building and running this from a container you will want to fork this repo and make sure you set up your configuration 
to match your wishes and your own system.

Check the entrypoint.sh  for the container. You will need to adjust how you handle secrets & configuration injection for the container.

Finally, use the build.sh script as a template for building an arm64 image and pushing it to a container repository.

---------------
(This package is in its infancy, but contributions and collaborations are welcome.)

Copyright 2022 Joshua Dodds
