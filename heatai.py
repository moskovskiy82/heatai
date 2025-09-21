# ======================================
# Heatai Project: EBUSD + Protherm Integration (Dockerized)
# ======================================
# Modes (input_select.heatai): off / room / manual
# - off: send reset once, then stop publishing
# - room: calculate flow = ti + factor*(ti - ta) and publish periodically
# - manual: read input_number.heatai_calculated_flow_temperature and publish that
# This script publishes numeric 12-field SetModeOverride payloads to ebusd via HA mqtt.publish.
# ======================================

import requests
import time
import logging
import os
import sys
import yaml
import signal

CONFIG_FILE = "config.yaml"

# defaults (merge with config.yaml)
DEFAULT_CONFIG = {
    "DEFAULT_HA_URL": "http://localhost:8123",
    "WEATHER_ENTITY": "weather.yandex_weather",
    "INSIDE_TEMP_ENTITY": "input_number.heatai_desired_inside_temperature",
    "CURVE_FACTOR_ENTITY": "input_number.heatai_heating_curve_factor",
    "CALCULATED_FLOW_ENTITY": "input_number.heatai_calculated_flow_temperature",
    "STORAGE_TEMP_ENTITY": "input_number.heatai_desired_storage_temperature",
    "HEATING_DISABLE_ENTITY": "input_boolean.heatai_heating_disable",
    "WATERSTORAGE_DISABLE_ENTITY": "input_boolean.heatai_waterstorage_disable",
    "MODE_ENTITY": "input_select.heatai",  # off / room / manual
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
    # All fields numeric (12 fields). Order: 
    # hcmode;flow;hwctemp;hwcflow;setmode1;disablehc;disablehwctapping;disablehwcload;setmode2;remoteControlHcPump;releaseBackup;releaseCooling
    "FIXED_PARAMS": "{hcmode};{flow};{hwc};{hwcflow};{setmode1};{disablehc};{disablehwctapping};{disablehwcload};{setmode2};{remoteControlHcPump};{releaseBackup};{releaseCooling}",
    # defaults for extra fields:
    "DEFAULT_HWCFLOW": 0,
    "DEFAULT_SETMODE1": 0,
    "DEFAULT_SETMODE2": 0,
    "DEFAULT_REMOTE_CONTROL_HC_PUMP": 0,
    "DEFAULT_RELEASE_BACKUP": 0,
    "DEFAULT_RELEASE_COOLING": 0,
    "DEFAULT_DISABLE_HWCTAPPING": 0,
}

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("heatai")

# config loader
def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            user_conf = yaml.safe_load(f) or {}
        cfg = {**DEFAULT_CONFIG, **user_conf}
        logger.info("Loaded configuration from %s", CONFIG_FILE)
    except FileNotFoundError:
        cfg = DEFAULT_CONFIG
        logger.warning("%s not found - using defaults", CONFIG_FILE)
    except yaml.YAMLError as e:
        cfg = DEFAULT_CONFIG
        logger.error("YAML parse error in %s: %s - using defaults", CONFIG_FILE, e)
    return cfg

# HA helpers
def get_entity_data(entity_id, ha_url, headers):
    url = f"{ha_url}/api/states/{entity_id}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.debug("GET %s failed: %s", entity_id, e)
        return None

def call_service(domain, service, service_data, ha_url, headers):
    """
    Call HA service. Returns (ok:bool, response_text:str).
    Logs status and body for easier debugging.
    """
    url = f"{ha_url}/api/services/{domain}/{service}"
    try:
        r = requests.post(url, headers=headers, json=service_data, timeout=10)
        # We treat any 2xx as success
        ok = 200 <= r.status_code < 300
        body = r.text.strip() or "<no-body>"
        if ok:
            logger.debug("Service %s.%s OK %s", domain, service, r.status_code)
        else:
            logger.warning("Service %s.%s returned %s: %s", domain, service, r.status_code, body)
        return ok, body
    except requests.RequestException as e:
        logger.error("Service call %s.%s error: %s", domain, service, e)
        return False, str(e)

# helper to safely format float to single decimal (as ebusd logs show D1C, one decimal)
def fmt_temp(x):
    try:
        return f"{float(x):.1f}"
    except Exception:
        return f"{float(DEFAULT_CONFIG['DEFAULT_TI']):.1f}"

