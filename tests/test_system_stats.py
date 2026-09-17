import json
import socket
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

import psutil
import system_stats
from app import SysPiper


class SystemStatsTests(unittest.TestCase):
    def test_interfaces_preserve_ipv6_and_raw_counters(self):
        Address = namedtuple('Address', 'family address netmask broadcast ptp')
        Link = namedtuple('Link', 'isup duplex speed mtu')
        Counter = namedtuple('Counter', 'bytes_sent bytes_recv errin dropout')
        with patch.object(psutil, 'net_if_addrs', return_value={
            'eth0': [Address(socket.AF_INET6, 'fe80::1%eth0', 'ffff:ffff::', None, None)]
        }), patch.object(psutil, 'net_if_stats', return_value={
            'eth0': Link(True, psutil.NIC_DUPLEX_UNKNOWN, 0, 1500)
        }), patch.object(psutil, 'net_io_counters', return_value={
            'eth0': Counter(100, 200, 0, 3)
        }) as counters:
            result = system_stats.interfaces()
        counters.assert_called_once_with(pernic=True, nowrap=False)
        self.assertEqual(result['addresses']['data']['eth0'][0]['family'], 'ipv6')
        self.assertEqual(result['counters']['data']['eth0']['dropout'], 3)
        self.assertIsNone(result['links']['data']['eth0']['speed_mbps'])
        self.assertIsNone(result['links']['data']['eth0']['duplex'])
        self.assertEqual(result['status'], 'ok')

    def test_partial_interfaces(self):
        with patch.object(psutil, 'net_if_addrs', side_effect=PermissionError), \
             patch.object(psutil, 'net_if_stats', return_value={}), \
             patch.object(psutil, 'net_io_counters', return_value={}):
            result = system_stats.interfaces()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['addresses'], {'status': 'permission_denied', 'data': None})

    def test_filesystem_disappearing_does_not_hide_other_mounts(self):
        Partition = namedtuple('Partition', 'device mountpoint fstype opts')
        Usage = namedtuple('Usage', 'total used free percent')
        Stat = namedtuple('Stat', 'f_files f_ffree f_favail')
        with patch.object(psutil, 'disk_partitions', return_value=[
            Partition('/dev/a', '/', 'ext4', 'rw'), Partition('/dev/b', '/gone', 'ext4', 'rw')
        ]), patch.object(psutil, 'disk_usage', side_effect=[Usage(100, 60, 40, 60), OSError()]), \
             patch.object(system_stats.os, 'statvfs', side_effect=[Stat(100, 30, 20), PermissionError()]):
            result = system_stats.filesystems()
        first, second = result['filesystems']['data']
        self.assertEqual(first['inodes']['data']['used'], 70)
        self.assertEqual(first['inodes']['data']['available'], 20)
        self.assertEqual(second['usage']['status'], 'unavailable')
        self.assertEqual(second['inodes']['status'], 'permission_denied')
        self.assertEqual(result['status'], 'partial')

    def test_pressure_missing_denied_and_valid(self):
        with patch.object(Path, 'read_text', side_effect=[
            'some avg10=1.25 avg60=0.50 avg300=0.10 total=12345\n',
            FileNotFoundError(), PermissionError()
        ]):
            result = system_stats.pressure()
        self.assertEqual(result['cpu']['data']['some']['total_us'], 12345)
        self.assertNotIn('full', result['cpu']['data'])
        self.assertEqual(result['memory']['status'], 'unsupported')
        self.assertEqual(result['io']['status'], 'permission_denied')
        self.assertEqual(result['status'], 'partial')

    def test_pressure_rejects_invalid_data(self):
        for value in ('', 'some avg10=0', 'some avg10=nan avg60=0 avg300=0 total=2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                system_stats.parse_pressure(value)

    def test_system_load_and_uptime(self):
        with patch.object(psutil, 'boot_time', return_value=100), \
             patch.object(system_stats.time, 'time', return_value=160), \
             patch.object(system_stats.os, 'getloadavg', return_value=(1, 2, 3)):
            result = system_stats.system()
        self.assertEqual(result['boot']['data']['uptime_seconds'], 60)
        self.assertEqual(result['load']['data'], {'avg1': 1, 'avg5': 2, 'avg15': 3})

    def test_routes_auth_proxy_and_local_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'api_key': 'test', 'allowed_ips': ['127.0.0.1'],
                                          'allowed_nodes': {'hop': 'http://hop.invalid'}}))
            app = SysPiper(str(config))
            client = app.app.test_client()
            headers = {'X-API-Key': 'test'}
            for name in ('interfaces', 'system', 'filesystems', 'pressure', 'apt'):
                with self.subTest(name=name):
                    for suffix in ('', '/hop'):
                        self.assertEqual(client.get('/' + name + suffix).status_code, 401)
                        self.assertEqual(client.get('/' + name + suffix, headers=headers,
                            environ_overrides={'REMOTE_ADDR': '192.0.2.1'}).status_code, 401)
                    response = client.get('/' + name, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('sampled_at', response.json)
                    with patch.object(app, '_fetch_upstream', return_value={'status': 'ok'}) as fetch:
                        response = client.get('/' + name + '/hop', headers=headers)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(fetch.call_args.args[0], 'http://hop.invalid/' + name)
                        self.assertEqual(fetch.call_args.kwargs['headers']['X-SysPiper-Hops'], '1')
