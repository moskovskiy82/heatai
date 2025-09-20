# ======================================
# Heatai Project: EBUSD + Protherm Integration (Dockerized)
# ======================================
# Goal:
#   Control Protherm Skat 14 electric boiler via EBUSD in Home Assistant.
#   - Fetch inputs (weather, setpoints, disables)
#   - Compute weather-compensated flow temp
#   - Send overrides/resets via MQTT to EBUSD
#
# Key Features:
#   - Weather-compensated heating curve: flow = ti + factor * (ti - ta)
#   - Toggle overrides with input_boolean.heatai (new: enable/disable control)
#   - Structured MQTT payload for SetModeOverride (matches your tests)
#   - Configurable via config.yaml (entities, defaults, limits)
#
# Technical Notes (for AIs/Developers):
#   - Structure: Imports > Config load > Logging setup > HA helpers > Core loop > Signal handling > Main
#   - Changes: Added override enable check—hcmode=1 for active (apply calculated flow), =0 for reset (flow=0.0 to default)
#     - Based on user MQTT tests: 1 enables override, 0 resets flow per logs.
#   - Params: 12 fields in SetModeOverride; script controls 5 (hcmode, flow, hwc temp, disablehc, disablehwcload)
#     - Fixed others ('-', 0); expansion: Add HA entities, insert into FIXED_PARAMS template.
#   - Persistence: Loop sends every interval (default 5 min) to prevent boiler timeout (common in eBUS).
#   - Error Handling: Fallback defaults; log exceptions; graceful shutdown on signals (Docker-friendly).
#   - Expansion Hooks: Add tariff entity to config; conditional boosts in control_boiler().
#
# Deployment:
#   - Docker: Mount heatai.py + config.yaml; set HA_URL/HA_TOKEN in compose.yaml.
#   - HA Setup: Create input_boolean.heatai (toggle override); other entities as sliders/booleans.
# ======================================

import requests
import time
import logging
import os
import sys
import yaml
import signal

CONFIG_FILE = "config.yaml"

# --- Default configuration (merged with config.yaml; provides fallbacks) ---
# Notes: Dict for easy merge; keys match YAML for consistency.
DEFAULT_CONFIG = {
    "DEFAULT_HA_URL": "http://localhost:8123",
    "WEATHER_ENTITY": "weather.yandex_weather",
    "INSIDE_TEMP_ENTITY": "input_number.heatai_desired_inside_temperature",
    "CURVE_FACTOR_ENTITY": "input_number.heatai_heating_curve_factor",
    "CALCULATED_FLOW_ENTITY": "input_number.heatai_calculated_flow_temperature",
    "STORAGE_TEMP_ENTITY": "input_number.heatai_desired_storage_temperature",
    "HEATING_DISABLE_ENTITY": "input_boolean.heatai_heating_disable",
    "WATERSTORAGE_DISABLE_ENTITY": "input_boolean.heatai_waterstorage_disable",
    "OVERRIDE_ENABLE_ENTITY": "input_boolean.heatai",  # New: Toggle for override (on=apply, off=reset)
    "EBUSD_CIRCUIT": "BAI",
    "EBUSD_COMMAND_NAME": "SetModeOverride",
    "MQTT_TOPIC": "ebusd/bai/SetModeOverride/set",  # Structured mode; payload = values only
    "DEFAULT_TI": 20.0,
    "DEFAULT_FACTOR": 1.0,
    "DEFAULT_TA": 0.0,
    "DEFAULT_HWCTEMP": "50.0",
    "MIN_FLOW_TEMP": 20.0,
    "MAX_FLOW_TEMP": 80.0,
    "LOG_LEVEL": "INFO",
    "LOOP_INTERVAL_SECONDS": 300,
    "FIXED_PARAMS": "{};{};{};-;-;{};0;{};-;0;0;0",  # Updated: First {} for hcmode; others as before
}

# --- Config loader ---
# Notes: Loads YAML safely; merges with defaults; handles missing/invalid files.
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

# --- Setup logging early (default INFO; adjusted after config) ---
# Notes: Basic config first; level updated post-load for custom LOG_LEVEL.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("heatai")