# global state to avoid repeating reset publishes
last_mode = None

def control_boiler(cfg, ha_url, headers):
    global last_mode
    try:
        # read mode
        mode_entity = cfg.get("MODE_ENTITY")
        mode_data = get_entity_data(mode_entity, ha_url, headers)
        mode = (mode_data.get("state") if mode_data else "off") or "off"
        mode = str(mode).lower()

        # shared flags and targets
        storage_data = get_entity_data(cfg["STORAGE_TEMP_ENTITY"], ha_url, headers)
        hwctempdesired = storage_data.get("state") if storage_data and "state" in storage_data else cfg["DEFAULT_HWCTEMP"]
        # ensure numeric string one-decimal
        try:
            hwc_str = f"{float(hwctempdesired):.1f}"
        except Exception:
            hwc_str = fmt_temp(cfg["DEFAULT_HWCTEMP"])

        heating_disable_data = get_entity_data(cfg["HEATING_DISABLE_ENTITY"], ha_url, headers)
        disablehc = "1" if heating_disable_data and heating_disable_data.get("state") == "on" else "0"

        waterstorage_disable_data = get_entity_data(cfg["WATERSTORAGE_DISABLE_ENTITY"], ha_url, headers)
        disablehwcload = "1" if waterstorage_disable_data and waterstorage_disable_data.get("state") == "on" else "0"

        # default extra fields (numeric)
        hwcflow = int(cfg.get("DEFAULT_HWCFLOW", 0))
        setmode1 = int(cfg.get("DEFAULT_SETMODE1", 0))
        setmode2 = int(cfg.get("DEFAULT_SETMODE2", 0))
        disablehwctapping = int(cfg.get("DEFAULT_DISABLE_HWCTAPPING", 0))
        remoteControlHcPump = int(cfg.get("DEFAULT_REMOTE_CONTROL_HC_PUMP", 0))
        releaseBackup = int(cfg.get("DEFAULT_RELEASE_BACKUP", 0))
        releaseCooling = int(cfg.get("DEFAULT_RELEASE_COOLING", 0))

        # prepare template fields
        publish = False
        hcmode = 0
        flow_val = 0.0

        if mode == "off":
            # send reset only once
            if last_mode != "off":
                hcmode = 0
                flow_val = 0.0
                params = cfg["FIXED_PARAMS"].format(
                    hcmode=int(hcmode), flow=f"{flow_val:.1f}", hwc=fmt_temp(hwc_str),
                    hwcflow=hwcflow, setmode1=setmode1, disablehc=int(disablehc),
                    disablehwctapping=disablehwctapping, disablehwcload=int(disablehwcload),
                    setmode2=setmode2, remoteControlHcPump=remoteControlHcPump,
                    releaseBackup=releaseBackup, releaseCooling=releaseCooling
                )
                logger.info("Mode=off -> sending reset once: topic=%s payload=%s", cfg["MQTT_TOPIC"], params)
                ok, body = call_service("mqtt", "publish", {"topic": cfg["MQTT_TOPIC"], "payload": params}, ha_url, headers)
                if not ok:
                    logger.warning("mqtt.publish returned non-ok: %s", body)
            else:
                logger.debug("Mode=off and reset already sent -> skipping publish")
        elif mode == "room":
            # compute flow based on ti + factor*(ti - ta)
            ti_data = get_entity_data(cfg["INSIDE_TEMP_ENTITY"], ha_url, headers)
            ti = float(ti_data.get("state")) if ti_data and "state" in ti_data else cfg["DEFAULT_TI"]

            factor_data = get_entity_data(cfg["CURVE_FACTOR_ENTITY"], ha_url, headers)
            factor = float(factor_data.get("state")) if factor_data and "state" in factor_data else cfg["DEFAULT_FACTOR"]

            weather_data = get_entity_data(cfg["WEATHER_ENTITY"], ha_url, headers)
            ta = float(weather_data.get("attributes", {}).get("temperature")) if weather_data and "attributes" in weather_data else cfg["DEFAULT_TA"]

            flow = round(ti + factor * (ti - ta), 1)
            flow = max(cfg["MIN_FLOW_TEMP"], min(cfg["MAX_FLOW_TEMP"], flow))
            flow_val = float(flow)
            hcmode = 1
            publish = True

            # update HA visible input_number (monitoring)
            call_service("input_number", "set_value", {"entity_id": cfg["CALCULATED_FLOW_ENTITY"], "value": flow_val}, ha_url, headers)

            params = cfg["FIXED_PARAMS"].format(
                hcmode=int(hcmode), flow=f"{flow_val:.1f}", hwc=fmt_temp(hwc_str),
                hwcflow=hwcflow, setmode1=setmode1, disablehc=int(disablehc),
                disablehwctapping=disablehwctapping, disablehwcload=int(disablehwcload),
                setmode2=setmode2, remoteControlHcPump=remoteControlHcPump,
                releaseBackup=releaseBackup, releaseCooling=releaseCooling
            )
            logger.info("Mode=room -> publishing: topic=%s payload=%s", cfg["MQTT_TOPIC"], params)
            ok, body = call_service("mqtt", "publish", {"topic": cfg["MQTT_TOPIC"], "payload": params}, ha_url, headers)
            if not ok:
                logger.warning("mqtt.publish failed: %s", body)

        elif mode == "manual":
            # read manual value from HA input_number
            manual_data = get_entity_data(cfg["CALCULATED_FLOW_ENTITY"], ha_url, headers)
            if manual_data and "state" in manual_data:
                try:
                    flow_val = float(manual_data["state"])
                except Exception:
                    flow_val = float(cfg["DEFAULT_TI"])
            else:
                flow_val = float(cfg["DEFAULT_TI"])
            flow_val = max(cfg["MIN_FLOW_TEMP"], min(cfg["MAX_FLOW_TEMP"], flow_val))
            hcmode = 1
            params = cfg["FIXED_PARAMS"].format(
                hcmode=int(hcmode), flow=f"{flow_val:.1f}", hwc=fmt_temp(hwc_str),
                hwcflow=hwcflow, setmode1=setmode1, disablehc=int(disablehc),
                disablehwctapping=disablehwctapping, disablehwcload=int(disablehwcload),
                setmode2=setmode2, remoteControlHcPump=remoteControlHcPump,
                releaseBackup=releaseBackup, releaseCooling=releaseCooling
            )
            logger.info("Mode=manual -> publishing: topic=%s payload=%s", cfg["MQTT_TOPIC"], params)
            ok, body = call_service("mqtt", "publish", {"topic": cfg["MQTT_TOPIC"], "payload": params}, ha_url, headers)
            if not ok:
                logger.warning("mqtt.publish failed: %s", body)
        else:
            logger.warning("Unknown mode '%s' from %s - treating as off", mode, cfg.get("MODE_ENTITY"))

        last_mode = mode

    except Exception as e:
        logger.exception("Unexpected error in control_boiler: %s", e)


