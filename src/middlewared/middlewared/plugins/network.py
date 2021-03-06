from middlewared.service import Service, private
from middlewared.utils import Popen
from middlewared.schema import accepts, Str

import asyncio
import ipaddr
import ipaddress
import netif
import os
import re
import signal
import subprocess
import urllib.request


def dhclient_status(interface):
    """
    Get the current status of dhclient for a given `interface`.

    Args:
        interface (str): name of the interface

    Returns:
        tuple(bool, pid): if dhclient is running follow its pid.
    """
    pidfile = '/var/run/dhclient.{}.pid'.format(interface)
    pid = None
    if os.path.exists(pidfile):
        with open(pidfile, 'r') as f:
            try:
                pid = int(f.read().strip())
            except ValueError:
                pass

    running = False
    if pid:
        try:
            os.kill(pid, 0)
        except OSError:
            pass
        else:
            running = True
    return running, pid


def dhclient_leases(interface):
    """
    Reads the leases file for `interface` and returns the content.

    Args:
        interface (str): name of the interface.

    Returns:
        str: content of dhclient leases file for `interface`.
    """
    leasesfile = '/var/db/dhclient.leases.{}'.format(interface)
    if os.path.exists(leasesfile):
        with open(leasesfile, 'r') as f:
            return f.read()


