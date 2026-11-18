"""Run a local network check and generate a shareable receipt."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ENDPOINT = "one.one.one.one"
REFERENCE_IP = "1.1.1.1"
NA = "Unavailable"


@dataclass(frozen=True)
class Checks:
    signal: str
    router: str
    dns: str
    https: str
    direct: str


DEMOS = {
    "healthy": Checks("Strong", "Responded", "Passed", "Passed", "Passed"),
    "dns": Checks("Strong", "Responded", "Failed", "Skipped", "Passed"),
    "router": Checks("Strong", "No reply", "Failed", "Skipped", "Failed"),
    "weak-signal": Checks("Weak", "Responded", "Passed", "Passed", "Passed"),
}


def capture(command, timeout=8):
    try:
        return subprocess.run(
            command, capture_output=True, text=True, errors="replace",
            timeout=timeout, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def wifi_signal():
    output = capture(["netsh", "wlan", "show", "interfaces"])
    signals = re.findall(r"^\s*Signal\s*:\s*(\d+)%", output, re.MULTILINE)
    if len(signals) != 1:
        return NA
    percent = int(signals[0])
    return "Strong" if percent >= 70 else "Fair" if percent >= 45 else "Weak"


def router_check():
    command = (
        "$r = Get-NetIPConfiguration | Where-Object "
        "{ $_.IPv4DefaultGateway -and $_.InterfaceAlias -like 'Wi-Fi*' }; "
        "$g = @($r | ForEach-Object { $_.IPv4DefaultGateway.NextHop }); "
        "if ($g.Count -eq 1) { $g[0] }"
    )
    gateway = capture(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]).strip()
    if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", gateway):
        return NA
    reply = capture(["ping.exe", "-4", "-n", "1", "-w", "1200", gateway], 4)
    return "Responded" if "TTL=" in reply.upper() else "No reply"


def dns_check():
    script = (
        "import json, socket, sys; "
        "print(json.dumps(list(dict.fromkeys(row[4][0] for row in "
        "socket.getaddrinfo(sys.argv[1], 443, type=socket.SOCK_STREAM)))))"
    )
    output = capture([sys.executable, "-c", script, ENDPOINT], 5)
    return json.loads(output) if output else []


def https_check(address):
    # Connect to the selected IP while validating the certificate for ENDPOINT.
    try:
        with socket.create_connection((address, 443), timeout=3) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=ENDPOINT) as tls:
                tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {ENDPOINT}\r\nConnection: close\r\n\r\n".encode())
                response = http.client.HTTPResponse(tls)
                with response:
                    response.begin()
                    return response.status < 500 and response.status != 407
    except (OSError, http.client.HTTPException):
        return False


def live_checks():
    with ThreadPoolExecutor(max_workers=4) as pool:
        signal = pool.submit(wifi_signal)
        router = pool.submit(router_check)
        direct = pool.submit(https_check, REFERENCE_IP)
        addresses = dns_check()
        # Try at most one address per IP family to bound the check time.
        candidates = list({":" in address: address for address in reversed(addresses)}.values())
        named = any(pool.map(https_check, candidates))
        return Checks(signal.result(), router.result(), "Passed" if addresses else "Failed",
                      ("Passed" if named else "Failed") if addresses else "Skipped",
                      "Passed" if direct.result() else "Failed")


def diagnosis(checks):
    if checks.dns == "Failed" and checks.direct == "Passed":
        return "Likely DNS resolution issue", "The reference IP answered over HTTPS."
    if checks.https == "Passed":
        if checks.signal == "Weak":
            return "Weak Wi-Fi signal", "HTTPS worked during this check."
        return "HTTPS reachable", "The reference hostname worked during this check."
    if checks.router == "No reply":
        return "Inconclusive", "The router did not reply. It may block ping."
    if checks.direct == "Passed":
        return "Inconclusive", "Direct HTTPS worked. The resolved address failed."
    return "Inconclusive", "The reference endpoint was unreachable in this check."


def receipt_data(checks):
    return [
        ("Wi-Fi signal", checks.signal),
        ("Router ping", checks.router),
        ("DNS resolution", checks.dns),
        ("HTTPS via DNS", checks.https),
        ("HTTPS via reference IP", checks.direct),
    ]


def render(path, checks, demo=None):
    image = Image.new("RGB", (860, 640), "#10271f")
    draw = ImageDraw.Draw(image)
    ink, muted, green = "#133528", "#4b6258", "#bbfa8b"
    draw.rounded_rectangle((30, 30, 830, 610), radius=24, fill="#f6f9ef")
    draw.text((60, 53), "NETWORK DIAGNOSTICS", font=ImageFont.load_default(19), fill=muted)
    draw.text((60, 83), "WiFi Receipt", font=ImageFont.load_default(61), fill=ink)
    badge = f"DEMO / {demo.upper()}" if demo else "LIVE CHECK"
    draw.rounded_rectangle((560, 83, 800, 126), radius=16, fill=green)
    draw.text((680, 105), badge, anchor="mm", font=ImageFont.load_default(18), fill=ink)
    body = ImageFont.load_default(26)
    for y, (label, value) in enumerate(receipt_data(checks), start=0):
        top = 176 + 52 * y
        draw.line((60, top, 800, top), fill="#d4dfd3", width=2)
        draw.text((60, top + 12), label, font=body, fill=ink)
        draw.text((800, top + 12), value, anchor="ra", font=body, fill=ink)
    title, detail = diagnosis(checks)
    draw.rounded_rectangle((60, 456, 800, 560), radius=16, fill=ink)
    draw.text((82, 471), title, font=ImageFont.load_default(33), fill=green)
    draw.text((82, 519), detail, font=ImageFont.load_default(22), fill="white")
    draw.text((60, 577), "A snapshot of one endpoint  /  No device details on this receipt",
              font=ImageFont.load_default(17), fill=muted)
    image.save(path, format="PNG")
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Run a live check on Windows")
    demo = commands.add_parser("demo", help="Render a simulated check offline")
    demo.add_argument("scenario", choices=DEMOS)
    for command in (check, demo):
        command.add_argument("--output", type=Path, default=Path("wifi-receipt.png"))
    args = parser.parse_args()
    scenario = getattr(args, "scenario", None)
    if not scenario and sys.platform != "win32":
        parser.error("Live checks require Windows. Demo mode works on any platform.")
    checks = DEMOS[scenario] if scenario else live_checks()
    try:
        render(args.output, checks, scenario)
    except OSError as error:
        parser.exit(1, f"Could not save PNG: {error}\n")
    print(f"WiFi Receipt / {'DEMO: ' + scenario if scenario else 'Live check'}")
    for label, value in receipt_data(checks):
        print(f"{label:25} {value}")
    print(*diagnosis(checks), sep="\n")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
