# Copyright (c) 2019 Dubalu LLC
# Copyright (c) 2017 Elasticsearch
#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to you under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
from itertools import chain

from .connection import Urllib3HttpConnection
from .connection_pool import ConnectionPool, DummyConnectionPool
from .serializer import Deserializer, DEFAULT_SERIALIZERS, DEFAULT_SERIALIZER
from .exceptions import ConnectionError, TransportError, SerializationError, ConnectionTimeout


def get_node_info(node_info, host):
    """
    Simple callback that takes the node info from `GET /` and a
    parsed connection information and return the connection information. If
    `None` is returned this node will be skipped.

    Useful for filtering nodes (by proximity for example) or if additional
    information needs to be provided for the :class:`~xapiand.Connection`
    class.

    :arg node_info: node information from `GET /`
    :arg host: connection information (host, port) extracted from the node info
    """
    return host


class Transport(object):
    """
    Encapsulation of transport-related to logic. Handles instantiation of the
    individual connections as well as creating a connection pool to hold them.

    Main interface is the `perform_request` method.
    """
    def __init__(self, hosts, connection_class=Urllib3HttpConnection,
                 connection_pool_class=ConnectionPool, node_info_callback=get_node_info,
                 sniff_on_start=False, sniffer_timeout=None, sniff_timeout=.5,
                 sniff_on_connection_fail=False, serializer=DEFAULT_SERIALIZER, serializers=None,
                 default_mimetype=DEFAULT_SERIALIZER.mimetype, max_retries=3, retry_on_status=(502, 503, 504),
                 retry_on_timeout=False, http_method_override=False, **kwargs):
        """
        :arg hosts: list of dictionaries, each containing keyword arguments to
            create a `connection_class` instance
        :arg connection_class: subclass of :class:`~xapiand.Connection` to use
        :arg connection_pool_class: subclass of :class:`~xapiand.ConnectionPool` to use
        :arg node_info_callback: callback responsible for taking the node information from
            `GET /`, along with already extracted information, and
            producing a list of arguments (same as `hosts` parameter)
        :arg sniff_on_start: flag indicating whether to obtain a list of nodes
            from the cluser at startup time
        :arg sniffer_timeout: number of seconds between automatic sniffs
        :arg sniff_on_connection_fail: flag controlling if connection failure triggers a sniff
        :arg sniff_timeout: timeout used for the sniff request - it should be a
            fast api call and we are talking potentially to more nodes so we want
            to fail quickly. Not used during initial sniffing (if
            ``sniff_on_start`` is on) when the connection still isn't
            initialized.
        :arg serializer: serializer instance
        :arg serializers: optional dict of serializer instances that will be
            used for deserializing data coming from the server. (key is the mimetype)
        :arg default_mimetype: when no mimetype is specified by the server
            response assume this mimetype, defaults to `'application/x-msgpack'`
        :arg max_retries: maximum number of retries before an exception is propagated
        :arg retry_on_status: set of HTTP status codes on which we should retry
            on a different node. defaults to ``(502, 503, 504)``
        :arg retry_on_timeout: should timeout trigger a retry on different
            node? (default `False`)
        :arg http_method_override: for environments that don't support passing
            bodies with GET requests or non-standard methods such as `MERGE`,
            this option allows you to specify using `POST` and the
            `X-HTTP-Method-Override` header instead.
        :arg kwargs: any additional arguments will be passed on to the
            :class:`~xapiand.Connection` instances.

        Any extra keyword arguments will be passed to the `connection_class`
        when creating and instance unless overridden by that connection's
        options provided as part of the hosts parameter.
        """

        # serialization config
        _serializers = DEFAULT_SERIALIZERS.copy()
        # if a serializer has been specified, use it for deserialization as well
        _serializers[serializer.mimetype] = serializer
        # if custom serializers map has been supplied, override the defaults with it
        if serializers:
            _serializers.update(serializers)
        # create a deserializer with our config
        self.deserializer = Deserializer(_serializers, default_mimetype)

        self.max_retries = max_retries
        self.retry_on_timeout = retry_on_timeout
        self.retry_on_status = retry_on_status
        self.http_method_override = http_method_override

        # data serializer
        self.serializer = serializer

        # store all strategies...
        self.connection_pool_class = connection_pool_class
        self.connection_class = connection_class

        # ...save kwargs to be passed to the connections
        self.kwargs = kwargs
        self.hosts = hosts

        # ...and instantiate them
        self.set_connections(hosts)
        # retain the original connection instances for sniffing
        self.seed_connections = self.connection_pool.connections[:]

        # sniffing data
        self.sniffer_timeout = sniffer_timeout
        self.sniff_on_connection_fail = sniff_on_connection_fail
        self.last_sniff = time.time()
        self.sniff_timeout = sniff_timeout

        # callback to construct host dict from data in `GET /`
        self.node_info_callback = node_info_callback

        if sniff_on_start:
            self.sniff_hosts(True)

    def add_connection(self, host):
        """
        Create a new :class:`~xapiand.Connection` instance and add it to the pool.

        :arg host: kwargs that will be used to create the instance
        """
        self.hosts.append(host)
        self.set_connections(self.hosts)

    def set_connections(self, hosts):
        """
        Instantiate all the connections and create new connection pool to hold them.
        Tries to identify unchanged hosts and re-use existing
        :class:`~xapiand.Connection` instances.

        :arg hosts: same as `__init__`
        """
        # construct the connections
        def _create_connection(host):
            # if this is not the initial setup look at the existing connection
            # options and identify connections that haven't changed and can be
            # kept around.
            if hasattr(self, 'connection_pool'):
                for (connection, old_host) in self.connection_pool.connection_opts:
                    if old_host == host:
                        return connection

            # previously unseen params, create new connection
            kwargs = self.kwargs.copy()
            kwargs.update(host)
            return self.connection_class(**kwargs)
        connections = map(_create_connection, hosts)

        connections = list(zip(connections, hosts))
        if len(connections) == 1:
            self.connection_pool = DummyConnectionPool(connections)
        else:
            # pass the hosts dicts to the connection pool to optionally extract parameters from
            self.connection_pool = self.connection_pool_class(connections, **self.kwargs)

    def get_connection(self, **kwargs):
        """
        Retreive a :class:`~xapiand.Connection` instance from the
        :class:`~xapiand.ConnectionPool` instance.
        """
        if self.sniffer_timeout:
            if time.time() >= self.last_sniff + self.sniffer_timeout:
                self.sniff_hosts()
        return self.connection_pool.get_connection(**kwargs)

    def _get_sniff_data(self, initial=False):
        """
        Perform the request to get sniffins information. Returns a list of
        dictionaries (one per node) containing all the information from the
        cluster.

        It also sets the last_sniff attribute in case of a successful attempt.

        In rare cases it might be possible to override this method in your
        custom Transport class to serve data from alternative source like
        configuration management.
        """
        previous_sniff = self.last_sniff

        try:
            # reset last_sniff timestamp
            self.last_sniff = time.time()
            # go through all current connections as well as the
            # seed_connections for good measure
            for c in chain(self.connection_pool.connections, self.seed_connections):
                try:
                    # use small timeout for the sniffing request, should be a fast api call
                    _, headers, response = c.perform_request('GET', '/',
                        timeout=self.sniff_timeout if not initial else None, ignore_url_prefix=True)
                    response = self.deserializer.loads(response, headers.get('content-type'))
                    break
                except (ConnectionError, SerializationError):
                    pass
            else:
                raise TransportError("N/A", "Unable to sniff hosts.")
        except Exception:
            # keep the previous value on error
            self.last_sniff = previous_sniff
            if initial:
                return []
            raise

        return response['nodes']

    def _get_node_info(self, node_info):
        name = node_info.get('name')
        host = node_info.get('host')
        port = node_info.get('http_port')
        node_info = {
            'name': name,
        }
        if port is not None:
            node_info['port'] = port
        if host is not None:
            node_info['host'] = host
        return node_info

    def sniff_hosts(self, initial=False):
        """
        Obtain a list of nodes from the cluster and create a new connection
        pool using the information retrieved.

        To extract the node connection parameters use the ``nodes_to_host_callback``.

        :arg initial: flag indicating if this is during startup
            (``sniff_on_start``), ignore the ``sniff_timeout`` if ``True``
        """
        node_info = self._get_sniff_data(initial)

        hosts = [self._get_node_info(n) for n in node_info]
        hosts.sort(key=lambda x: x['name'].lower())

        # we weren't able to get any nodes or node_info_callback blocked all -
        # raise error.
        if not hosts:
            if initial:
                return
            raise TransportError("N/A", "Unable to sniff hosts - no viable hosts found.")

        self.set_connections(hosts)

    def mark_dead(self, connection):
        """
        Mark a connection as dead (failed) in the connection pool. If sniffing
        on failure is enabled this will initiate the sniffing process.

        :arg connection: instance of :class:`~xapiand.Connection` that failed
        """
        # mark as dead even when sniffing to avoid hitting this host during the sniff process
        self.connection_pool.mark_dead(connection)
        if self.sniff_on_connection_fail:
            self.sniff_hosts()

    def perform_request(self, method, url, **kwargs):
        """
        Perform the actual request. Retrieve a connection from the connection
        pool, pass all the information to it's perform_request method and
        return the data.

        If an exception was raised, mark the connection as failed and retry (up
        to `max_retries` times).

        If the operation was succesful and the connection used was previously
        marked as dead, mark it as live, resetting it's failure count.

        :arg method: HTTP method to use
        :arg url: url (without host) to target
        :arg headers: dictionary of headers, will be handed over to the
            underlying :class:`~xapiand.Connection` class
        :arg params: dictionary of query parameters, will be handed over to the
            underlying :class:`~xapiand.Connection` class for serialization
        :arg body: body of the request, will be serializes using serializer and
            passed to the connection
        """
        headers = kwargs.get('headers')
        params = kwargs.get('params')
        body = kwargs.get('body')

        if body is not None:
            body = self.serializer.dumps(body)

        if self.http_method_override and (
            (body is not None and method in ('GET', 'OPTIONS', 'HEAD')) or
            method not in ('POST', 'PUT', 'PATCH', 'DELETE')
        ):
            # some clients or environments don't support sending GET with body or
            # non-standard HTTP methods (such as `UPDATE` or `STORE`), send the
            # request it as `POST` instead, with the X-HTTP-Method-Override header.
            if headers is None:
                headers = {}
            headers['X-HTTP-Method-Override'] = method
            method = 'POST'

        ignore = ()
        timeout = None
        if params:
            timeout = params.pop('request_timeout', None)
            ignore = params.pop('ignore', ())
            if isinstance(ignore, int):
                ignore = (ignore, )

        for attempt in range(1, self.max_retries + 1):
            connection = self.get_connection(method=method, path=url, headers=headers, params=params)

            try:
                status, headers_response, data = connection.perform_request(
                    method,
                    url,
                    params,
                    body,
                    headers=headers,
                    ignore=ignore,
                    timeout=timeout,
                    deserializer=self.deserializer,
                )

            except TransportError as e:
                if method == 'HEAD' and e.status_code == 404:
                    return False

                retry = False
                if isinstance(e, ConnectionTimeout):
                    retry = self.retry_on_timeout
                elif isinstance(e, ConnectionError):
                    retry = True
                elif e.status_code in self.retry_on_status:
                    retry = True

                if retry:
                    # only mark as dead if we are retrying
                    self.mark_dead(connection)
                    # raise exception on last retry
                    if attempt == self.max_retries:
                        raise
                else:
                    raise

                # add a delay before attempting the next retry
                # 0, 1, 3, 7, etc...
                delay = 2**attempt - 1
                time.sleep(delay)

            else:
                # connection didn't fail, confirm it's live status
                self.connection_pool.mark_live(connection)

                if method == 'HEAD':
                    return 200 <= status < 300

                return data

    def close(self):
        """
        Explicitly closes connections
        """
        self.connection_pool.close()
