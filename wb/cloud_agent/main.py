#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass
from json import JSONDecodeError

from wb_common.mqtt_client import MQTTClient

from wb.cloud_agent.version import package_version

HTTP_200_OK = 200
HTTP_204_NO_CONTENT = 204
DEFAULT_CONF_FILE = "/mnt/data/etc/wb-cloud-agent.conf"


@dataclass
class AppSettings:
    """
    Simple settings configurator.

    To rewrite parameters just add them to wb-cloud-agent config.

    An example of config at /mnt/data/etc/wb-cloud-agent.conf:

    {
        "CLIENT_CERT_ENGINE_KEY": "ATECCx08:00:04:C0:00",
    }

    """

    LOG_LEVEL: str = "INFO"

    CLIENT_CERT_ENGINE_KEY: str = "ATECCx08:00:02:C0:00"
    CLIENT_CERT_FILE: str = "/var/lib/wb-cloud-agent/device_bundle.crt.pem"
    CLOUD_URL: str = "https://agent.wirenboard.cloud/api-agent/v1/"
    REQUEST_PERIOD_SECONDS: int = 3

    FRP_SERVICE: str = "wb-cloud-agent-frpc.service"
    FRP_CONFIG: str = "/var/lib/wb-cloud-agent/frpc.conf"

    TELEGRAF_SERVICE: str = "wb-cloud-agent-telegraf.service"
    TELEGRAF_CONFIG: str = "/var/lib/wb-cloud-agent/telegraf.conf"

    ACTIVATION_LINK_CONFIG: str = "/var/lib/wb-cloud-agent/activation_link.conf"

    MQTT_PREFIX: str = "/devices/system__wb-cloud-agent"

    @classmethod
    def update_from_json_file(cls, conf_file=None):
        if not conf_file:
            return cls()

        try:
            with open(conf_file, "r") as file:
                conf = file.read()
        except (FileNotFoundError, OSError):
            raise ValueError("Cannot read config file at: " + conf_file)

        try:
            conf = json.loads(conf)
        except JSONDecodeError:
            raise ValueError("Invalid config file format (must be valid json) at: " + conf_file)

        return cls(**conf)


settings = AppSettings.update_from_json_file(DEFAULT_CONF_FILE)


def setup_log():
    numeric_level = getattr(logging, settings.LOG_LEVEL.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid log level: %s" % settings.LOG_LEVEL)

    logging.basicConfig(level=numeric_level, encoding="utf-8", format="%(message)s")


def do_curl(method="get", endpoint="", body=None):
    data_delimiter = "|||"
    output_format = data_delimiter + '{"code":"%{response_code}"}'

    if method == "get":
        command = ["curl"]
    elif method in ("post", "put"):
        command = ["curl", "-X", method.upper()]
        if body:
            command += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    else:
        raise ValueError("Invalid method: " + method)

    url = settings.CLOUD_URL + endpoint

    command += [
        "--connect-timeout",
        "45",
        "--retry",
        "8",
        "--retry-max-time",
        "300",
        "--retry-all-errors",
        "--cert",
        settings.CLIENT_CERT_FILE,
        "--key",
        settings.CLIENT_CERT_ENGINE_KEY,
        "--engine",
        "ateccx08",
        "--key-type",
        "ENG",
        "-w",
        output_format,
        url,
    ]

    result = subprocess.run(command, timeout=360, check=True, capture_output=True)

    decoded_result = result.stdout.decode("utf-8")
    split_result = decoded_result.split(data_delimiter)
    if len(split_result) != 2:
        raise ValueError("Invalid data in response: " + str(split_result))

    try:
        data = json.loads(split_result[0])
    except JSONDecodeError:
        data = {}

    try:
        status = int(json.loads(split_result[1])["code"])
    except (KeyError, TypeError, ValueError, JSONDecodeError):
        raise ValueError("Invalid data in response: " + str(split_result))

    return data, status


def write_activation_link(link, mqtt):
    with open(settings.ACTIVATION_LINK_CONFIG, "w") as file:
        file.write(link)

    publish_ctrl(mqtt, "activation_link", link)


def read_activation_link():
    if not os.path.exists(settings.ACTIVATION_LINK_CONFIG):
        return "unknown"

    with open(settings.ACTIVATION_LINK_CONFIG, "r") as file:
        return file.readline()


def update_activation_link(payload, mqtt):
    write_activation_link(payload["activationLink"], mqtt)


def update_tunnel_config(payload, mqtt):
    with open(settings.FRP_CONFIG, "w") as file:
        file.write(payload["config"])

    subprocess.run(["systemctl", "enable", settings.FRP_SERVICE], check=True)
    subprocess.run(["systemctl", "restart", settings.FRP_SERVICE], check=True)
    write_activation_link("unknown", mqtt)


def update_metrics_config(payload, mqtt):
    with open(settings.TELEGRAF_CONFIG, "w") as file:
        file.write(payload["config"])

    subprocess.run(["systemctl", "enable", settings.TELEGRAF_SERVICE], check=True)
    subprocess.run(["systemctl", "restart", settings.TELEGRAF_SERVICE], check=True)
    write_activation_link("unknown", mqtt)


HANDLERS = {
    "update_activation_link": update_activation_link,
    "update_tunnel_config": update_tunnel_config,
    "update_metrics_config": update_metrics_config,
}


def publish_vdev(mqtt):
    mqtt.publish(settings.MQTT_PREFIX + "/meta/name", "cloud status", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/meta/driver", "wb-cloud-agent", retain=True, qos=2)
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/status/meta",
        '{"type": "text", "readonly": true, "order": 1, "title": {"en": "Status"}}',
        retain=True,
        qos=2,
    )
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/activation_link/meta",
        '{"type": "text", "readonly": true, "order": 2, "title": {"en": "Link"}}',
        retain=True,
        qos=2,
    )
    mqtt.publish(settings.MQTT_PREFIX + "/controls/status", "disconnected", retain=True, qos=2)
    mqtt.publish(
        settings.MQTT_PREFIX + "/controls/activation_link", read_activation_link(), retain=True, qos=2
    )