# graceful shutdown
stop_requested = False
def handle_signal(sig, frame):
    global stop_requested
    stop_requested = True
    logger.info("Shutdown signal received")

def main():
    cfg = load_config()
    # normalize types
    try:
        cfg["LOOP_INTERVAL_SECONDS"] = int(cfg.get("LOOP_INTERVAL_SECONDS", 300))
    except Exception:
        cfg["LOOP_INTERVAL_SECONDS"] = 300

    # set log level
    logger.setLevel(getattr(logging, cfg.get("LOG_LEVEL", "INFO"), logging.INFO))

    # print effective config subset
    logger.info("Effective config: HA_URL=%s, MQTT_TOPIC=%s, MODE_ENTITY=%s",
                os.getenv("HA_URL", cfg["DEFAULT_HA_URL"]), cfg.get("MQTT_TOPIC"), cfg.get("MODE_ENTITY"))

    # HA connection
    ha_url = os.getenv("HA_URL", cfg["DEFAULT_HA_URL"])
    ha_token = os.getenv("HA_TOKEN")
    if not ha_token:
        logger.error("HA_TOKEN missing. Exiting.")
        sys.exit(1)
    headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}

    # signals
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    interval = cfg["LOOP_INTERVAL_SECONDS"]
    logger.info("Starting control loop (interval=%ss)...", interval)
    while not stop_requested:
        control_boiler(cfg, ha_url, headers)
        # sleep with 1s granularity for responsive shutdown
        for _ in range(interval):
            if stop_requested:
                break
            time.sleep(1)

    logger.info("Exited cleanly")

if __name__ == "__main__":
    main()
