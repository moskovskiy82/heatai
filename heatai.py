# ======================================
# Heatai Project: EBUSD + Protherm Integration (Dockerized)
# ======================================
# Goal:
#   Control Protherm Skat 14 electric boiler via EBUSD in Home Assistant.
#   - Fetch inputs (weather, setpoints, disables, mode)
#   - Compute weather-compensated flow temp OR respect manual flow
#   - Send overrides/resets via MQTT to EBUSD
#
# Modes (input_select.heatai):
#   - off    → reset boiler once, then stop publishing (boiler runs internal program)
#   - room   → weather-compensated curve: flow = ti + factor * (ti - ta)
#   - manual → respect manual input_number.heatai_calculated_flow_temperature
#
# Technical Notes:
#   - Params: 12 fields in SetModeOverride; script controls 5 (hcmode, flow, hwctemp, disablehc, disablehwcload)
#   - Persistence: Loop sends every interval (default 5 min) in active modes
#   - Reset: Sent only once when switching to off
# ======================================

import requests
import time
import logging
import os
import sys
import yaml
import signal

CONFIG_FILE = "config.yaml"

# --- Default configuration ---
DEFAULT_CONFIG = {
    "DEFAULT_HA_URL": "http://localhost:8123",
    "WEATHER_ENTITY": "weather.yandex_weather",
    "INSIDE_TEMP_ENTITY": "input_number.heatai_desired_inside_temperature",
    "CURVE_FACTOR_ENTITY": "input_number.heatai_heating_curve_factor",
    "CALCULATED_FLOW_ENTITY": "input_number.heatai_calculated_flow_temperature",
    "STORAGE_TEMP_ENTITY": "input_number.heatai_desired_storage_temperature",
    "HEATING_DISABLE_ENTITY": "input_boolean.heatai_heating_disable",
    "WATERSTORAGE_DISABLE_ENTITY": "input_boolean.heatai_waterstorage_disable",
    "MODE_ENTITY": "input_select.heatai",  # New: off / room / manual
    "EBUSD_CIRCUIT": "BAI",
    "EBUSD_COMMAND_NAME": "SetModeOverride",
    "MQTT_TOPIC": "ebusd/bai/SetModeOverride/set",
    "DEFAULT_TI": 20.0,
    "DEFAULT_FACTOR": 1.0,
    "DEFAULT_TA": 0.0,
    "DEFAULT_HWCTEMP": "50.0",
    "MIN_FLOW_TEMP": 20.0,
    "MAX_FLOW_TEMP": 80.0,
    "LOG_LEVEL": "INFO",
    "LOOP_INTERVAL_SECONDS": 300,
    # Explicit hcmode placeholder first for clarity
    "FIXED_PARAMS": "{hcmode};{flow};{hwc};-;-;{disablehc};0;{disablehwcload};-;0;0;0",
}

# --- Setup logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("heatai")

# --- Config loader ---
def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            user_conf = yaml.safe_load(f) or {}
        cfg = {**DEFAULT_CONFIG, **user_conf}
        logger.info(f"Loaded config from {CONFIG_FILE}")
    except FileNotFoundError:
        cfg = DEFAULT_CONFIG
        logger.warning(f"{CONFIG_FILE} not found; using defaults")
    except yaml.YAMLError as e:
        cfg = DEFAULT_CONFIG
        logger.error(f"Parse error in {CONFIG_FILE}: {e}; using defaults")
    return cfg

# --- HA API helpers ---
def get_entity_data(entity_id, ha_url, headers):
    url = f"{ha_url}/api/states/{entity_id}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error(f"Fetch error for {entity_id}: {e}")
        return None

def call_service(domain, service, service_data, ha_url, headers):
    url = f"{ha_url}/api/services/{domain}/{service}"
    try:
        r = requests.post(url, headers=headers, json=service_data, timeout=10)
        r.raise_for_status()
        logger.debug(f"Called {domain}.{service}: {service_data}")
    except requests.RequestException as e:
        logger.error(f"Service error {domain}.{service}: {e}")

# --- Core control loop ---
last_mode = None  # remember last mode to avoid repeated resets

