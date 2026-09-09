"""Connection telemetry must not change provider results or order outcomes."""
import logging


def publish_status(callback, component, state):
    if callback is not None:
        try:
            callback({"component": component, "state": state})
        except Exception as exc:
            logging.getLogger(__name__).warning("Status publication failed (%s)", type(exc).__name__)
