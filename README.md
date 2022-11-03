# B route

## Requirements

- poetry
- yarn

## Install

```sh
yarn install
cp config.ini{.sample,}
$EDITOR config.ini
```

## Run

```sh
poetry run sensor
```

## Munin

Copy `munin/*` to `/etc/munin/plugins` and `alias b-route-wattage='cd PATH_TO_ROUTE_B && poetry run sensor'`