class InterfacesService(Service):

    @private
    async def sync(self):
        """
        Sync interfaces configured in database to the OS.
        """

        interfaces = [i['int_interface'] for i in (await self.middleware.call('datastore.query', 'network.interfaces'))]
        cloned_interfaces = []
        parent_interfaces = []

        # First of all we need to create the virtual interfaces
        # LAGG comes first and then VLAN
        laggs = await self.middleware.call('datastore.query', 'network.lagginterface')
        for lagg in laggs:
            name = lagg['lagg_interface']['int_interface']
            cloned_interfaces.append(name)
            self.logger.info('Setting up {}'.format(name))
            try:
                iface = netif.get_interface(name)
            except KeyError:
                netif.create_interface(name)
                iface = netif.get_interface(name)

            protocol = getattr(netif.AggregationProtocol, lagg['lagg_protocol'].upper())
            if iface.protocol != protocol:
                self.logger.info('{}: changing protocol to {}'.format(name, protocol))
                iface.protocol = protocol

            members_configured = set(p[0] for p in iface.ports)
            members_database = set()
            for member in (await self.middleware.call('datastore.query', 'network.lagginterfacemembers', [('lagg_interfacegroup_id', '=', lagg['id'])])):
                members_database.add(member['lagg_physnic'])

            # Remeve member configured but not in database
            for member in (members_configured - members_database):
                iface.delete_port(member)

            # Add member in database but not configured
            for member in (members_database - members_configured):
                iface.add_port(member)

            for port in iface.ports:
                try:
                    port_iface = netif.get_interface(port[0])
                except KeyError:
                    self.logger.warn('Could not find {} from {}'.format(port[0], name))
                    continue
                parent_interfaces.append(port[0])
                port_iface.up()

        vlans = await self.middleware.call('datastore.query', 'network.vlan')
        for vlan in vlans:
            cloned_interfaces.append(vlan['vlan_vint'])
            self.logger.info('Setting up {}'.format(vlan['vlan_vint']))
            try:
                iface = netif.get_interface(vlan['vlan_vint'])
            except KeyError:
                netif.create_interface(vlan['vlan_vint'])
                iface = netif.get_interface(vlan['vlan_vint'])

            if iface.parent != vlan['vlan_pint'] or iface.tag != vlan['vlan_tag'] or iface.pcp != vlan['vlan_pcp']:
                iface.unconfigure()
                iface.configure(vlan['vlan_pint'], vlan['vlan_tag'], vlan['vlan_pcp'])

            try:
                parent_iface = netif.get_interface(iface.parent)
            except KeyError:
                self.logger.warn('Could not find {} from {}'.format(iface.parent, vlan['vlan_vint']))
                continue
            parent_interfaces.append(iface.parent)
            parent_iface.up()

        self.logger.info('Interfaces in database: {}'.format(', '.join(interfaces) or 'NONE'))
        for interface in interfaces:
            try:
                await self.sync_interface(interface)
            except:
                self.logger.error('Failed to configure {}'.format(interface), exc_info=True)

        internal_interfaces = ['lo', 'pflog', 'pfsync', 'tun', 'tap', 'bridge', 'epair']
        if not await self.middleware.call('system.is_freenas'):
            internal_interfaces.extend(await self.middleware.call('notifier.failover_internal_interfaces') or [])
        internal_interfaces = tuple(internal_interfaces)

        # Destroy interfaces which are not in database
        for name, iface in list(netif.list_interfaces().items()):
            # Skip internal interfaces
            if name.startswith(internal_interfaces):
                continue
            # Skip interfaces in database
            if name in interfaces:
                continue

            # Interface not in database lose addresses
            for address in iface.addresses:
                iface.remove_address(address)

            # Kill dhclient if its running for this interface
            dhclient_running, dhclient_pid = dhclient_status(name)
            if dhclient_running:
                os.kill(dhclient_pid, signal.SIGTERM)

            # If we have vlan or lagg not in the database at all
            # It gets destroy, otherwise just bring it down
            if name not in cloned_interfaces and name.startswith(('lagg', 'vlan')):
                netif.destroy_interface(name)
            elif name not in parent_interfaces:
                iface.down()

    @private
    def alias_to_addr(self, alias):
        addr = netif.InterfaceAddress()
        ip = ipaddress.ip_interface('{}/{}'.format(alias['address'], alias['netmask']))
        addr.af = getattr(netif.AddressFamily, 'INET6' if ':' in alias['address'] else 'INET')
        addr.address = ip.ip
        addr.netmask = ip.netmask
        addr.broadcast = ip.network.broadcast_address
        if 'vhid' in alias:
            addr.vhid = alias['vhid']
        return addr

    @private
    async def sync_interface(self, name):
        try:
            data = await self.middleware.call('datastore.query', 'network.interfaces', [('int_interface', '=', name)], {'get': True})
        except IndexError:
            self.logger.info('{} is not in interfaces database'.format(name))
            return

        aliases = await self.middleware.call('datastore.query', 'network.alias', [('alias_interface_id', '=', data['id'])])

        iface = netif.get_interface(name)

        addrs_database = set()
        addrs_configured = set([
            a for a in iface.addresses
            if a.af != netif.AddressFamily.LINK
        ])

        has_ipv6 = data['int_ipv6auto'] or False

        if (
            not await self.middleware.call('system.is_freenas') and
            await self.middleware.call('notifier.failover_node') == 'B'
        ):
            ipv4_field = 'int_ipv4address_b'
            ipv6_field = 'int_ipv6address'
            alias_ipv4_field = 'alias_v4address_b'
            alias_ipv6_field = 'alias_v6address_b'
        else:
            ipv4_field = 'int_ipv4address'
            ipv6_field = 'int_ipv6address'
            alias_ipv4_field = 'alias_v4address'
            alias_ipv6_field = 'alias_v6address'

        dhclient_running, dhclient_pid = dhclient_status(name)
        if dhclient_running and data['int_dhcp']:
            leases = dhclient_leases(name)
            if leases:
                reg_address = re.search(r'fixed-address\s+(.+);', leases)
                reg_netmask = re.search(r'option subnet-mask\s+(.+);', leases)
                if reg_address and reg_netmask:
                    addrs_database.add(self.alias_to_addr({
                        'address': reg_address.group(1),
                        'netmask': reg_netmask.group(1),
                    }))
                else:
                    self.logger.info('Unable to get address from dhclient')
            if data[ipv6_field] and has_ipv6 is False:
                addrs_database.add(self.alias_to_addr({
                    'address': data[ipv6_field],
                    'netmask': data['int_v6netmaskbit'],
                }))
        else:
            if data[ipv4_field]:
                addrs_database.add(self.alias_to_addr({
                    'address': data[ipv4_field],
                    'netmask': data['int_v4netmaskbit'],
                }))
            if data[ipv6_field] and has_ipv6 is False:
                addrs_database.add(self.alias_to_addr({
                    'address': data[ipv6_field],
                    'netmask': data['int_v6netmaskbit'],
                }))
                has_ipv6 = True

        carp_vhid = carp_pass = None
        if data['int_vip']:
            addrs_database.add(self.alias_to_addr({
                'address': data['int_vip'],
                'netmask': '32',
                'vhid': data['int_vhid'],
            }))
            carp_vhid = data['int_vhid']
            carp_pass = data['int_pass'] or None

        for alias in aliases:
            if alias[alias_ipv4_field]:
                addrs_database.add(self.alias_to_addr({
                    'address': alias[alias_ipv4_field],
                    'netmask': alias['alias_v4netmaskbit'],
                }))
            if alias[alias_ipv6_field]:
                addrs_database.add(self.alias_to_addr({
                    'address': alias[alias_ipv6_field],
                    'netmask': alias['alias_v6netmaskbit'],
                }))

            if alias['alias_vip']:
                addrs_database.add(self.alias_to_addr({
                    'address': alias['alias_vip'],
                    'netmask': '32',
                    'vhid': data['int_vhid'],
                }))

        if carp_vhid:
            advskew = None
            for cc in iface.carp_config:
                if cc.vhid == carp_vhid:
                    advskew = cc.advskew
                    break

        if has_ipv6:
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.IFDISABLED}
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.AUTO_LINKLOCAL}
        else:
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.IFDISABLED}
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.AUTO_LINKLOCAL}

        # Remove addresses configured and not in database
        for addr in (addrs_configured - addrs_database):
            if has_ipv6 and str(addr.address).startswith('fe80::'):
                continue
            self.logger.debug('{}: removing {}'.format(name, addr))
            iface.remove_address(addr)

        # carp must be configured after removing addresses
        # in case removing the address removes the carp
        if carp_vhid:
            if not await self.middleware.call('system.is_freenas') and not advskew:
                if await self.middleware.call('notifier.failover_node') == 'A':
                    advskew = 20
                else:
                    advskew = 80
            # FIXME: change py-netif to accept str() key
            iface.carp_config = [netif.CarpConfig(carp_vhid, advskew=advskew, key=carp_pass.encode())]

        # Add addresses in database and not configured
        for addr in (addrs_database - addrs_configured):
            self.logger.debug('{}: adding {}'.format(name, addr))
            iface.add_address(addr)

        # Apply interface options specified in GUI
        if data['int_options']:
            self.logger.info('{}: applying {}'.format(name, data['int_options']))
            proc = await Popen('/sbin/ifconfig {} {}'.format(name, data['int_options']), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
            err = (await proc.communicate())[1].decode()
            if err:
                self.logger.info('{}: error applying: {}'.format(name, err))

            # In case there is no MTU in interface options and it is currently
            # different than the default of 1500, revert it
            if data['int_options'].find('mtu') == -1 and iface.mtu != 1500:
                iface.mtu = 1500

        if netif.InterfaceFlags.UP not in iface.flags:
            iface.up()

        # If dhclient is not running and dhcp is configured, lets start it
        if not dhclient_running and data['int_dhcp']:
            self.logger.debug('Starting dhclient for {}'.format(name))
            asyncio.ensure_future(self.dhclient_start(data['int_interface']))
        elif dhclient_running and not data['int_dhcp']:
            self.logger.debug('Killing dhclient for {}'.format(name))
            os.kill(dhclient_pid, signal.SIGTERM)

        if data['int_ipv6auto']:
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.ACCEPT_RTADV}
            await (await Popen(
                ['/etc/rc.d/rtsold', 'onestart'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )).wait()
        else:
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.ACCEPT_RTADV}

    @private
    async def dhclient_start(self, interface):
        proc = await Popen([
            '/sbin/dhclient', '-b', interface,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
        output = (await proc.communicate())[0].decode()
        if proc.returncode != 0:
            self.logger.error('Failed to run dhclient on {}: {}'.format(
                interface, output,
            ))

    @accepts()
    def ipv4_in_use(self):
        """
        Get all IPv4 from all valid interfaces, excluding lo0, bridge* and tap*.

        Returns:
            list: A list with all IPv4 in use, or an empty list.
        """
        list_of_ipv4 = []
        ignore_nics = ('lo', 'bridge', 'tap', 'epair')
        for if_name, iface in list(netif.list_interfaces().items()):
            if not if_name.startswith(ignore_nics):
                for nic_address in iface.addresses:
                    if nic_address.af == netif.AddressFamily.INET:
                        ipv4_address = nic_address.address.exploded
                        list_of_ipv4.append(str(ipv4_address))

        return list_of_ipv4


class RoutesService(Service):

    @private
    async def sync(self):
        config = await self.middleware.call('datastore.query', 'network.globalconfiguration', [], {'get': True})

        # Generate dhclient.conf so we can ignore routes (def gw) option
        # in case there is one explictly set in network config
        await self.middleware.call('etc.generate', 'network')

        ipv4_gateway = config['gc_ipv4gateway'] or None
        if not ipv4_gateway:
            interface = await self.middleware.call('datastore.query', 'network.interfaces', [('int_dhcp', '=', True)])
            if interface:
                interface = interface[0]
                dhclient_running, dhclient_pid = dhclient_status(interface['int_interface'])
                if dhclient_running:
                    leases = dhclient_leases(interface['int_interface'])
                    reg_routers = re.search(r'option routers (.+);', leases or '')
                    if reg_routers:
                        # Make sure to get first route only
                        ipv4_gateway = reg_routers.group(1).split(' ')[0]
        routing_table = netif.RoutingTable()
        if ipv4_gateway:
            ipv4_gateway = netif.Route('0.0.0.0', '0.0.0.0', ipaddress.ip_address(str(ipv4_gateway)))
            ipv4_gateway.flags.add(netif.RouteFlags.STATIC)
            ipv4_gateway.flags.add(netif.RouteFlags.GATEWAY)
            # If there is a gateway but there is none configured, add it
            # Otherwise change it
            if not routing_table.default_route_ipv4:
                self.logger.info('Adding IPv4 default route to {}'.format(ipv4_gateway.gateway))
                routing_table.add(ipv4_gateway)
            elif ipv4_gateway != routing_table.default_route_ipv4:
                self.logger.info('Changing IPv4 default route from {} to {}'.format(routing_table.default_route_ipv4.gateway, ipv4_gateway.gateway))
                routing_table.change(ipv4_gateway)
        elif routing_table.default_route_ipv4:
            # If there is no gateway in database but one is configured
            # remove it
            self.logger.info('Removing IPv4 default route')
            routing_table.delete(routing_table.default_route_ipv4)

        ipv6_gateway = config['gc_ipv6gateway'] or None
        if ipv6_gateway:
            ipv6_gateway = netif.Route('::', '::', ipaddress.ip_address(str(ipv6_gateway)))
            ipv6_gateway.flags.add(netif.RouteFlags.STATIC)
            ipv6_gateway.flags.add(netif.RouteFlags.GATEWAY)
            # If there is a gateway but there is none configured, add it
            # Otherwise change it
            if not routing_table.default_route_ipv6:
                self.logger.info('Adding IPv6 default route to {}'.format(ipv6_gateway.gateway))
                routing_table.add(ipv6_gateway)
            elif ipv6_gateway != routing_table.default_route_ipv6:
                self.logger.info('Changing IPv6 default route from {} to {}'.format(routing_table.default_route_ipv6.gateway, ipv6_gateway.gateway))
                routing_table.change(ipv6_gateway)
        elif routing_table.default_route_ipv6:
            # If there is no gateway in database but one is configured
            # remove it
            self.logger.info('Removing IPv6 default route')
            routing_table.delete(routing_table.default_route_ipv6)

    @accepts(Str('ipv4_gateway'))
    def ipv4gw_reachable(self, ipv4_gateway):
        """
            Get the IPv4 gateway and verify if it is reachable by any interface.

            Returns:
                bool: True if the gateway is reachable or otherwise False.
        """
        ignore_nics = ('lo', 'bridge', 'tap', 'epair')
        for if_name, iface in list(netif.list_interfaces().items()):
            if not if_name.startswith(ignore_nics):
                for nic_address in iface.addresses:
                    if nic_address.af == netif.AddressFamily.INET:
                        ipv4_nic = ipaddress.IPv4Interface(nic_address)
                        nic_network = ipaddr.IPv4Network(ipv4_nic)
                        nic_prefixlen = nic_network.prefixlen
                        nic_result = str(nic_network.network) + '/' + str(nic_prefixlen)
                        if ipaddress.ip_address(ipv4_gateway) in ipaddress.ip_network(nic_result):
                            return True
        return False


class DNSService(Service):

    @private
    async def sync(self):
        domain = None
        nameservers = []

        if await self.middleware.call('notifier.common', 'system', 'domaincontroller_enabled'):
            cifs = await self.middleware.call('datastore.query', 'services.cifs', None, {'get': True})
            dc = await self.middleware.call('datastore.query', 'services.DomainController', None, {'get': True})
            domain = dc['dc_realm']
            if cifs['cifs_srv_bindip']:
                for ip in cifs['cifs_srv_bindip']:
                    nameservers.append(ip)
            else:
                nameservers.append('127.0.0.1')
        else:
            gc = await self.middleware.call('datastore.query', 'network.globalconfiguration', None, {'get': True})
            if gc['gc_domain']:
                domain = gc['gc_domain']
            if gc['gc_nameserver1']:
                nameservers.append(gc['gc_nameserver1'])
            if gc['gc_nameserver2']:
                nameservers.append(gc['gc_nameserver2'])
            if gc['gc_nameserver3']:
                nameservers.append(gc['gc_nameserver3'])

        resolvconf = ''
        if domain:
            resolvconf += 'search {}\n'.format(domain)
        for ns in nameservers:
            resolvconf += 'nameserver {}\n'.format(ns)

        proc = await Popen([
            '/sbin/resolvconf', '-a', 'lo0'
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        data = await proc.communicate(input=resolvconf.encode())
        if proc.returncode != 0:
            self.logger.warn(f'Failed to run resolvconf: {data[1].decode()}')


async def configure_http_proxy(middleware, *args, **kwargs):
    """
    Configure the `http_proxy` and `https_proxy` environment vars
    from the database.
    """
    gc = await middleware.call('datastore.config', 'network.globalconfiguration')
    http_proxy = gc['gc_httpproxy']
    if http_proxy:
        os.environ['http_proxy'] = http_proxy
        os.environ['https_proxy'] = http_proxy
    elif not http_proxy:
        if 'http_proxy' in os.environ:
            del os.environ['http_proxy']
        if 'https_proxy' in os.environ:
            del os.environ['https_proxy']

    # Reset global opener so ProxyHandler can be recalculated
    urllib.request.install_opener(None)


async def setup(middleware):
    # Configure http proxy on startup and on network.config events
    asyncio.ensure_future(configure_http_proxy(middleware))
    middleware.event_subscribe('network.config', configure_http_proxy)
