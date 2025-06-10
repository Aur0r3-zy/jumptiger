#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2012 clowwindy
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import sys
import threading
import json
import time
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler

try:
    import gevent, gevent.monkey
    gevent.monkey.patch_all(dns=gevent.version_info[0]>=1)
except ImportError:
    gevent = None
    print >>sys.stderr, 'warning: gevent not found, using threading instead'

import socket
import select
import SocketServer
import struct
import string
import hashlib
import os
import logging
import getopt

# Global stats for connections and traffic
connection_stats = {
    "connections": [],
    "total_bytes_in": 0,
    "total_bytes_out": 0,
    "total_connections": 0,
    "active_connections": 0,
    "real_time_bytes_in": 0,  # 实时入站流量
    "real_time_bytes_out": 0  # 实时出站流量
}
stats_lock = threading.Lock()

def get_table(key):
    m = hashlib.md5()
    m.update(key)
    s = m.digest()
    (a, b) = struct.unpack('<QQ', s)
    table = [c for c in string.maketrans('', '')]
    for i in xrange(1, 1024):
        table.sort(lambda x, y: int(a % (ord(x) + i) - a % (ord(y) + i)))
    return table

def send_all(sock, data):
    bytes_sent = 0
    while True:
        r = sock.send(data[bytes_sent:])
        if r < 0:
            return r
        bytes_sent += r
        if bytes_sent == len(data):
            return bytes_sent

class ThreadingTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    allow_reuse_address = True


class StatsHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for monitoring stats"""

    def do_GET(self):
        if self.path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            with stats_lock:
                self.wfile.write(json.dumps(connection_stats))
        else:
            self.send_error(404)

def start_http_server(port=8388):
    """Start the HTTP server for monitoring"""
    server = HTTPServer(('0.0.0.0', port), StatsHTTPHandler)
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    print("HTTP monitoring server started at http://127.0.0.1:%d" % port)

class Socks5Server(SocketServer.StreamRequestHandler):
    def handle_tcp(self, sock, remote):
        try:
            fdset = [sock, remote]
            client_id = "%s:%d" % (self.client_address[0], self.client_address[1])
            with stats_lock:
                connection_stats["connections"].append({
                    "id": client_id,
                    "host": self.client_address[0],
                    "port": self.client_address[1],
                    "bytes_in": 0,
                    "bytes_out": 0,
                    "status": "active",
                    "start_time": time.time()
                })
                connection_stats["total_connections"] += 1
                connection_stats["active_connections"] += 1

            last_update_time = time.time()
            while True:
                r, w, e = select.select(fdset, [], [])
                if sock in r:
                    data = sock.recv(4096)
                    if len(data) <= 0:
                        break
                    result = send_all(remote, self.decrypt(data))
                    if result < len(data):
                        raise Exception('failed to send all data')
                    with stats_lock:
                        for conn in connection_stats["connections"]:
                            if conn["id"] == client_id:
                                conn["bytes_in"] += len(data)
                        connection_stats["total_bytes_in"] += len(data)
                        connection_stats["real_time_bytes_in"] += len(data)

                if remote in r:
                    data = remote.recv(4096)
                    if len(data) <= 0:
                        break
                    result = send_all(sock, self.encrypt(data))
                    if result < len(data):
                        raise Exception('failed to send all data')
                    with stats_lock:
                        for conn in connection_stats["connections"]:
                            if conn["id"] == client_id:
                                conn["bytes_out"] += len(data)
                        connection_stats["total_bytes_out"] += len(data)
                        connection_stats["real_time_bytes_out"] += len(data)

                # 每秒更新实时流量
                current_time = time.time()
                if current_time - last_update_time >= 1:
                    with stats_lock:
                        connection_stats["real_time_bytes_in"] = 0
                        connection_stats["real_time_bytes_out"] = 0
                    last_update_time = current_time

        finally:
            sock.close()
            remote.close()
            with stats_lock:
                for conn in connection_stats["connections"]:
                    if conn["id"] == client_id:
                        conn["status"] = "closed"
                connection_stats["active_connections"] -= 1

    def encrypt(self, data):
        return data.translate(encrypt_table)

    def decrypt(self, data):
        return data.translate(decrypt_table)

    def handle(self):
        try:
            sock = self.connection
            addrtype = ord(self.decrypt(sock.recv(1)))      # receive addr type
            if addrtype == 1:
                addr = socket.inet_ntoa(self.decrypt(self.rfile.read(4)))   # get dst addr
            elif addrtype == 3:
                addr = self.decrypt(
                    self.rfile.read(ord(self.decrypt(sock.recv(1)))))       # read 1 byte of len, then get 'len' bytes name
            else:
                # not support
                logging.warn('addr_type not support')
                return
            port = struct.unpack('>H', self.decrypt(self.rfile.read(2)))    # get dst port into small endian
            try:
                logging.info('connecting %s:%d' % (addr, port[0]))
                remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                remote.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                remote.connect((addr, port[0]))         # connect to dst
            except socket.error, e:
                # Connection refused
                logging.warn(e)
                return
            self.handle_tcp(sock, remote)
        except socket.error, e:
            logging.warn(e)

if __name__ == '__main__':
    os.chdir(os.path.dirname(__file__) or '.')

    print 'shadowsocks v0.9'

    with open('config.json', 'rb') as f:
        config = json.load(f)

    SERVER = config['server']
    PORT = config['server_port']
    KEY = config['password']

    optlist, args = getopt.getopt(sys.argv[1:], 'p:k:')
    for key, value in optlist:
        if key == '-p':
            PORT = int(value)
        elif key == '-k':
            KEY = value

    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S', filemode='a+')

    encrypt_table = ''.join(get_table(KEY))
    decrypt_table = string.maketrans(encrypt_table, string.maketrans('', ''))
    if '-6' in sys.argv[1:]:
        ThreadingTCPServer.address_family = socket.AF_INET6

    # Start the HTTP monitoring server
    start_http_server(port=8388)

    try:
        server = ThreadingTCPServer(('', PORT), Socks5Server)
        logging.info("starting server at port %d ..." % PORT)
        server.serve_forever()
    except socket.error, e:
        logging.error(e)

