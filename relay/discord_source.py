"""Select the configured personal-account message transport."""
from .core import Hold


def transport(config):
    value = config.get("discord", {}).get("transport", "browser")
    if value not in {"browser", "gateway"}:
        raise Hold("Discord transport must be browser or gateway")
    return value


async def monitor(config, on_message, *, register_verifier=None, on_status=None):
    if transport(config) == "gateway":
        from .gateway import monitor as reader
    else:
        from .browser import monitor as reader
    await reader(config, on_message, register_verifier=register_verifier, on_status=on_status)


async def setup(config, *, on_status=None):
    if transport(config) == "gateway":
        from .gateway import setup as connect
        await connect(config, on_status=on_status)
    else:
        from .browser import login
        await login(config["browser"]["profile_dir"], keep_open=True, on_status=on_status,
                    discovery_runtime_path=config.get("runtime_status_file"))