def control_boiler(cfg, ha_url, headers):
    global last_mode
    try:
        # Mode selection
        mode_data = get_entity_data(cfg["MODE_ENTITY"], ha_url, headers)
        mode = mode_data["state"] if mode_data else "off"

        # Shared params
        storage_data = get_entity_data(cfg["STORAGE_TEMP_ENTITY"], ha_url, headers)
        hwctempdesired = str(storage_data["state"]) if storage_data else cfg["DEFAULT_HWCTEMP"]

        heating_disable_data = get_entity_data(cfg["HEATING_DISABLE_ENTITY"], ha_url, headers)
        disablehc = "1" if heating_disable_data and heating_disable_data["state"] == "on" else "0"

        waterstorage_disable_data = get_entity_data(cfg["WATERSTORAGE_DISABLE_ENTITY"], ha_url, headers)
        disablehwcload = "1" if waterstorage_disable_data and waterstorage_disable_data["state"] == "on" else "0"

        # Defaults
        hcmode = "0"
        flow_temp_str = "0.0"

        if mode == "off":
            if last_mode != "off":
                params = cfg["FIXED_PARAMS"].format(
                    hcmode=hcmode,
                    flow=flow_temp_str,
                    hwc=hwctempdesired,
                    disablehc=disablehc,
                    disablehwcload=disablehwcload,
                )
                call_service("mqtt", "publish",
                             {"topic": cfg["MQTT_TOPIC"], "payload": params},
                             ha_url, headers)
                logger.info("Boiler reset (mode=off). Params: %s", params)

        elif mode == "room":
            ti_data = get_entity_data(cfg["INSIDE_TEMP_ENTITY"], ha_url, headers)
            ti = float(ti_data["state"]) if ti_data else cfg["DEFAULT_TI"]

            factor_data = get_entity_data(cfg["CURVE_FACTOR_ENTITY"], ha_url, headers)
            factor = float(factor_data["state"]) if factor_data else cfg["DEFAULT_FACTOR"]

            weather_data = get_entity_data(cfg["WEATHER_ENTITY"], ha_url, headers)
            ta = float(weather_data["attributes"].get("temperature", cfg["DEFAULT_TA"])) if weather_data else cfg["DEFAULT_TA"]

            flow_temp = round(ti + factor * (ti - ta), 1)
            flow_temp = max(cfg["MIN_FLOW_TEMP"], min(cfg["MAX_FLOW_TEMP"], flow_temp))
            flow_temp_str = str(flow_temp)
            hcmode = "1"

            call_service("input_number", "set_value",
                         {"entity_id": cfg["CALCULATED_FLOW_ENTITY"], "value": flow_temp},
                         ha_url, headers)

            params = cfg["FIXED_PARAMS"].format(
                hcmode=hcmode,
                flow=flow_temp_str,
                hwc=hwctempdesired,
                disablehc=disablehc,
                disablehwcload=disablehwcload,
            )
            call_service("mqtt", "publish",
                         {"topic": cfg["MQTT_TOPIC"], "payload": params},
                         ha_url, headers)
            logger.info("Boiler set (mode=room). Flow=%s, Params: %s", flow_temp_str, params)

        elif mode == "manual":
            manual_data = get_entity_data(cfg["CALCULATED_FLOW_ENTITY"], ha_url, headers)
            flow_temp_str = str(manual_data["state"]) if manual_data else str(cfg["DEFAULT_TI"])
            hcmode = "1"

            params = cfg["FIXED_PARAMS"].format(
                hcmode=hcmode,
                flow=flow_temp_str,
                hwc=hwctempdesired,
                disablehc=disablehc,
                disablehwcload=disablehwcload,
            )
            call_service("mqtt", "publish",
                         {"topic": cfg["MQTT_TOPIC"], "payload": params},
                         ha_url, headers)
            logger.info("Boiler set (mode=manual). Flow=%s, Params: %s", flow_temp_str, params)

        last_mode = mode

    except Exception as e:
        logger.error(f"Loop error: {e}")

# --- Graceful shutdown ---
stop_requested = False
def handle_signal(sig, frame):
    global stop_requested
    stop_requested = True
    logger.info("Shutdown signal received")

# --- Main entrypoint ---
def main():
    cfg = load_config()
    log_level = getattr(logging, cfg["LOG_LEVEL"], logging.INFO)
    logger.setLevel(log_level)

    ha_url = os.getenv("HA_URL", cfg["DEFAULT_HA_URL"])
    ha_token = os.getenv("HA_TOKEN")
    if not ha_token:
        logger.error("HA_TOKEN missing. Exiting.")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    interval = cfg["LOOP_INTERVAL_SECONDS"]

    logger.info("Starting control loop...")
    while not stop_requested:
        control_boiler(cfg, ha_url, headers)
        for _ in range(interval):
            if stop_requested:
                break
            time.sleep(1)

    logger.info("Shutdown complete.")

if __name__ == "__main__":
    main()
