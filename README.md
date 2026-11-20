# B route

Bルートサービスを利用して、瞬間消費電力を取得するスクリプト。

muninと組み合わせることで、画像のような監視を行うことができる。

![b_route](https://user-images.githubusercontent.com/909444/230387166-1d03145c-9702-422f-b525-e21b23003020.png)

## Requirements

- uv
- pnpm

## Install

```sh
pnpm install
cp config.ini{.sample,}
$EDITOR config.ini
```

## Run

```sh
uv run sensor
```

## Munin

Copy `munin/*` to `/etc/munin/plugins` and `alias b-route-wattage='cd PATH_TO_B_ROUTE && uv run sensor'`