def remove_vdev(mqtt):
    mqtt.publish(settings.MQTT_PREFIX + "/meta/name", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/meta/driver", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/status/meta", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/activation_link/meta", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/status", "", retain=True, qos=2)
    mqtt.publish(settings.MQTT_PREFIX + "/controls/activation_link", "", retain=True, qos=2)


def publish_ctrl(mqtt, ctrl, value):
    mqtt.publish(settings.MQTT_PREFIX + f"/controls/{ctrl}", value, retain=True, qos=2)


def make_event_request(mqtt):
    event_data, http_status = do_curl(method="get", endpoint="events/")
    logging.debug("Checked for new events. Status " + str(http_status) + ". Data: " + str(event_data))

    if http_status == HTTP_204_NO_CONTENT:
        return

    if http_status != HTTP_200_OK:
        raise ValueError("Not a 200 status while retrieving event: " + str(http_status))

    code = event_data.get("code", "")
    handler = HANDLERS.get(code)

    event_id = event_data.get("id")
    if not event_id:
        raise ValueError("Unknown event id: " + str(event_id))

    payload = event_data.get("payload")
    if not payload:
        raise ValueError("Empty payload")

    if handler:
        handler(payload, mqtt)
    else:
        logging.warning("Got an unknown event '" + code + "'. Try to update wb-cloud-agent package.")

    logging.info("Event '" + code + "' handled successfully, event id " + str(event_id))

    _, http_status = do_curl(method="post", endpoint="events/" + event_id + "/confirm/")

    if http_status != HTTP_204_NO_CONTENT:
        raise ValueError("Not a 204 status on event confirmation: " + str(http_status))


def make_start_up_request(mqtt):
    status_data, http_status = do_curl(method="get", endpoint="agent-start-up/")
    if http_status != HTTP_200_OK:
        raise ValueError("Not a 200 status while making start up request: " + str(http_status))

    if "activated" not in status_data or "activationLink" not in status_data:
        raise ValueError("Invalid response data while making start up request: " + str(status_data))

    activated = status_data["activated"]
    activation_link = status_data["activationLink"]

    if activated or not activation_link:
        write_activation_link("unknown", mqtt)
    else:
        write_activation_link(activation_link, mqtt)

    return status_data


def send_agent_version():
    status_data, http_status = do_curl(
        method="put",
        endpoint="update_device_data/",
        body={"agent_version": package_version},
    )
    if http_status != HTTP_200_OK:
        logging.error("Not a 200 status while making send_agent_version request: " + str(http_status))


def on_connect(client, _, flags, reason_code, properties=None):
    # 0: Connection successful
    if reason_code != 0:
        logging.error(f"Failed to connect: {reason_code}. loop_forever() will retry connection")
    else:
        client.subscribe("/devices/system/controls/HW Revision", qos=2)


def on_message(client, userdata, message):
    client.unsubscribe("/devices/system/controls/HW Revision")
    status_data, http_status = do_curl(
        method="put",
        endpoint="update_device_data/",
        body={"hardware_revision": str(message.payload, "utf-8")},
    )
    if http_status != HTTP_200_OK:
        raise ValueError("Not a 200 status while making update HW revision request: " + str(http_status))


def main():
    setup_log()

    mqtt = MQTTClient("wb-cloud-agent")
    mqtt.on_connect = on_connect
    mqtt.on_message = on_message
    mqtt.start()

    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true", help="Run cloud agent in daemon mode")
    options = parser.parse_args()

    make_start_up_request(mqtt)
    send_agent_version()

    if not options.daemon:
        link = read_activation_link()
        if link != "unknown":
            print(f">> {link}")
        else:
            print("No active link. Controller may be already connected")
        return

    publish_vdev(mqtt)

    with ExitStack() as stack:
        stack.callback(remove_vdev, mqtt)

        while True:
            start = time.perf_counter()

            try:
                make_event_request(mqtt)
            except Exception as ex:
                logging.exception("Error making request to cloud!")
                publish_ctrl(mqtt, "status", "error:" + str(ex))
            else:
                publish_ctrl(mqtt, "status", "ok")

            request_time = time.perf_counter() - start

            logging.debug("Done in: " + str(int(request_time * 1000)) + " ms.")

            time.sleep(settings.REQUEST_PERIOD_SECONDS)
