"""Stdlib authenticated lifecycle request/reply client.

Input is a canonical CloudEvent on stdin. Reply subjects are correlation-specific;
only a reply HMAC verified by Pilot grants execution, never a NATS PONG.
"""
import argparse
import json
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlparse


def call(envelope, timeout=30):
    target = urlparse(os.environ.get('NATS_URL', 'nats://localhost:4222'))
    sock = socket.create_connection((target.hostname, target.port or 4222), timeout=timeout)
    if target.scheme == 'tls':
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)
    inbox = 'bloodbank.rpy.lifecycle.task.invoke'
    connection = {'verbose': False, 'pedantic': True, 'name': 'bb-call'}
    if target.username:
        connection.update(user=target.username, **({'pass': target.password} if target.password else {}))
    if os.environ.get('NATS_TOKEN'):
        connection['auth_token'] = os.environ['NATS_TOKEN']
    raw = json.dumps(envelope, separators=(',', ':'), ensure_ascii=False).encode()
    deadline = time.monotonic() + timeout
    with sock, sock.makefile('rb') as reader:
        if not reader.readline().startswith(b'INFO '):
            raise RuntimeError('NATS handshake failed')
        sock.sendall(b'CONNECT ' + json.dumps(connection).encode() + b'\r\n')
        sock.sendall(f'SUB {inbox} 1\r\nPING\r\n'.encode())
        # Flush the subscription before publication. PONG is only transport readiness.
        while reader.readline().strip() != b'PONG':
            if time.monotonic() >= deadline:
                raise TimeoutError('NATS subscription timeout')
        sock.sendall(f'PUB {envelope["subject"]} {len(raw)}\r\n'.encode() + raw + b'\r\n')
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            line = reader.readline().strip()
            if line == b'PING':
                sock.sendall(b'PONG\r\n')
            elif line.startswith(b'-ERR') or not line:
                raise RuntimeError('NATS connection rejected or closed')
            elif line.startswith(b'MSG '):
                size = int(line.split()[-1]); data = reader.read(size); reader.read(2)
                reply = json.loads(data)
                if reply.get('causationid') == envelope['command_id']:
                    return reply
        raise TimeoutError('No controller receipt; retain command id and query status/retry same body')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(call(json.load(sys.stdin), args.timeout)))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}), file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
