# TrueNAS deployment

The release workflow targets `ghcr.io/javadevjt/discord-option-tailer` and publishes a Linux `amd64` image from `main` and semantic-version tags. A `main` build gets `main`, `latest`, and a long `sha-...` tag; a `v1.2.3` release gets `1.2.3`, `1.2`, `1`, and its `sha-...` tag. ARM64 is omitted until the Codex CLI and Playwright image have been verified on that architecture.

For unauthenticated NAS pulls, set the GHCR package visibility to **Public** in the repository's **Packages** settings after the first publish. This local release work does not change GitHub settings. If the package remains private, authenticate the NAS Docker client with a read-only GitHub token that has `read:packages`, and keep that credential in the NAS Docker credential store rather than in this compose file.

This deployment uses [compose.truenas.yaml](../compose.truenas.yaml). It pulls the image and never builds source on the NAS. Create a private TrueNAS dataset for `/data`, then create an `.env` beside the compose file. Use the NAS address assigned to its trusted LAN interface:

```dotenv
RELAY_IMAGE=ghcr.io/javadevjt/discord-option-tailer:latest
RELAY_DATA_DIR=/mnt/tank/apps/discord-option-tailer
HTTP_BIND=192.168.1.20
HTTP_PORT=8787
DASHBOARD_USER=relay
DASHBOARD_PASSWORD=
```

Replace the dataset path and LAN address. Keep this file private (`chmod 600`) and set `DASHBOARD_PASSWORD` before startup when an operator-managed password is preferred. When it is empty, the image generates a random password on first start and stores it at `${RELAY_DATA_DIR}/dashboard.password`; read that owner-only file through the NAS administrative shell and protect it like a credential. The data dataset also retains the relay configuration, browser profile, Codex login, Robinhood OAuth state, ledger, and kill switch.

With the TrueNAS Apps UI, choose **Apps → Discover Apps → Install via YAML** (called **Custom App** on some SCALE releases), paste or upload `compose.truenas.yaml`, and set the listed environment values and host path in the form. Apps installations do not necessarily read a neighboring `.env`; enter the values in the form or substitute them in a private YAML copy. Preserve the `127.0.0.1:8766:8766` callback mapping and the `/data` dataset bind.

Validate and start from the directory containing the files:

```sh
docker compose --env-file .env -f compose.truenas.yaml config
docker compose --env-file .env -f compose.truenas.yaml pull
docker compose --env-file .env -f compose.truenas.yaml up -d
docker compose --env-file .env -f compose.truenas.yaml ps
```

Open `http://192.168.1.20:8787` from a trusted LAN client. Set `HTTP_BIND` to the NAS's private LAN IP, keep the NAS firewall restricted to the intended subnet, and do not use `0.0.0.0`, a WAN address, or port forwarding to the public Internet. The dashboard and noVNC desktop require Basic Auth; the Robinhood callback is a separate loopback-only listener with OAuth state validation, and port 8766 is never a LAN service.

## Robinhood authorization from a remote browser

Robinhood has a fixed redirect URI, `http://127.0.0.1:8766/callback`. The compose file binds that port to NAS host loopback. Docker forwards the host-loopback port into the container interface, so the internal callback listener uses `0.0.0.0` while the NAS itself keeps port 8766 unreachable from the LAN. If the normal browser is on your workstation while the relay runs on the NAS, create a local SSH forward before starting Robinhood sign-in and leave it running:

```sh
ssh -N -T -o ExitOnForwardFailure=yes -L 8766:127.0.0.1:8766 <nas-user>@<nas-host>
```

Then open the LAN dashboard in that same workstation browser, choose **Setup → Start Robinhood sign-in**, and complete the authorization. The browser's request to its own `127.0.0.1:8766` travels through the tunnel to the NAS callback. Close the tunnel after Setup reports completion or failure. A browser running directly on the NAS does not need the tunnel. Never change the callback mapping to `0.0.0.0` or publish 8766 on the LAN.

## First run and maintenance

Fresh persistent data starts in Shadow mode with live submissions disabled. Complete Discord, Codex, and Robinhood setup in the authenticated dashboard; deployment does not import host credentials. Use an immutable GHCR digest in `RELAY_IMAGE` when repeatable rollbacks matter, for example `ghcr.io/javadevjt/discord-option-tailer@sha256:<digest>`.

Check the unauthenticated health endpoint and recent logs when diagnosing startup:

```sh
curl --fail http://192.168.1.20:8787/healthz
docker compose --env-file .env -f compose.truenas.yaml logs --tail=100 relay
```

Stop the service before backing up the entire data dataset, including authentication state. Keep backups encrypted and do not remove the dataset or run a volume cleanup command during upgrades.
