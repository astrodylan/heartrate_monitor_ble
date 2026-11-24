/*
 * ============================================================================
 * ESP32 IR LED Pulse Sensor
 * ============================================================================
 * 
 * OVERVIEW:
 *   Reads heart rate (BPM) using two standard IR LEDs facing each other with 
 *   a finger placed in between, then sends the data to a FastAPI server.
 * 
 * HOW IT WORKS:
 *   The transmitter IR LED shines light through the finger to the receiver 
 *   IR LED on GPIO17. As blood pumps through the finger, it changes how much 
 *   IR light passes through, shifting the receiver's discharge time with each beat.
 * 
 * CONNECTION DIAGRAM:
 *   Transmitter IR LED Anode   (long leg)  ---> 3.3V (via 100Ω or 220Ω resistor)
 *   Transmitter IR LED Cathode (short leg) ---> GND
 *   Receiver IR LED Anode      (short leg) ---> GPIO17
 *   Receiver IR LED Cathode    (long leg)  ---> GND
 *   Onboard Indicator LED                  ---> GPIO2 (Built-in)
 * ============================================================================
 */



#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>

// ==========================================
// 1. HARDCODED WIFI & SERVER SETTINGS
// ==========================================
const char* WIFI_SSID     = "WIFI SSID";    // Replace with your Wi-Fi credentials
const char* WIFI_PASSWORD = "WIFI_PASSWORD";// Replace with your Wi-Fi credentials
const char* SERVER_URL    = "SERVER_URL";   // Replace with your FastAPI server URL
const char* CLIENT_NAME   = "SENSOR NAME";   // Replace with a unique name for this sensor device
// ==========================================

const int sensorLedPin = 17; // Sensor IR LED cathode (short leg)
const int beatLedPin = 2;    // Built-in LED on ESP32

float fastAvg = 0;
float slowAvg = 0;
float signalPeak = 2.0;
bool beatDetected = false;
unsigned long lastBeatTime = 0;
unsigned long lastPrintTime = 0;
unsigned long fingerPlacedTime = 0;
unsigned long lastDeltaMs = 0;
bool fingerPresent = false;

const int NUM_SAMPLES = 4;
int bpmHistory[NUM_SAMPLES] = {0, 0, 0, 0};
int bpmIndex = 0;
int validBeats = 0;

String getIsoUtcTimestamp() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo, 10)) {
    return "2026-01-01T00:00:00Z";
  }
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &timeinfo);
  return String(buf);
}

void sendTelemetryToServer(int bpm, unsigned long rrMs) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(" | WiFi disconnected!");
    return;
  }

  WiFiClient client;
  HTTPClient http;
  http.setTimeout(2000);

  if (!http.begin(client, SERVER_URL)) {
    Serial.println(" | HTTP begin() failed (check URL format)");
    return;
  }
  http.addHeader("Content-Type", "application/json");

  String payload = "{";
  payload += "\"timestamp\":\"" + getIsoUtcTimestamp() + "\",";
  payload += "\"device_id\":\"" + WiFi.macAddress() + "\",";
  payload += "\"client_name\":\"" + String(CLIENT_NAME) + "\",";
  payload += "\"bpm\":" + String(bpm) + ",";
  payload += "\"rr_intervals_ms\":[" + String(rrMs) + "]";
  payload += "}";

  int httpCode = http.POST(payload);
  Serial.print(" | Server HTTP: ");
  Serial.print(httpCode);
  if (httpCode < 0) {
    Serial.print(" (");
    Serial.print(http.errorToString(httpCode));
    Serial.print(")");
  }
  Serial.println();
  http.end();
}

long readLightTime() {
  pinMode(sensorLedPin, OUTPUT);
  digitalWrite(sensorLedPin, HIGH);
  delayMicroseconds(40);

  pinMode(sensorLedPin, INPUT);
  unsigned long startTime = micros();
  
  while (digitalRead(sensorLedPin) == HIGH) {
    if (micros() - startTime > 20000) break;
  }
  
  return micros() - startTime;
}

