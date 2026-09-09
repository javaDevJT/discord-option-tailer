# TrueNAS deployment

The release workflow targets `ghcr.io/javadevjt/discord-option-tailer` and publishes a Linux `amd64` image from `main` and semantic-version tags. A `main` build gets `main`, `latest`, and a long `sha-...` tag; a `v1.2.3` release gets `1.2.3`, `1.2`, `1`, and its `sha-...` tag. ARM64 is omitted until the Codex CLI and Playwright image have been verified on that architecture.

For unauthenticated NAS pulls, set the GHCR package visibility to **Public** in the repository's **Packages** settings after the first publish. This local release work does not change GitHub settings. If the package remains private, authenticate the NAS Docker client with a read-only GitHub token that has `read:packages`, and keep that credential in the NAS Docker credential store rather than in this compose file.

This deployment uses [compose.truenas.yaml](../compose.truenas.yaml). It pulls the image and never builds source on the NAS. Create a private TrueNAS dataset for `/data`, then create an `.env` beside the compose file. Use the NAS address assigned to its trusted LAN interface:

```dotenv
RELAY_IMAGE=ghcr.io/javadevjt/discord-option-tailer:latest
RELAY_DATA_DIR=/mnt/tank/apps/discord-option-tailer
HTTP_BIND=192.168.1.20
HTTP_PORT=8787
RELAY_ROBINHOOD_REDIRECT_URI=http://192.168.1.20:8787/callback
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

Open `http://192.168.1.20:8787` from a trusted LAN client. Set `HTTP_BIND` to the NAS's private LAN IP, keep the NAS firewall restricted to the intended subnet, and do not use `0.0.0.0`, a WAN address, or port forwarding to the public Internet. The dashboard and noVNC desktop require Basic Auth. The exact `/callback` route forwards to the internal OAuth listener without Basic Auth; OAuth state and PKCE protect the authorization exchange. Callback query strings are excluded from proxy logs. Port 8766 stays host-loopback-only.

## Robinhood authorization from a remote browser

Set the app environment variable `RELAY_ROBINHOOD_REDIRECT_URI` to the address your browser uses for the dashboard, with the exact path `/callback`. For the example above, use `http://192.168.1.20:8787/callback`; an HTTPS reverse proxy can use `https://relay.example.com/callback`. Queries, fragments and embedded credentials are rejected. Use HTTP only on a trusted LAN; use HTTPS when available. If using another reverse proxy, disable access and error request logging for `/callback` there too so OAuth codes do not enter its logs.

Redeploy after changing the environment, open **Setup → Start Robinhood sign-in**, and complete authorization in your normal browser. No SSH tunnel is needed. The dashboard proxy forwards the callback to port 8766 inside the container. A link created before redeployment must be replaced by starting sign-in again. Changing this setting does not clear saved tokens, client registration, dashboard credentials or account bindings. Keep the existing `/data` storage and environment when upgrading.

## First run and maintenance

Fresh persistent data starts in Shadow mode with live submissions disabled. Complete Discord, Codex, and Robinhood setup in the authenticated dashboard; deployment does not import host credentials. Use an immutable GHCR digest in `RELAY_IMAGE` when repeatable rollbacks matter, for example `ghcr.io/javadevjt/discord-option-tailer@sha256:<digest>`.

Check the unauthenticated health endpoint and recent logs when diagnosing startup:

```sh
curl --fail http://192.168.1.20:8787/healthz
docker compose --env-file .env -f compose.truenas.yaml logs --tail=100 relay
```

Stop the service before backing up the entire data dataset, including authentication state. Keep backups encrypted and do not remove the dataset or run a volume cleanup command during upgrades.
