from configparser import ConfigParser, NoOptionError

from serial import Serial


def scan(ser: Serial) -> tuple[str, str, str]:
    # cannel scan
    ducation = 4
    res = {}
    require_keys = ("Channel", "Pan ID")
    command = str.encode(f"SKSCAN 2 FFFFFFFF {ducation}\r\n")
    while True:
        ser.write(command)

        while True:
            line = ser.readline().decode(encoding="utf-8")
            if line.startswith("EVENT 22"):
                break

            if line.startswith("  "):
                cols = line.strip().split(":")
                res[cols[0]] = cols[1]

        if all([k in res for k in require_keys]):
            break

        ducation += 1
        if 7 < ducation:
            exit()

    # convert mac address to ipv6 local address
    ser.write(str.encode(f"SKLL64 {res['Addr']}\r\n"))
    ser.readline().decode(encoding="utf-8")
    address = ser.readline().decode(encoding="utf-8").strip()

    return (res["Channel"], res["Pan ID"], address)


def save_config(inifile: ConfigParser) -> None:
    with open("./config.ini", "w") as configfile:
        inifile.write(configfile)


def save_connection(inifile: ConfigParser, channnel: str, pan_id: str, address: str) -> None:
    inifile.set("connection", "channel", channnel)
    inifile.set("connection", "pan_id", pan_id)
    inifile.set("connection", "address", address)

    save_config(inifile)


def reset_connection(inifile: ConfigParser) -> None:
    inifile.remove_option("connection", "channel")
    inifile.remove_option("connection", "pan_id")
    inifile.remove_option("connection", "address")

    save_config(inifile)


def main(is_triable: bool = True) -> None:
    # initlize
    inifile = ConfigParser()
    inifile.read("./config.ini", "utf-8")

    port = inifile.get("device", "port")
    broute_id = inifile.get("auth", "broute_id")
    broute_pw = inifile.get("auth", "broute_pw")

    ser = Serial(port, 115200)

    # set password
    ser.write(str.encode(f"SKSETPWD C {broute_pw}\r\n"))
    ser.readline()
    ser.readline()

    # set id
    ser.write(str.encode(f"SKSETRBID {broute_id}\r\n"))
    ser.readline()
    ser.readline()

    try:
        channel = inifile.get("connection", "channel")
        pan_id = inifile.get("connection", "pan_id")
        address = inifile.get("connection", "address")
    except NoOptionError:
        (channel, pan_id, address) = scan(ser)
        save_connection(inifile, channel, pan_id, address)

    try:
        # set channel
        ser.write(str.encode(f"SKSREG S2 {channel}\r\n"))
        ser.readline()
        ser.readline()

        # set pan id
        ser.write(str.encode(f"SKSREG S3 {pan_id}\r\n"))
        ser.readline()
        ser.readline()

        # pana connection
        ser.write(str.encode(f"SKJOIN {address}\r\n"))
        ser.readline()
        ser.readline()
        while True:
            line = ser.readline().decode(encoding="utf-8")
            if line.startswith("EVENT 24"):
                raise IOError("EVENT 24")
            if line.startswith("EVENT 25"):
                break
        ser.readline()
        ser.timeout = 2

        # get data
        frame = b"\x10\x81\x00\x01\x05\xFF\x01\x02\x88\x01\x62\x01\xE7\x00"
        command = str.encode(f"SKSENDTO 1 {address} 0E1A 1 {len(frame):04X} ") + frame
        while True:
            ser.write(command)
            ser.readline()
            ser.readline()
            ser.readline()
            line = ser.readline().decode(encoding="utf-8")
            if not line.startswith("ERXUDP"):
                continue
            data = line.split(" ")
            res = data[8]
            seoj = res[8:14]
            esv = res[20:22]
            if seoj != "028801" or esv != "72":
                continue
            epc = res[24:26]
            if epc != "E7":
                continue
            wattage_hex = res[-8:]
            wattage = int(wattage_hex, 16)
            print(f"{wattage} [W]")

            break

        ser.close()
    except Exception as e:
        if not is_triable:
            raise e
        reset_connection(inifile)
        main(is_triable=False)


if __name__ == "__main__":
    main()
