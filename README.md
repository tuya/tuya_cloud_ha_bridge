# tuya_cloud_ha_bridge

tuya_cloud_ha_bridge is a new Home Assistant (HA) plugin officially released by Tuya. Leveraging this plugin, users can synchronize devices from Home Assistant to the Tuya App via a QR code authorization process. Through the App, users can access a range of features—including device management, bidirectional device control, voice control, and scene automation—thereby enabling developers to conveniently manage smart home devices across both the Tuya ecosystem and Home Assistant.

## Key Features

1. Supports real-time, bi-directional cloud-based status synchronization via MQTT (Tuya Link).
2. Supported device categories include switches, smart colored lights, air conditioners, humidifiers, dehumidifiers, smart curtains, robotic vacuums, air purifiers, smart water heaters, and more. As the plugin undergoes continuous updates, new device categories will be added—and existing ones optimized—based on market feedback.
3. Supports automatic device discovery and binding.
4. Supports remote control of HA devices via the Tuya App, scene linkage, smart voice control, and more.

## Installation

### HACS Installation

1. Add this repository as a custom repository in HACS.
2. Search for "tuya_cloud_ha_bridge" and install it.

### Manual Installation

1. Download this repository.
2. Copy the files to the `custom_components/tuya_cloud_ha_bridge/` directory.
3. Restart Home Assistant.

### Installation config

1. Official URL to apply for a Tuya API Key: [https://tuya.ai/](https://tuya.ai/)
2. Download and install "Tuya Smart Life" or the "Tuya" app
3. Enter your API Key to generate a QR code. Scan the code using the App, then click the Submit button to complete the process

## Home Assistant Version

Home Assistant Version 2026.2.+




# LICENSE
[MIT License](LICENSE)