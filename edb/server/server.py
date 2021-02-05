#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2016-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


from __future__ import annotations
from typing import *

import asyncio
import binascii
import json
import logging
import pickle
import uuid

import immutables

from edb import errors

from edb.common import taskgroup

from edb.schema import reflection as s_refl
from edb.schema import schema as s_schema

from edb.edgeql import parser as ql_parser

from edb.server import config
from edb.server import connpool
from edb.server import compiler_pool
from edb.server import defines
from edb.server import http
from edb.server import http_edgeql_port
from edb.server import http_graphql_port
from edb.server import notebook_port
from edb.server import mng_port
from edb.server import pgcon

from . import baseport
from . import dbview


logger = logging.getLogger('edb.server')


class StartupScript(NamedTuple):

    text: str
    database: str
    user: str


class RoleDescriptor(TypedDict):
    superuser: bool
    name: str
    password: str


class Server:

    _ports: List[baseport.Port]
    _sys_conf_ports: Dict[config.ConfigType, baseport.Port]
    _sys_pgcon: Optional[pgcon.PGConnection]

    _roles: Mapping[str, RoleDescriptor]
    _instance_data: Mapping[str, str]
    _sys_queries: Mapping[str, str]
    _local_intro_query: bytes
    _global_intro_query: bytes

    _std_schema: s_schema.Schema
    _refl_schema: s_schema.Schema
    _schema_class_layout: s_refl.SchemaTypeLayout

    def __init__(
        self,
        *,
        loop,
        cluster,
        runstate_dir,
        internal_runstate_dir,
        max_backend_connections,
        compiler_pool_size,
        nethost,
        netport,
        auto_shutdown: bool=False,
        echo_runtime_info: bool = False,
        max_protocol: Tuple[int, int],
        startup_script: Optional[StartupScript] = None,
    ):

        self._loop = loop

        # Used to tag PG notifications to later disambiguate them.
        self._server_id = str(uuid.uuid4())

        self._serving = False

        self._cluster = cluster
        self._pg_addr = self._get_pgaddr()

        # 1 connection is reserved for the system DB
        pool_capacity = max_backend_connections - 1
        self._pg_pool = connpool.Pool(
            connect=self._pg_connect,
            disconnect=self._pg_disconnect,
            max_capacity=pool_capacity,
        )

        # DB state will be initialized in init().
        self._dbindex = None

        self._runstate_dir = runstate_dir
        self._internal_runstate_dir = internal_runstate_dir
        self._max_backend_connections = max_backend_connections
        self._compiler_pool_size = compiler_pool_size

        self._mgmt_port = None
        self._mgmt_host_addr = nethost
        self._mgmt_port_no = netport
        self._mgmt_protocol_max = max_protocol

        self._ports = []
        self._sys_conf_ports = {}
        self._sys_auth: Tuple[Any, ...] = tuple()

        # Shutdown the server after the last management
        # connection has disconnected
        self._auto_shutdown = auto_shutdown

        self._echo_runtime_info = echo_runtime_info

        self._startup_script = startup_script

        # Never use `self.__sys_pgcon` directly; get it via
        # `await self._acquire_sys_pgcon()`.
        self.__sys_pgcon = None
        self._sys_pgcon_waiters = None

        self._roles = immutables.Map()
        self._instance_data = immutables.Map()
        self._sys_queries = immutables.Map()

    async def _pg_connect(self, dbname):
        return await pgcon.connect(self._get_pgaddr(), dbname)

    async def _pg_disconnect(self, conn):
        conn.terminate()

    async def init(self):
        self.__sys_pgcon = await self._pg_connect(defines.EDGEDB_SYSTEM_DB)

        self._sys_pgcon_waiters = asyncio.Queue()
        self._sys_pgcon_waiters.put_nowait(self.__sys_pgcon)

        await self._load_instance_data()
        await self._fetch_roles()

        global_schema = await self.introspect_global_schema()
        self._dbindex = await dbview.DatabaseIndex.init(
            self, self._std_schema, global_schema)

        await self._introspect_dbs()

        # Now, once all DBs have been introspected, start listening on
        # any notifications about schema/roles/etc changes.
        await self.__sys_pgcon.set_server(self)

        self._compiler_pool = await compiler_pool.create_compiler_pool(
            pool_size=self._compiler_pool_size,
            dbindex=self._dbindex,
            runstate_dir=self._runstate_dir,
            backend_runtime_params=self.get_backend_runtime_params(),
            std_schema=self._std_schema,
            refl_schema=self._refl_schema,
            schema_class_layout=self._schema_class_layout,
        )

        self._populate_sys_auth()

        cfg = self._dbindex.get_sys_config()

        if not self._mgmt_host_addr:
            self._mgmt_host_addr = (
                config.lookup('listen_addresses', cfg) or 'localhost')

        if not self._mgmt_port_no:
            self._mgmt_port_no = (
                config.lookup('listen_port', cfg) or defines.EDGEDB_PORT)

        self._mgmt_port = self._new_port(
            mng_port.ManagementPort,
            nethost=self._mgmt_host_addr,
            netport=self._mgmt_port_no,
            auto_shutdown=self._auto_shutdown,
            max_protocol=self._mgmt_protocol_max,
            startup_script=self._startup_script,
        )

    def _populate_sys_auth(self):
        cfg = self._dbindex.get_sys_config()
        auth = config.lookup('auth', cfg) or ()
        self._sys_auth = tuple(sorted(auth, key=lambda a: a.priority))

    def _get_pgaddr(self):
        return self._cluster.get_connection_spec()

    def get_compiler_pool(self):
        return self._compiler_pool

    async def acquire_pgcon(self, dbname):
        return await self._pg_pool.acquire(dbname)

    def release_pgcon(self, dbname, conn, *, discard=False):
        if not conn.is_healthy_to_go_back_to_pool():
            # TODO: Add warning. This shouldn't happen.
            discard = True
        self._pg_pool.release(dbname, conn, discard=discard)

    async def introspect_global_schema(self):
        syscon = await self._acquire_sys_pgcon()
        try:
            json_data = await syscon.parse_execute_json(
                self._global_intro_query, b'__global_intro_db',
                dbver=0, use_prep_stmt=True, args=(),
            )
        finally:
            self._release_sys_pgcon()

        return s_refl.parse_into(
            base_schema=self._std_schema,
            schema=s_schema.FlatSchema(),
            data=json_data,
            schema_class_layout=self._schema_class_layout,
        )

    async def _reintrospect_global_schema(self):
        new_global_schema = await self.introspect_global_schema()
        self._dbindex.update_global_schema(new_global_schema)

    async def introspect_user_schema(self, conn):
        json_data = await conn.parse_execute_json(
            self._local_intro_query, b'__local_intro_db',
            dbver=0, use_prep_stmt=True, args=(),
        )

        return s_refl.parse_into(
            base_schema=self._std_schema,
            schema=s_schema.FlatSchema(),
            data=json_data,
            schema_class_layout=self._schema_class_layout,
        )

    async def introspect_db(self, dbname, *, refresh=False):
        conn = await self.acquire_pgcon(dbname)
        try:
            user_schema = await self.introspect_user_schema(conn)

            reflection_cache_json = await conn.parse_execute_json(
                b'''
                    SELECT json_agg(o.c)
                    FROM (
                        SELECT
                            json_build_object(
                                'eql_hash', t.eql_hash,
                                'argnames', array_to_json(t.argnames)
                            ) AS c
                        FROM
                            ROWS FROM(edgedb._get_cached_reflection())
                                AS t(eql_hash text, argnames text[])
                    ) AS o;
                ''',
                b'__reflection_cache',
                dbver=0,
                use_prep_stmt=True,
                args=(),
            )

            reflection_cache = immutables.Map({
                r['eql_hash']: tuple(r['argnames'])
                for r in json.loads(reflection_cache_json)
            })

            backend_ids_json = await conn.parse_execute_json(
                b'''
                SELECT
                    json_object_agg(
                        "id"::text,
                        "backend_id"
                    )::text
                FROM
                    edgedb."_SchemaScalarType"
                ''',
                b'__backend_ids_fetch',
                dbver=0,
                use_prep_stmt=True,
                args=(),
            )
            backend_ids = json.loads(backend_ids_json)

            self._dbindex.register_db(
                dbname, user_schema, reflection_cache, backend_ids,
                refresh=refresh)
        finally:
            self.release_pgcon(dbname, conn)

    async def _introspect_dbs(self):
        syscon = await self._acquire_sys_pgcon()
        try:
            dbs_query = self.get_sys_query('listdbs')
            json_data = await syscon.parse_execute_json(
                dbs_query, b'__listdbs',
                dbver=0, use_prep_stmt=True, args=(),
            )
            dbnames = json.loads(json_data)
        finally:
            self._release_sys_pgcon()

        async with taskgroup.TaskGroup(name='introspect DBs') as g:
            for dbname in dbnames:
                g.create_task(self.introspect_db(dbname))

    async def _fetch_roles(self):
        syscon = await self._acquire_sys_pgcon()
        try:
            role_query = self.get_sys_query('roles')
            json_data = await syscon.parse_execute_json(
                role_query, b'__sys_role',
                dbver=0, use_prep_stmt=True, args=(),
            )
            roles = json.loads(json_data)
            self._roles = immutables.Map([(r['name'], r) for r in roles])
        finally:
            self._release_sys_pgcon()

    async def _load_instance_data(self):
        syscon = await self._acquire_sys_pgcon()
        try:
            result = await syscon.simple_query(b'''\
                SELECT json FROM edgedbinstdata.instdata
                WHERE key = 'instancedata';
            ''', ignore_data=False)
            self._instance_data = immutables.Map(
                json.loads(result[0][0].decode('utf-8')))

            result = await syscon.simple_query(b'''\
                SELECT json FROM edgedbinstdata.instdata
                WHERE key = 'sysqueries';
            ''', ignore_data=False)
            queries = json.loads(result[0][0].decode('utf-8'))
            self._sys_queries = immutables.Map(
                {k: q.encode() for k, q in queries.items()})

            result = await syscon.simple_query(b'''\
                SELECT text FROM edgedbinstdata.instdata
                WHERE key = 'local_intro_query';
            ''', ignore_data=False)
            self._local_intro_query = result[0][0]

            result = await syscon.simple_query(b'''\
                SELECT text FROM edgedbinstdata.instdata
                WHERE key = 'global_intro_query';
            ''', ignore_data=False)
            self._global_intro_query = result[0][0]

            result = await syscon.simple_query(b'''\
                SELECT bin FROM edgedbinstdata.instdata
                WHERE key = 'stdschema';
            ''', ignore_data=False)
            try:
                data = binascii.a2b_hex(result[0][0][2:])
                self._std_schema = pickle.loads(data)
            except Exception as e:
                raise RuntimeError(
                    'could not load std schema pickle') from e

            result = await syscon.simple_query(b'''\
                SELECT bin FROM edgedbinstdata.instdata
                WHERE key = 'reflschema';
            ''', ignore_data=False)
            try:
                data = binascii.a2b_hex(result[0][0][2:])
                self._refl_schema = pickle.loads(data)
            except Exception as e:
                raise RuntimeError(
                    'could not load refl schema pickle') from e

            result = await syscon.simple_query(b'''\
                SELECT bin FROM edgedbinstdata.instdata
                WHERE key = 'classlayout';
            ''', ignore_data=False)
            try:
                data = binascii.a2b_hex(result[0][0][2:])
                self._schema_class_layout = pickle.loads(data)
            except Exception as e:
                raise RuntimeError(
                    'could not load schema class layout pickle') from e
        finally:
            self._release_sys_pgcon()

    def get_roles(self):
        return self._roles

    def _new_port(self, portcls, **kwargs):
        return portcls(
            server=self,
            loop=self._loop,
            pg_addr=self._pg_addr,
            runstate_dir=self._runstate_dir,
            internal_runstate_dir=self._internal_runstate_dir,
            dbindex=self._dbindex,
            **kwargs,
        )

    async def _restart_mgmt_port(self, nethost, netport):
        await self._mgmt_port.stop()

        try:
            new_mgmt_port = self._new_port(
                mng_port.ManagementPort,
                nethost=nethost,
                netport=netport,
                auto_shutdown=self._auto_shutdown,
                max_protocol=self._mgmt_protocol_max,
            )
        except Exception:
            await self._mgmt_port.start()
            raise

        try:
            await new_mgmt_port.start()
        except Exception:
            try:
                await new_mgmt_port.stop()
            except Exception:
                logging.exception('could not stop the new server')
                pass
            await self._mgmt_port.start()
            raise
        else:
            self._mgmt_host_addr = nethost
            self._mgmt_port_no = netport
            self._mgmt_port = new_mgmt_port

    async def _start_portconf(self, portconf: Any, *,
                              suppress_errors=False):
        if portconf in self._sys_conf_ports:
            logging.info('port for config %r has been already started',
                         portconf)
            return

        port_cls: Type[http.BaseHttpPort]
        if portconf.protocol == 'graphql+http':
            port_cls = http_graphql_port.HttpGraphQLPort
        elif portconf.protocol == 'edgeql+http':
            port_cls = http_edgeql_port.HttpEdgeQLPort
        elif portconf.protocol == 'notebook':
            port_cls = notebook_port.NotebookPort
        else:
            raise errors.InvalidReferenceError(
                f'unknown protocol {portconf.protocol!r}')

        port = self._new_port(
            port_cls,
            netport=portconf.port,
            nethost=portconf.address,
            database=portconf.database,
            user=portconf.user,
            protocol=portconf.protocol,
            concurrency=portconf.concurrency)

        try:
            await port.start()
        except Exception as ex:
            await port.stop()
            if suppress_errors:
                logging.error(
                    'failed to start port for config: %r', portconf,
                    exc_info=True)
            else:
                raise ex
        else:
            logging.info('started port for config: %r', portconf)

        self._sys_conf_ports[portconf] = port
        return port

    async def _stop_portconf(self, portconf):
        if portconf not in self._sys_conf_ports:
            logging.warning('no port to stop for config: %r', portconf)
            return

        try:
            port = self._sys_conf_ports.pop(portconf)
            await port.stop()
        except Exception:
            logging.error(
                'failed to stop port for config: %r', portconf,
                exc_info=True)
        else:
            logging.info('stopped port for config: %r', portconf)

    async def _on_drop_db(self, dbname: str, current_dbname: str) -> None:
        assert self._dbindex is not None

        if current_dbname == dbname:
            raise errors.ExecutionError(
                f'cannot drop the currently open database {dbname!r}')

        if self._dbindex.count_connections(dbname):
            # If there are open EdgeDB connections to the `dbname` DB
            # just raise the error Postgres would have raised itself.
            raise errors.ExecutionError(
                f'database {dbname!r} is being accessed by other users')
        else:
            # If, however, there are no open EdgeDB connections, prune
            # all non-active postgres connection to the `dbname` DB.
            await self._pg_pool.prune_inactive_connections(dbname)

    async def _on_system_config_add(self, setting_name, value):
        # CONFIGURE SYSTEM INSERT ConfigObject;
        if setting_name == 'ports':
            await self._start_portconf(value)

    async def _on_system_config_rem(self, setting_name, value):
        # CONFIGURE SYSTEM RESET ConfigObject;
        if setting_name == 'ports':
            await self._stop_portconf(value)

    async def _on_system_config_set(self, setting_name, value):
        # CONFIGURE SYSTEM SET setting_name := value;
        if setting_name == 'listen_addresses':
            await self._restart_mgmt_port(value, self._mgmt_port_no)

        elif setting_name == 'listen_port':
            await self._restart_mgmt_port(self._mgmt_host_addr, value)

    async def _on_system_config_reset(self, setting_name):
        # CONFIGURE SYSTEM RESET setting_name;
        if setting_name == 'listen_addresses':
            await self._restart_mgmt_port(
                'localhost', self._mgmt_port_no)

        elif setting_name == 'listen_port':
            await self._restart_mgmt_port(
                self._mgmt_host_addr, defines.EDGEDB_PORT)

    async def _after_system_config_add(self, setting_name, value):
        # CONFIGURE SYSTEM INSERT ConfigObject;
        if setting_name == 'auth':
            self._populate_sys_auth()

    async def _after_system_config_rem(self, setting_name, value):
        # CONFIGURE SYSTEM RESET ConfigObject;
        if setting_name == 'auth':
            self._populate_sys_auth()

    async def _after_system_config_set(self, setting_name, value):
        # CONFIGURE SYSTEM SET setting_name := value;
        pass

    async def _after_system_config_reset(self, setting_name):
        # CONFIGURE SYSTEM RESET setting_name;
        pass

    async def _acquire_sys_pgcon(self):
        if self._sys_pgcon_waiters is None:
            raise RuntimeError('invalid request to acquire a system pgcon')
        return await self._sys_pgcon_waiters.get()

    def _release_sys_pgcon(self):
        self._sys_pgcon_waiters.put_nowait(self.__sys_pgcon)

    async def _signal_sysevent(self, event, **kwargs):
        pgcon = await self._acquire_sys_pgcon()
        try:
            await pgcon.signal_sysevent(event, **kwargs)
        finally:
            self._release_sys_pgcon()

    def _on_remote_ddl(self, dbname):
        # Triggered by a postgres notification event 'schema-changes'
        # on the __edgedb_sysevent__ channel
        self._loop.create_task(
            self.introspect_db(dbname, refresh=True)
        )

    def _on_remote_database_config_change(self, dbname):
        # Triggered by a postgres notification event 'database-config-changes'
        # on the __edgedb_sysevent__ channel
        pass

    def _on_remote_system_config_change(self):
        # Triggered by a postgres notification event 'ystem-config-changes'
        # on the __edgedb_sysevent__ channel
        pass

    def _on_role_change(self):
        self._loop.create_task(self._fetch_roles())

    def _on_global_schema_change(self):
        self._loop.create_task(self._reintrospect_global_schema())

    def add_port(self, portcls, **kwargs):
        if self._serving:
            raise RuntimeError(
                'cannot add new ports after start() call')

        port = self._new_port(portcls, **kwargs)
        self._ports.append(port)
        return port

    async def run_startup_script_and_exit(self):
        """Run the script specified in *startup_script* and exit immediately"""
        if self._startup_script is None:
            raise AssertionError('startup script is not defined')

        ql_parser.preload()
        await self._mgmt_port.run_startup_script_and_exit()
        return

    async def start(self):
        # Make sure that EdgeQL parser is preloaded; edgecon might use
        # it to restore config values.
        ql_parser.preload()

        async with taskgroup.TaskGroup() as g:
            g.create_task(self._mgmt_port.start())
            for port in self._ports:
                g.create_task(port.start())

        sys_config = self._dbindex.get_sys_config()
        ports = config.lookup('ports', sys_config)
        if ports:
            for portconf in ports:
                await self._start_portconf(portconf, suppress_errors=True)

        self._serving = True

        if self._echo_runtime_info:
            ri = {
                "port": self._mgmt_port_no,
                "runstate_dir": str(self._runstate_dir),
            }
            print(f'\nEDGEDB_SERVER_DATA:{json.dumps(ri)}\n', flush=True)

    async def stop(self):
        try:
            self._serving = False

            async with taskgroup.TaskGroup() as g:
                for port in self._ports:
                    g.create_task(port.stop())
                self._ports.clear()
                for port in self._sys_conf_ports.values():
                    g.create_task(port.stop())
                self._sys_conf_ports.clear()
                g.create_task(self._mgmt_port.stop())
                self._mgmt_port = None
        finally:
            pgcon = await self._acquire_sys_pgcon()
            self._sys_pgcon_waiters = None
            self.__sys_pgcon = None
            pgcon.terminate()

    async def get_auth_method(self, user):
        authlist = self._sys_auth

        if not authlist:
            default_method = 'SCRAM'
            return config.get_settings().get_type_by_name(default_method)()
        else:
            for auth in authlist:
                match = (
                    (user in auth.user or '*' in auth.user)
                )

                if match:
                    return auth.method

    def get_sys_query(self, key):
        return self._sys_queries[key]

    def get_instance_data(self, key):
        return self._instance_data[key]

    def get_backend_runtime_params(self) -> Any:
        return self._cluster.get_runtime_params()