// Fixed 32-sample average eliminates timing jitter & quantization steps
float readSmoothLight() {
  long sum = 0;
  for (int i = 0; i < 32; i++) {
    sum += readLightTime();
  }
  return (float)sum / 32.0;
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(beatLedPin, OUTPUT);

  // Connect to Wi-Fi
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWi-Fi connected!");

  // Sync UTC time via NTP
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");

  float initVal = readSmoothLight();
  fastAvg = initVal;
  slowAvg = initVal;
  Serial.println("Ready. Place finger gently and hold still...");
}

void loop() {
  float rawLight = readSmoothLight();

  // Check if finger is covering the LEDs
  if (rawLight < 40 || rawLight > 18000) {
    fingerPresent = false;
    validBeats = 0;
    bpmIndex = 0;
    lastDeltaMs = 0;
    signalPeak = 2.0;
    fastAvg = rawLight;
    slowAvg = rawLight;
    digitalWrite(beatLedPin, LOW);

    if (millis() - lastPrintTime >= 1000) {
      lastPrintTime = millis();
      Serial.print("Place finger on sensor... | Raw(us): ");
      Serial.println((int)rawLight);
    }
    return;
  }

  // Record when finger first entered so we ignore the huge initial insertion spike
  if (!fingerPresent) {
    fingerPresent = true;
    fingerPlacedTime = millis();
    fastAvg = rawLight;
    slowAvg = rawLight;
    signalPeak = 2.0;
  }

  // Bandpass filter tuned for ~120-150 Hz loop speed
  fastAvg = (0.85 * fastAvg) + (0.15 * rawLight);
  slowAvg = (0.96 * slowAvg) + (0.04 * rawLight);
  float pulseSignal = fastAvg - slowAvg;

  // Wait 1.5s after finger placement for filters to settle before peak tracking
  if (millis() - fingerPlacedTime < 1500) {
    delay(5);
    return;
  }

  // Faster decay (0.975) prevents missing beats after a slightly stronger pulse
  signalPeak *= 0.975;
  if (signalPeak < 1.5) signalPeak = 1.5;
  if (pulseSignal > signalPeak && pulseSignal < 30.0) {
    signalPeak = pulseSignal;
  }

  // Trigger at 50% of current peak height
  float dynamicThreshold = signalPeak * 0.50;

  if (pulseSignal > dynamicThreshold && !beatDetected) {
    unsigned long now = millis();
    unsigned long delta = now - lastBeatTime;

    // Accept 45 to 130 BPM (460ms to 1333ms) - blocks dicrotic notch double-counting
    if (delta > 460 && delta < 1333) {
      lastDeltaMs = delta;
      int instantBpm = 60000 / delta;
      bpmHistory[bpmIndex] = instantBpm;
      bpmIndex = (bpmIndex + 1) % NUM_SAMPLES;
      if (validBeats < NUM_SAMPLES) validBeats++;
    }

    lastBeatTime = now;
    beatDetected = true;
    digitalWrite(beatLedPin, HIGH);
  }

  // Reset trigger when wave drops back near zero
  if (pulseSignal < (signalPeak * 0.15)) {
    beatDetected = false;
    digitalWrite(beatLedPin, LOW);
  }

  if (millis() - lastPrintTime >= 1000) {
    lastPrintTime = millis();

    if (validBeats >= 2) {
      int sum = 0;
      for (int i = 0; i < validBeats; i++) sum += bpmHistory[i];
      int avgBpm = sum / validBeats;

      Serial.print("Heart Rate: ");
      Serial.print(avgBpm);
      Serial.print(" BPM  |  Pulse Amp: ");
      Serial.print(signalPeak, 1);
      Serial.print(" us  |  Raw: ");
      Serial.print((int)rawLight);

      // Stream live BPM & R-R interval to your FastAPI server
      sendTelemetryToServer(avgBpm, lastDeltaMs);
    } else {
      Serial.print("Settling... | Pulse Amp: ");
      Serial.print(signalPeak, 1);
      Serial.print(" us  |  Raw: ");
      Serial.println((int)rawLight);
    }
  }

  delay(5);
}
