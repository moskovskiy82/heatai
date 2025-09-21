#!/usr/bin/env python3
import time
import logging
import requests
import yaml
import paho.mqtt.publish as publish
from datetime import datetime

# =========================
# Load Configuration
# =========================
with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

HA_URL = cfg.get("DEFAULT_HA_URL", "http://homeassistant.local:8123")
MQTT_TOPIC = cfg["MQTT_TOPIC"]
LOG_LEVEL = getattr(logging, cfg.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
INTERVAL = cfg.get("LOOP_INTERVAL_SECONDS", 300)

# Entities
INSIDE_TEMP_ENTITY = cfg["INSIDE_TEMP_ENTITY"]
CURVE_FACTOR_ENTITY = cfg["CURVE_FACTOR_ENTITY"]
WEATHER_ENTITY = cfg["WEATHER_ENTITY"]
CALCULATED_FLOW_ENTITY = cfg["CALCULATED_FLOW_ENTITY"]
STORAGE_TEMP_ENTITY = cfg["STORAGE_TEMP_ENTITY"]
HEATING_DISABLE_ENTITY = cfg["HEATING_DISABLE_ENTITY"]
WATERSTORAGE_DISABLE_ENTITY = cfg["WATERSTORAGE_DISABLE_ENTITY"]

# Defaults
DEFAULT_TI = cfg.get("DEFAULT_TI", 20.0)
DEFAULT_FACTOR = cfg.get("DEFAULT_FACTOR", 1.0)
DEFAULT_TA = cfg.get("DEFAULT_TA", 0.0)
DEFAULT_HWCTEMP = cfg.get("DEFAULT_HWCTEMP", "50.0")
MIN_FLOW_TEMP = cfg.get("MIN_FLOW_TEMP", 20.0)
MAX_FLOW_TEMP = cfg.get("MAX_FLOW_TEMP", 80.0)

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
def ha_get_state(entity_id, default=None):
    """Fetch state from Home Assistant API."""
    try:
        url = f"{HA_URL}/api/states/{entity_id}"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data.get("state", default)
        else:
            logger.warning("HA returned %s for %s", r.status_code, entity_id)
    except Exception as e:
        logger.error("Error fetching state for %s: %s", entity_id, e)
    return default

def mqtt_publish(topic, payload):
    """Publish MQTT message."""
    try:
        publish.single(topic, payload, hostname="localhost")
        logger.info("Published to %s: %s", topic, payload)
    except Exception as e:
        logger.error("Error publishing MQTT: %s", e)

# =========================
# Main Control Loop
# =========================
def control_boiler():
    # Inside temperature setpoint
    try:
        ti = float(ha_get_state(INSIDE_TEMP_ENTITY, DEFAULT_TI))
    except ValueError:
        ti = DEFAULT_TI

    # Curve factor
    try:
        factor = float(ha_get_state(CURVE_FACTOR_ENTITY, DEFAULT_FACTOR))
    except ValueError:
        factor = DEFAULT_FACTOR

    # Outside temperature
    try:
        ta = float(
            ha_get_state(f"{WEATHER_ENTITY}", DEFAULT_TA)
            or DEFAULT_TA
        )
    except ValueError:
        ta = DEFAULT_TA

    # Calculate flow temperature
    flow_temp = round(ti * factor - ta * factor + ti, 1)
    flow_temp = max(MIN_FLOW_TEMP, min(MAX_FLOW_TEMP, flow_temp))

    # Write back to HA
    try:
        url = f"{HA_URL}/api/states/{CALCULATED_FLOW_ENTITY}"
        requests.post(url, json={"state": flow_temp})
    except Exception as e:
        logger.warning("Could not update HA with flow temp: %s", e)

    flowtempdesired = str(flow_temp)
    hwctempdesired = ha_get_state(STORAGE_TEMP_ENTITY, DEFAULT_HWCTEMP)

    # Read disable booleans
    disablehc_state = ha_get_state(HEATING_DISABLE_ENTITY, "off")
    disablehwcload_state = ha_get_state(WATERSTORAGE_DISABLE_ENTITY, "off")

    disablehc = "1" if disablehc_state == "on" else "0"
    disablehwcload = "1" if disablehwcload_state == "on" else "0"

    # Fill params string
    params = FIXED_PARAMS.format(
        hcmode="0",
        flow=flowtempdesired,
        hwc=hwctempdesired,
        hwcflow="0",
        setmode1="0",
        disablehc=disablehc,
        disablehwctapping="0",
        disablehwcload=disablehwcload,
        setmode2="0",
        remoteControlHcPump="0",
        releaseBackup="0",
        releaseCooling="0",
    )

    # Publish to MQTT
    mqtt_publish(MQTT_TOPIC, params)

    logger.info(
        "Flow temp=%s, ti=%s, ta=%s, factor=%s, disablehc=%s, disablehwcload=%s",
        flowtempdesired, ti, ta, factor, disablehc, disablehwcload
    )

# =========================
# Run Loop
# =========================
if __name__ == "__main__":
    logger.info(
        "Starting control loop (interval=%ss)...",
        INTERVAL
    )
    while True:
        try:
            control_boiler()
        except Exception as e:
            logger.error("Unexpected error in control_boiler: %s", e)
        time.sleep(INTERVAL)
