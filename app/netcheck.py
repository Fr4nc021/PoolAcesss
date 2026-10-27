"""Confere se o SERVER_LAN_IP do env.txt é mesmo um IP deste PC.

O facial baixa a foto em http://SERVER_LAN_IP:PORT/media/... Se o IP for de
outro computador (env.txt copiado), o cadastro fica sem foto. Este aviso
aparece antes disso.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time

from app.config import configured_device_urls, on_chromeos, reload_env

_CACHE_SECONDS = 30
_lock = threading.Lock()
_cached: tuple[float, frozenset[str]] = (0.0, frozenset())


def local_ipv4_addresses() -> frozenset[str]:
    """IPv4 das placas de rede deste PC (Wi-Fi, cabo). Vazio se não deu para ler."""
    global _cached
    with _lock:
        stamp, value = _cached
        if value and time.monotonic() - stamp < _CACHE_SECONDS:
            return value
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except OSError:
        pass
    # A placa usada para sair para a rede, mesmo que o nome do PC não resolva.
    for target in ("192.168.0.1", "10.0.0.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 9))
                found.add(probe.getsockname()[0])
        except OSError:
            continue
    # Só endereços de rede local: VPN e IP público não servem para o facial.
    usable = frozenset(
        ip
        for ip in found
        if ipaddress.ip_address(ip).is_private
        and not ipaddress.ip_address(ip).is_loopback
        and not ip.startswith("169.254.")
    )
    with _lock:
        _cached = (time.monotonic(), usable)
    return usable


def suggested_ip(ips: frozenset[str]) -> str:
    """IP deste PC que a aba Configurações sugere para SERVER_LAN_IP."""
    return _suggest(ips) if ips else ""


def _suggest(ips: frozenset[str]) -> str:
    """O IP deste PC na mesma rede dos faciais (ex.: 192.168.15.x)."""
    for url in configured_device_urls():
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
        prefix = host.rsplit(".", 1)[0] + "."
        for ip in sorted(ips):
            if ip.startswith(prefix):
                return ip
    return sorted(ips)[0]


def lan_ip_problem(local_ips: frozenset[str] | None = None) -> str:
    """Mensagem para a tela quando o SERVER_LAN_IP não serve. Vazio quando está certo."""
    reload_env()
    configured = os.getenv("SERVER_LAN_IP", "").strip()
    if not configured:
        return (
            "O IP deste PC (SERVER_LAN_IP) está vazio. O facial não consegue baixar as fotos "
            "deste PC. Preencha em ADM > Configurações (engrenagem) > Este PC."
        )
    if on_chromeos():
        # No Chromebook o IP do Wi-Fi fica fora do Linux, então não dá para comparar.
        return ""
    try:
        ipaddress.ip_address(configured)
    except ValueError:
        return (
            f"SERVER_LAN_IP={configured} não é um endereço IP. "
            "Corrija em ADM > Configurações (engrenagem) > Este PC."
        )
    ips = local_ipv4_addresses() if local_ips is None else local_ips
    if not ips or configured in ips:
        return ""
    return (
        f"SERVER_LAN_IP={configured} não é deste PC "
        f"(este PC usa {', '.join(sorted(ips))}). "
        "O facial iria buscar a foto em outro computador e o cadastro ficaria sem foto. "
        f"Troque para {_suggest(ips)} em ADM > Configurações (engrenagem) > Este PC."
    )
