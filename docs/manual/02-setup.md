# Installation

For installing up there are two option: Client and Server.

## Client
Pre-built standalone executables for **Windows**, **macOS**, and **Linux** are available on the **[Releases](../../releases)** page—no Python installation required.

### Windows
1. Download `HartRateMonitor-Windows.exe` from the latest Release.
2. Double-click to run. *(A Windows SmartScreen (red/blue) will likely appears, click **More info** → **Run anyway**)*. *This warning is for a service/app that is unknown to windows*

### macOS
1. Download `HartRateMonitor-macOS.zip` from the latest Release and unzip it.
2. **Right-click** `HartRateMonitor.app` → **Open** on the first launch to bypass Gatekeeper.
3. Grant Bluetooth permissions when prompted in **System Settings → Privacy & Security → Bluetooth**.

### Linux
1. Download `HartRateMonitor-Linux` from the latest Release.
2. Make the binary executable and run it:
   ```bash
   chmod +x HartRateMonitor-Linux
   ./HartRateMonitor-Linux
   ```
   *(Ensure your user belongs to the `bluetooth` group: `sudo usermod -aG bluetooth $USER`)*.

Now that the client is running, start setting up the server or continue to the next step

## Server
*Before installing the server side, make sure python is installed on the server device*


For the server, download the server folder from the main page. This folder has two files: server.py and requirements.txt. 


To install the server code, run the following commands.

```bash
cd server
pip install -r requirements.txt
python server.py
```
For reaching the dashboard connected to the server, go to the following url in your favorite browser.

* **Web Dashboard:** `http://<LOCAL-IP>>:8000`
* **Interactive API Docs:** `http://<LOCAL-IP>:8000/docs`

Now that the interface is open, we can go to the next step: setting up the device(s)


---
[Next: Setup →](./03-setup.md)