#!/usr/bin/env python3
"""
Heatai: control Protherm SKAT via ebusd using Home Assistant as MQTT gateway.

Behaviour:
 - Modes (input_select.heatai):
     - off    : send a single reset payload so the boiler reverts to its internal curve
     - manual : read input_number.heatai_manual_flowtemp and publish that value
     - auto   : calculate flow from inside/outside/factor, write redundant HA sensor, publish value
 - Uses Home Assistant REST API for:
     - reading states
     - writing the redundant calculated sensor (AUTO mode)
     - calling HA service mqtt.publish (to send ebusd MQTT payloads) — NO direct broker connection
 - Requires HA_TOKEN in environment (long-lived token).
"""

import os
import time
import logging
import requests
import yaml
import sys

# -------------------------
# Load configuration
# -------------------------
CONFIG_FILE = "config.yaml"
try:
    with open(CONFIG_FILE, "r") as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    print(f"Configuration file {CONFIG_FILE} not found. Exiting.", file=sys.stderr)
    sys.exit(1)

# HA config (env overrides config)
HA_URL = os.getenv("HA_URL", cfg.get("DEFAULT_HA_URL", "http://localhost:8123"))
HA_TOKEN = os.getenv("HA_TOKEN") or cfg.get("HA_TOKEN")
if not HA_TOKEN:
    print("ERROR: HA_TOKEN not set (provide via environment). Exiting.", file=sys.stderr)
    sys.exit(1)

