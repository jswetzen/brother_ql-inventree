#!/usr/bin/env python

"""
Backend to support Brother QL-series printers via network.
Works cross-platform.
"""

import asyncio
import logging
import socket
import time
import select

from .generic import BrotherQLBackendGeneric

logger = logging.getLogger(__name__)

STATUS_OID = '1.3.6.1.4.1.2435.3.3.9.1.6.1.0'


def get_snmp_status(host, community='public', timeout=2.0):
    """
    Query printer status via SNMP (UDP/161).
    Returns raw 32-byte status bytes, or None if puresnmp is not installed
    or the query fails.
    """
    try:
        import puresnmp
    except ImportError:
        logger.warning(
            "puresnmp not installed; network status unavailable. "
            "Install with: pip install brother_ql-inventree[network-status]"
        )
        return None
    try:
        async def _get():
            client = puresnmp.Client(host, puresnmp.V2C(community))
            wrapper = puresnmp.PyWrapper(client)
            return await asyncio.wait_for(wrapper.get(STATUS_OID), timeout=timeout)
        raw = asyncio.run(_get())
        return bytes(raw)
    except Exception as e:
        logger.warning("SNMP status query failed for %s: %s", host, e)
        return None


def list_available_devices():
    """
    Discover Brother QL printers on the local network via mDNS/DNS-SD.

    Browses _pdl-datastream._tcp (port 9100 AppSocket/JetDirect),
    which Brother QL network models (QL-810W, QL-820NWB, etc.) advertise
    via Bonjour/AirPrint.

    Returns list of {'identifier': 'tcp://hostname:9100', 'instance': None}.
    Requires: pip install brother_ql-inventree[network-status]
    """
    try:
        from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    except ImportError:
        logger.warning(
            "zeroconf not installed; network discovery unavailable. "
            "Install with: pip install brother_ql-inventree[network-status]"
        )
        return []

    devices = []

    def on_service_state_change(zeroconf, service_type, name, state_change, **kwargs):
        if state_change is ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info is None:
                return
            # Filter to likely Brother QL printers by name
            name_lower = name.lower()
            if 'brother' not in name_lower and 'ql' not in name_lower:
                return
            # Build tcp:// identifier from address and port
            addresses = info.parsed_addresses()
            if not addresses:
                return
            host = addresses[0]
            port = info.port or 9100
            identifier = f'tcp://{host}:{port}'
            devices.append({'identifier': identifier, 'instance': None})
            logger.info("Discovered network printer: %s (%s)", name, identifier)

    zc = Zeroconf()
    browser = ServiceBrowser(zc, '_pdl-datastream._tcp.local.', handlers=[on_service_state_change])
    # Browse for ~2 seconds to collect responses
    time.sleep(2.0)
    zc.close()

    return devices


class BrotherQLBackendNetwork(BrotherQLBackendGeneric):
    """
    BrotherQL backend using TCP network connection (port 9100).
    Status readback uses SNMP (UDP/161) via get_status_snmp().
    """

    def __init__(self, device_specifier):
        """
        device_specifier: string in the format tcp://host[:port] or host[:port].
        """

        self.read_timeout = 2.0
        # strategy : try_twice, select or socket_timeout
        self.strategy = 'socket_timeout'
        if isinstance(device_specifier, str):
            if device_specifier.startswith('tcp://'):
                device_specifier = device_specifier[6:]
            host, _, port = device_specifier.partition(':')
            if port:
                port = int(port)
            else:
                port = 9100
            #try:
            self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.s.connect((host, port))
            #except OSError as e:
            #    raise ValueError('Could not connect to the device.')
            if self.strategy == 'socket_timeout':
                self.s.settimeout(self.read_timeout)
            elif self.strategy == 'try_twice':
                self.s.settimeout(self.read_timeout)
            else:
                self.s.settimeout(0)

        elif isinstance(device_specifier, int):
            self.dev = device_specifier
        else:
            raise NotImplementedError('Currently the printer can be specified either via an appropriate string or via an os.open() handle.')

    def get_status_snmp(self):
        """Query printer status via SNMP using the connected host."""
        host = self.s.getpeername()[0]
        return get_snmp_status(host)

    def _write(self, data):
        self.s.settimeout(10)
        self.s.sendall(data)
        self.s.settimeout(self.read_timeout)

    def _read(self, length=32):
        if self.strategy in ('socket_timeout', 'try_twice'):
            if self.strategy == 'socket_timeout':
                tries = 1
            if self.strategy == 'try_twice':
                tries = 2
            for i in range(tries):
                try:
                    data = self.s.recv(length)
                    return data
                except socket.timeout:
                    pass
            return b''
        elif self.strategy == 'select':
            data = b''
            start = time.time()
            while (not data) and (time.time() - start < self.read_timeout):
                result, _, _ = select.select([self.s], [], [], 0)
                if self.s in result:
                    data += self.s.recv(length)
                if data: break
                time.sleep(0.001)
            return data
        else:
            raise NotImplementedError('Unknown strategy')

    def _dispose(self):
        self.s.shutdown(socket.SHUT_RDWR)
        self.s.close()
