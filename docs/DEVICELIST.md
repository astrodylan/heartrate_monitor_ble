# Compatible Devices List

This document outlines the **heart rate sensors** and **host computers** compatible with the BLE Heart Rate Monitor desktop application.

---

## 1. Compatible Heart Rate Sensors (BLE Peripherals)

Because the application implements the standard Bluetooth SIG **Heart Rate Service (`0x180D`)** and **Heart Rate Measurement Characteristic (`0x2A37`)**, it supports almost any standard Bluetooth Low Energy (BLE) heart rate monitor. *Note that this list is the expected to work devices, only the once marked with a X are tested*

### Chest Straps
* **Coospo:** H6(✅), H808S, H9Z
* **Polar:** H10, H9
* **Garmin:** HRM-Dual, HRM-Pro, HRM-Pro Plus
* **Wahoo:** TICKR, TICKR X
* **Magene:** H64, H303

### Optical Armbands
* **Polar:** Verity Sense, OH1
* **Coospo:** HW807, HW9
* **Wahoo:** TICKR FIT
* **Scosche:** Rhythm+

### Smartwatches & Fitness Trackers (HR Broadcast Mode)
* **Garmin:** Forerunner, Fenix, Venu, and Instinct series (requires enabling *Broadcast Heart Rate* on the watch)
* **COROS:** PACE, APEX, and VERTIX series (in HR Broadcast mode)
* **Whoop:** Whoop 4.0 (with HR Broadcast enabled in the mobile app)
* **Amazfit:** Select models supporting standard BLE Heart Rate Push


### DIY Heartrate Sensors
DIY Heartrate sensors could work as long as they broadcast the same signal as the heartrate tracker devices. See [ESP32_example_code.cpp](helper/ESP32_example_code.cpp) for an example code. This example code hardcodes the name broadcasted to the server. This code is expected to work for all ESP32 devices and might be updated to work with ESP8266 and/or arduino boards with WIFI.

### Sensor Feature Support Notes
| Bluetooth GATT Feature | UUID | App Behavior |
| :--- | :--- | :--- |
| **Heart Rate Measurement** | `00002a37-0000-1000-8000-00805f9b34fb` | **Required.** Streams live BPM (8-bit and 16-bit formats supported). |
| **R-R Intervals** | Flag bit `0x10` inside `0x2A37` | **Optional.** Plotted on the Detailed Graphs tab and exported to CSV/JSON/TXT if transmitted by the sensor. |
| **Battery Level** | `00002a19-0000-1000-8000-00805f9b34fb` | **Optional.** Displayed in Settings; gracefully defaults to *Unavailable* if unsupported by the sensor. |

### Incompatible Sensors
* **ANT+ Only Sensors:** Older straps without Bluetooth (e.g., Garmin HRM-Run, Garmin HRM-Tri).
* **Proprietary Watch OS Sensors:** Apple Watch and Samsung Galaxy Watch do not natively broadcast standard BLE Heart Rate profiles without a third-party broadcaster app installed on the watch.


---
*This is for informational purposes only. For medical advice or diagnosis, consult a professional. AI responses may include mistakes.*