# Read main config values
MQTT_TOPIC = cfg.get("MQTT_TOPIC", "ebusd/bai/SetModeOverride/set")
LOG_LEVEL = getattr(logging, cfg.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
INTERVAL = int(os.getenv("LOOP_INTERVAL_SECONDS", cfg.get("LOOP_INTERVAL_SECONDS", 300)))

# Entities (from config.yaml)
INSIDE_TEMP_ENTITY = cfg.get("INSIDE_TEMP_ENTITY", "input_number.heatai_desired_inside_temperature")
CURVE_FACTOR_ENTITY = cfg.get("CURVE_FACTOR_ENTITY", "input_number.heatai_heating_curve_factor")
WEATHER_ENTITY = cfg.get("WEATHER_ENTITY", "weather.yandex_weather")

# Calculated flow: now a sensor (we will write to it in AUTO mode as redundant backup)
CALCULATED_FLOW_ENTITY = cfg.get("CALCULATED_FLOW_ENTITY", "sensor.heatai_calculated_flowtemp")

# Manual flow: separate entity used only in MANUAL mode (prevents overwrite conflicts)
MANUAL_FLOW_ENTITY = cfg.get("MANUAL_FLOW_ENTITY", "input_number.heatai_manual_flowtemp")

STORAGE_TEMP_ENTITY = cfg.get("STORAGE_TEMP_ENTITY", "input_number.heatai_desired_storage_temperature")
HEATING_DISABLE_ENTITY = cfg.get("HEATING_DISABLE_ENTITY", "input_boolean.heatai_heating_disable")
WATERSTORAGE_DISABLE_ENTITY = cfg.get("WATERSTORAGE_DISABLE_ENTITY", "input_boolean.heatai_waterstorage_disable")

# Mode selector
MODE_ENTITY = cfg.get("MODE_ENTITY", "input_select.heatai")

# Limits / defaults
DEFAULT_TI = float(cfg.get("DEFAULT_TI", 20.0))
DEFAULT_FACTOR = float(cfg.get("DEFAULT_FACTOR", 1.0))
DEFAULT_TA = float(cfg.get("DEFAULT_TA", 0.0))
DEFAULT_HWCTEMP = str(cfg.get("DEFAULT_HWCTEMP", "50.0"))
MIN_FLOW_TEMP = float(cfg.get("MIN_FLOW_TEMP", 20.0))
MAX_FLOW_TEMP = float(cfg.get("MAX_FLOW_TEMP", 80.0))

# The SetModeOverride template — should match your ebusd config mapping.
FIXED_PARAMS = cfg.get(
    "FIXED_PARAMS",
    "{hcmode};{flow};{hwc};{hwcflow};{setmode1};{disablehc};{disablehwctapping};{disablehwcload};{setmode2};{remoteControlHcPump};{releaseBackup};{releaseCooling}"
)

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("heatai")

# -------------------------
# Helpers: HA API
# -------------------------
def ha_headers():
    """Return headers for Home Assistant API calls (requires HA_TOKEN)."""
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

def ha_get_state(entity_id, default=None):
    """Fetch HA entity state via GET /api/states/<entity_id>. Returns state (string) or default."""
    try:
        url = f"{HA_URL}/api/states/{entity_id}"
        r = requests.get(url, headers=ha_headers(), timeout=8)
        if r.status_code == 200:
            return r.json().get("state", default)
        else:
            logger.warning("HA returned %s for %s", r.status_code, entity_id)
    except Exception as e:
        logger.error("Error fetching state for %s: %s", entity_id, e)
    return default

def ha_set_state(entity_id, value):
    """
    Write state into HA (POST /api/states/<entity_id>).
    Used only in AUTO mode as a redundant backup for the calculated flow sensor.
    """
    try:
        url = f"{HA_URL}/api/states/{entity_id}"
        r = requests.post(url, headers=ha_headers(), json={"state": value}, timeout=8)
        if r.status_code not in (200, 201):
            logger.warning("Failed to update %s -> %s (code=%s)", entity_id, value, r.status_code)
    except Exception as e:
        logger.error("Error updating HA state for %s: %s", entity_id, e)

def ha_mqtt_publish(topic, payload, qos=0, retain=False):
    """
    Use Home Assistant's mqtt.publish service to send a message to the broker.
    This avoids needing direct broker creds inside this container.
    """
    try:
        service_url = f"{HA_URL}/api/services/mqtt/publish"
        json_body = {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        r = requests.post(service_url, headers=ha_headers(), json=json_body, timeout=8)
        # HA returns 200 on success; log appropriately
        if r.status_code in (200, 201):
            logger.debug("HA mqtt.publish OK for topic %s", topic)
        else:
            logger.warning("HA mqtt.publish returned %s for topic %s", r.status_code, topic)
    except Exception as e:
        logger.error("Error calling HA mqtt.publish service: %s", e)

# -------------------------
# Main control logic
# -------------------------
_last_mode_sent_reset = None  # remember if we already sent OFF-mode reset

def control_boiler():
    global _last_mode_sent_reset

    # Read mode (safe default 'auto' if missing)
    mode_raw = ha_get_state(MODE_ENTITY, "auto")
    mode = (mode_raw or "auto").lower()
    logger.debug("Mode: %s", mode)

    # Read booleans that map to disable flags (work in all modes)
    disablehc = "1" if ha_get_state(HEATING_DISABLE_ENTITY, "off") == "on" else "0"
    disablehwcload = "1" if ha_get_state(WATERSTORAGE_DISABLE_ENTITY, "off") == "on" else "0"

    # Always read DHW setpoint so DHW behavior works in any mode
    hwctempdesired = ha_get_state(STORAGE_TEMP_ENTITY, DEFAULT_HWCTEMP)

    # --------------------------
    # OFF mode: send reset once, then no continuous overrides
    # --------------------------
    if mode == "off":
        if _last_mode_sent_reset != "off":
            params = FIXED_PARAMS.format(
                hcmode="0",
                flow="0",
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
            # Use HA mqtt.publish service (no direct broker creds required)
            ha_mqtt_publish(MQTT_TOPIC, params)
            logger.info("Mode=OFF: sent reset payload (letting boiler use internal curve).")
            _last_mode_sent_reset = "off"
        else:
            logger.debug("Mode=OFF: reset already sent — skipping.")
        return

    # Any non-off mode: clear the last-mode reset marker so future OFF transitions send again
    _last_mode_sent_reset = None

    # --------------------------
    # MANUAL mode: read manual flow input and publish it
    # --------------------------
    if mode == "manual":
        try:
            flow_temp = float(ha_get_state(MANUAL_FLOW_ENTITY, DEFAULT_TI))
        except (ValueError, TypeError):
            flow_temp = DEFAULT_TI
        # clamp
        flow_temp = max(MIN_FLOW_TEMP, min(MAX_FLOW_TEMP, flow_temp))
        flowtempdesired = str(flow_temp)

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
        ha_mqtt_publish(MQTT_TOPIC, params)
        logger.info("Mode=MANUAL → flowtemp=%s, hwc=%s", flowtempdesired, hwctempdesired)
        # IMPORTANT: do NOT write to CALCULATED_FLOW_ENTITY in manual mode (prevents confusion)
        return

    # --------------------------
    # AUTO mode: calculate curve, write backup sensor, publish
    # --------------------------
    # Read inputs
    try:
        ti = float(ha_get_state(INSIDE_TEMP_ENTITY, DEFAULT_TI))
    except (ValueError, TypeError):
        ti = DEFAULT_TI

    try:
        factor = float(ha_get_state(CURVE_FACTOR_ENTITY, DEFAULT_FACTOR))
    except (ValueError, TypeError):
        factor = DEFAULT_FACTOR

    try:
        ta = float(ha_get_state(WEATHER_ENTITY, DEFAULT_TA))
    except (ValueError, TypeError):
        ta = DEFAULT_TA

    # Compute flow temp (your chosen formula)
    flow_temp = round(ti * factor - ta * factor + ti, 1)
    flow_temp = max(MIN_FLOW_TEMP, min(MAX_FLOW_TEMP, flow_temp))
    flowtempdesired = str(flow_temp)

    # Redundant backup: write to HA sensor so GUI shows the computed flow (only in AUTO)
    # Note: this is optional when HA template sensor already exists; we keep it as requested.
    ha_set_state(CALCULATED_FLOW_ENTITY, flow_temp)

    # Format and publish payload via HA mqtt.publish service
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
    ha_mqtt_publish(MQTT_TOPIC, params)
    logger.info(
        "Mode=AUTO → flowtemp=%s, ti=%s, ta=%s, factor=%s, disablehc=%s, disablehwcload=%s",
        flowtempdesired, ti, ta, factor, disablehc, disablehwcload
    )

# -------------------------
# Run loop
# -------------------------
if __name__ == "__main__":
    logger.info("Starting control loop (interval=%ss)...", INTERVAL)
    while True:
        try:
            control_boiler()
        except Exception as e:
            logger.exception("Unexpected error in control_boiler: %s", e)
        time.sleep(INTERVAL)
