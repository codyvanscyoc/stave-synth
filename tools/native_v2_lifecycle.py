"""Non-audio boot helpers: explicit private listeners and systemd heartbeat."""
import ipaddress
import json
import os
import select
import socket
import subprocess


PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def private_addresses(text, interfaces):
    if len(text) > 65536:
        raise ValueError('Oversized network inventory')
    result = {'127.0.0.1'}
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise ValueError('Invalid network inventory')
    for row in rows:
        if row.get('ifname') not in interfaces or 'UP' not in row.get('flags', []):
            continue
        for entry in row.get('addr_info', []):
            if entry.get('family') != 'inet' or entry.get('scope') != 'global':
                continue
            address = ipaddress.ip_address(entry['local'])
            if any(address in network for network in PRIVATE):
                result.add(str(address))
    if len(result) > 8:
        raise ValueError('Too many private listen addresses')
    return result


def discover(interfaces):
    result = subprocess.run(['/usr/sbin/ip', '-j', '-4', 'address', 'show'],
                            capture_output=True, text=True, timeout=1, check=True)
    return private_addresses(result.stdout, interfaces)


class Listeners:
    """Bind only explicit local addresses. Rebinding never owns/restarts audio."""
    def __init__(self, factory):
        self.factory = factory
        self.servers = {}
        self.errors = {}

    def reconcile(self, addresses):
        for address in set(self.servers) - addresses:
            self.servers.pop(address).server_close()
        self.errors = {}
        for address in sorted(addresses - self.servers.keys()):
            try:
                self.servers[address] = self.factory(address)
            except OSError as error:
                self.errors[address] = str(error)

    def poll(self, timeout=.1):
        try:
            ready, _, _ = select.select(list(self.servers.values()), [], [], timeout)
        except OSError:
            return
        for server in ready:
            try:
                server.handle_request()
            except OSError:
                for address, candidate in list(self.servers.items()):
                    if candidate is server:
                        self.servers.pop(address).server_close()
                        break

    def close(self):
        for server in self.servers.values():
            server.server_close()
        self.servers.clear()


def notify(message):
    target = os.environ.get('NOTIFY_SOCKET')
    if not target:
        return
    if target.startswith('@'):
        target = '\0' + target[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.settimeout(.1)
            client.sendto(message.encode(), target)
    except OSError:
        pass  # systemd's deadline still catches an undelivered heartbeat
