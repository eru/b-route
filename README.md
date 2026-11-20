# B route

Bルートサービスを利用して、瞬間消費電力を取得する常駐デーモン。

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

デフォルトでは60秒間隔で電力を取得し、`/run/b-route/wattage.json` に最新値とタイムスタンプを出力します（`config.ini` の `[daemon]` セクションまたは `--interval` / `--output` で変更可能）。

## systemd

`systemd/b-route.service` を参考にパス等を環境に合わせて編集し、ユニットを配置して起動します。

```sh
sudo cp systemd/b-route.service /etc/systemd/system/b-route.service
sudo $EDITOR /etc/systemd/system/b-route.service
sudo systemctl daemon-reload
sudo systemctl enable --now b-route.service
```

ログは `journalctl -u b-route.service -f` で確認できます。

## Munin

`munin/b_route_wattage` を `/etc/munin/plugins/` に配置します。プラグインは `/run/b-route/wattage.json` を読み取り、300秒以上更新がない場合は `U`（Unknown）を返します。

パスや鮮度閾値を変更する場合は `/etc/munin/plugin-conf.d/b_route` に以下を設定してください。

```ini
[b_route_wattage]
env.status_file /run/b-route/wattage.json
env.max_age 300
```
