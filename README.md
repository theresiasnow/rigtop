# nmead

Read GPS/NMEA data from an Icom IC-705 via **rigctld** (Hamlib).

## Prerequisites

- **Hamlib** installed with `rigctld`
- IC-705 connected via USB (or network)

## Setup

```bash
uv sync
```

## Start rigctld

Linux:
```bash
rigctld -m 3085 -r /dev/ttyUSB0 -s 115200
```

Windows:
```bash
rigctld -m 3085 -r COM3 -s 115200
```

Model 3085 = IC-705. Adjust serial port and baud rate for your setup.

## Usage

```bash
# Poll GPS position every 2 seconds
uv run python main.py

# Custom host/port and interval
uv run python main.py --host 192.168.1.100 --port 4532 --interval 5

# Include NMEA GGA/RMC sentences in output
uv run python main.py --nmea

# Read position once and exit
uv run python main.py --once
```

## Options

| Flag         | Default       | Description                        |
|--------------|---------------|------------------------------------|
| `--host`     | `127.0.0.1`   | rigctld host address               |
| `--port`     | `4532`        | rigctld TCP port                   |
| `--interval` | `2.0`         | Poll interval in seconds           |
| `--nmea`     | off           | Output NMEA GGA/RMC sentences      |
| `--once`     | off           | Read position once and exit        |

## Output example

```
[14:32:01] Position: 59°21.456'N  18°04.321'E  Grid: JO89fi
           Decimal:  59.357600, 18.072017
           Rig:      145.500000 MHz  FM
           GGA: $GPGGA,123201.00,5921.4560,N,01804.3210,E,1,00,1.0,0.0,M,0.0,M,,*5A
           RMC: $GPRMC,123201.00,A,5921.4560,N,01804.3210,E,0.0,0.0,150326,,,*6B
```