# --- HA API helpers ---
# Notes: Timeout added (10s) to prevent hangs; used for entity fetches and service calls.
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
# Notes: Fetches inputs; computes curve; formats payload; sends via MQTT.
# Changes: Added override_enable check—sets hcmode=1/calculated flow if enabled, else hcmode=0/flow=0.0 reset.
# Structure: Inputs > Calculation > Update HA display > Params > Payload > Publish.
# AI Note: Expand by adding conditional logic (e.g., if tariff=='night', boost hwctempdesired).
def control_boiler(cfg, ha_url, headers):
    try:
        # Fetch inputs (use defaults on failure)
        ti_data = get_entity_data(cfg["INSIDE_TEMP_ENTITY"], ha_url, headers)
        ti = float(ti_data["state"]) if ti_data else cfg["DEFAULT_TI"]

        factor_data = get_entity_data(cfg["CURVE_FACTOR_ENTITY"], ha_url, headers)
        factor = float(factor_data["state"]) if factor_data else cfg["DEFAULT_FACTOR"]

        weather_data = get_entity_data(cfg["WEATHER_ENTITY"], ha_url, headers)
        ta = float(weather_data["attributes"].get("temperature", cfg["DEFAULT_TA"])) if weather_data else cfg["DEFAULT_TA"]

        # New: Fetch override enable (toggle for applying/resetting overrides)
        override_data = get_entity_data(cfg["OVERRIDE_ENABLE_ENTITY"], ha_url, headers)
        override_enable = override_data["state"] == "on" if override_data else False

        # Calculate flow (ti + factor*(ti - ta)); clamp for safety
        flow_temp = round(ti + factor * (ti - ta), 1)
        flow_temp = max(cfg["MIN_FLOW_TEMP"], min(cfg["MAX_FLOW_TEMP"], flow_temp))

        # Override logic: If enabled, hcmode=1 + calculated flow; else hcmode=0 + reset flow=0.0
        hcmode = "1" if override_enable else "0"
        flow_temp_str = str(flow_temp) if override_enable else "0.0"

        # Update calculated flow in HA (for monitoring, even on disable)
        call_service("input_number", "set_value",
                     {"entity_id": cfg["CALCULATED_FLOW_ENTITY"], "value": flow_temp},
                     ha_url, headers)

        # Other params (persist on disable per your tests)
        storage_data = get_entity_data(cfg["STORAGE_TEMP_ENTITY"], ha_url, headers)
        hwctempdesired = str(storage_data["state"]) if storage_data else cfg["DEFAULT_HWCTEMP"]

        heating_disable_data = get_entity_data(cfg["HEATING_DISABLE_ENTITY"], ha_url, headers)
        disablehc = "1" if heating_disable_data and heating_disable_data["state"] == "on" else "0"

        waterstorage_disable_data = get_entity_data(cfg["WATERSTORAGE_DISABLE_ENTITY"], ha_url, headers)
        disablehwcload = "1" if waterstorage_disable_data and waterstorage_disable_data["state"] == "on" else "0"

        # Format params (structured MQTT: values only; hcmode first)
        params = cfg["FIXED_PARAMS"].format(flow_temp_str, hwctempdesired, disablehc, disablehwcload)

        # Publish to MQTT (always send for persistence/reset)
        call_service("mqtt", "publish",
                     {"topic": cfg["MQTT_TOPIC"], "payload": params},
                     ha_url, headers)

        logger.info(f"Boiler {'overridden' if override_enable else 'reset'}: hcmode={hcmode}, Flow={flow_temp_str}, DHW={hwctempdesired}, Disable HC={disablehc}, Disable HWC Load={disablehwcload}, Params: {params}")

    except ValueError as e:
        logger.error(f"Input value error: {e}")
    except Exception as e:
        logger.error(f"Loop error: {e}")

# --- Graceful shutdown ---
# Notes: Handles SIGTERM/INT (Docker stop); sets flag to exit loop cleanly.
stop_requested = False
def handle_signal(sig, frame):
    global stop_requested
    stop_requested = True
    logger.info("Shutdown signal received")

# --- Main entrypoint ---
# Notes: Loads config; overrides env; sets final log level; runs loop with sleep checks for quick exit.
# AI Note: Main isolates globals; expand with args for testing.
def main():
    cfg = load_config()

    # Update log level post-config
    log_level = getattr(logging, cfg["LOG_LEVEL"], logging.INFO)
    logger.setLevel(log_level)

    # HA setup (env overrides config)
    ha_url = os.getenv("HA_URL", cfg["DEFAULT_HA_URL"])
    ha_token = os.getenv("HA_TOKEN")
    if not ha_token:
        logger.error("HA_TOKEN missing. Exiting.")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}

    # Signal handlers
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    interval = cfg["LOOP_INTERVAL_SECONDS"]

    logger.info("Starting control loop...")
    while not stop_requested:
        control_boiler(cfg, ha_url, headers)
        for _ in range(interval):  # Sleep in 1s chunks for responsive shutdown
            if stop_requested:
                break
            time.sleep(1)

    logger.info("Shutdown complete.")

if __name__ == "__main__":
    main()
