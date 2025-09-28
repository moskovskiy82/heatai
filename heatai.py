#!/usr/bin/env python3
import time
import logging
import requests
import yaml
import os

# =========================
# Load Configuration
# =========================
with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

HA_URL = os.getenv("HA_URL", cfg.get("DEFAULT_HA_URL", "http://homeassistant.local:8123"))
HA_TOKEN = os.getenv("HA_TOKEN")  # Required for HA API
MQTT_TOPIC = cfg["MQTT_TOPIC"]
LOG_LEVEL = getattr(logging, cfg.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
INTERVAL = cfg.get("LOOP_INTERVAL_SECONDS", 300)

# Entities
INSIDE_TEMP_ENTITY = cfg["INSIDE_TEMP_ENTITY"]
CURVE_FACTOR_ENTITY = cfg["CURVE_FACTOR_ENTITY"]
WEATHER_ENTITY = cfg["WEATHER_ENTITY"]
CALCULATED_FLOW_ENTITY = cfg["CALCULATED_FLOW_ENTITY"]
MANUAL_FLOW_ENTITY = cfg.get("MANUAL_FLOW_ENTITY", "input_number.heatai_manual_flowtemp")
HEATING_DISABLE_ENTITY = cfg["HEATING_DISABLE_ENTITY"]
WATERSTORAGE_DISABLE_ENTITY = cfg["WATERSTORAGE_DISABLE_ENTITY"]
MODE_ENTITY = cfg.get("MODE_ENTITY", "input_select.heatai")

# Defaults
DEFAULT_TI = cfg.get("DEFAULT_TI", 20.0)
DEFAULT_FACTOR = cfg.get("DEFAULT_FACTOR", 1.0)
DEFAULT_TA = cfg.get("DEFAULT_TA", 0.0)
MIN_FLOW_TEMP = cfg.get("MIN_FLOW_TEMP", 20.0)
MAX_FLOW_TEMP = cfg.get("MAX_FLOW_TEMP", 60.0)

FIXED_PARAMS = cfg["FIXED_PARAMS"]

# =========================
# Logging Setup
# =========================
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("heatai")

# =========================
# Helpers for HA API
# =========================
def ha_headers():
    if not HA_TOKEN:
        raise RuntimeError("Missing HA_TOKEN env var")
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

def ha_get_state(entity_id, default=None):
    try:
        url = f"{HA_URL}/api/states/{entity_id}"
        r = requests.get(url, headers=ha_headers(), timeout=5)
        if r.status_code == 200:
            return r.json().get("state", default)
        else:
            logger.warning("HA returned %s for %s", r.status_code, entity_id)
    except Exception as e:
        logger.error("Error fetching state for %s: %s", entity_id, e)
    return default

def ha_set_state(entity_id, value):
    try:
        url = f"{HA_URL}/api/states/{entity_id}"
        r = requests.post(url, headers=ha_headers(), json={"state": value}, timeout=5)
        if r.status_code != 200:
            logger.warning("Failed to update %s -> %s (code=%s)", entity_id, value, r.status_code)
    except Exception as e:
        logger.error("Error updating HA state for %s: %s", entity_id, e)

def mqtt_publish(topic, payload):
    """Publish via HA mqtt.publish service (NOT external broker)."""
    try:
        url = f"{HA_URL}/api/services/mqtt/publish"
        data = {"topic": topic, "payload": payload}
        r = requests.post(url, headers=ha_headers(), json=data, timeout=5)
        if r.status_code == 200:
            logger.info("Published to %s: %s", topic, payload)
        else:
            logger.error("HA mqtt.publish failed (code=%s): %s", r.status_code, r.text)
    except Exception as e:
        logger.error("Error calling HA mqtt.publish: %s", e)

# =========================
# Main Control Loop
# =========================
def control_boiler():
    mode = ha_get_state(MODE_ENTITY, "auto").lower()
    logger.debug("Current mode: %s", mode)

    # Read disables
    disablehc = "1" if ha_get_state(HEATING_DISABLE_ENTITY, "off") == "on" else "0"
    disablehwcload = "1" if ha_get_state(WATERSTORAGE_DISABLE_ENTITY, "off") == "on" else "0"

    # Always skip DHW temp → send "-" so boiler uses internal setting
    hwctempdesired = "-"

    # --------------------------
    # OFF MODE
    # --------------------------
    if mode == "off":
        params = FIXED_PARAMS.format(
            hcmode="0", flow="0", hwc="-", hwcflow="-",
            setmode1="-", disablehc=disablehc, disablehwctapping="0",
            disablehwcload=disablehwcload, setmode2="-",
            remoteControlHcPump="0", releaseBackup="0", releaseCooling="0",
        )
        mqtt_publish(MQTT_TOPIC, params)
        logger.info("Mode=OFF → letting boiler handle defaults (flow=0, hwc skipped).")
        return

    # --------------------------
    # MANUAL MODE
    # --------------------------
    if mode == "manual":
        try:
            flow_temp = float(ha_get_state(MANUAL_FLOW_ENTITY, DEFAULT_TI))
        except ValueError:
            flow_temp = DEFAULT_TI
        flow_temp = max(MIN_FLOW_TEMP, min(MAX_FLOW_TEMP, flow_temp))
        flowtempdesired = str(flow_temp)

        params = FIXED_PARAMS.format(
            hcmode="0", flow=flowtempdesired, hwc=hwctempdesired, hwcflow="-",
            setmode1="-", disablehc=disablehc, disablehwctapping="0",
            disablehwcload=disablehwcload, setmode2="-",
            remoteControlHcPump="0", releaseBackup="0", releaseCooling="0",
        )
        mqtt_publish(MQTT_TOPIC, params)
        logger.info("Mode=MANUAL → flowtemp=%s (hwc skipped)", flowtempdesired)
        return

    # --------------------------
    # AUTO MODE
    # --------------------------
    try:
        ti = float(ha_get_state(INSIDE_TEMP_ENTITY, DEFAULT_TI))
    except ValueError:
        ti = DEFAULT_TI
    try:
        factor = float(ha_get_state(CURVE_FACTOR_ENTITY, DEFAULT_FACTOR))
    except ValueError:
        factor = DEFAULT_FACTOR
    try:
        ta = float(ha_get_state(WEATHER_ENTITY, DEFAULT_TA))
    except ValueError:
        ta = DEFAULT_TA

    flow_temp = round(ti * factor - ta * factor + ti, 1)
    flow_temp = max(MIN_FLOW_TEMP, min(MAX_FLOW_TEMP, flow_temp))
    flowtempdesired = str(flow_temp)

    ha_set_state(CALCULATED_FLOW_ENTITY, flow_temp)

    params = FIXED_PARAMS.format(
        hcmode="0", flow=flowtempdesired, hwc=hwctempdesired, hwcflow="-",
        setmode1="-", disablehc=disablehc, disablehwctapping="0",
        disablehwcload=disablehwcload, setmode2="-",
        remoteControlHcPump="0", releaseBackup="0", releaseCooling="0",
    )
    mqtt_publish(MQTT_TOPIC, params)
    logger.info(
        "Mode=AUTO → flowtemp=%s, ti=%s, ta=%s, factor=%s, disablehc=%s, disablehwcload=%s (hwc skipped)",
        flowtempdesired, ti, ta, factor, disablehc, disablehwcload
    )

# =========================
# Run Loop
# =========================
if __name__ == "__main__":
    logger.info("Starting control loop (interval=%ss)...", INTERVAL)
    while True:
        try:
            control_boiler()
        except Exception as e:
            logger.error("Unexpected error in control_boiler: %s", e)
        time.sleep(INTERVAL)
