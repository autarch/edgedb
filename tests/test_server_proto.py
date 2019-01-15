#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2019-present MagicStack Inc. and the EdgeDB authors.
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

import asyncio

import edgedb

from edb.common import taskgroup as tg
from edb.testbase import server as tb


class TestServerProto(tb.NonIsolatedDDLTestCase):

    async def test_server_proto_parse_error_recover_01(self):
        for _ in range(2):
            with self.assertRaises(edgedb.EdgeQLSyntaxError):
                await self.con.fetch('select syntax error')

            with self.assertRaises(edgedb.EdgeQLSyntaxError):
                await self.con.fetch('select syntax error')

            for _ in range(10):
                self.assertEqual(
                    await self.con.fetch('select 1;'),
                    edgedb.Set((1,)))

    async def test_server_proto_parse_error_recover_02(self):
        for _ in range(2):
            with self.assertRaises(edgedb.EdgeQLSyntaxError):
                await self.con.execute('select syntax error')

            with self.assertRaises(edgedb.EdgeQLSyntaxError):
                await self.con.execute('select syntax error')

            for _ in range(10):
                await self.con.execute('select 1; select 2;'),

    async def test_server_proto_exec_error_recover_01(self):
        for _ in range(2):
            with self.assertRaises(edgedb.DivisionByZeroError):
                await self.con.fetch('select 1 / 0;')

            with self.assertRaises(edgedb.DivisionByZeroError):
                await self.con.fetch('select 1 / 0;')

            for _ in range(10):
                self.assertEqual(
                    await self.con.fetch('select 1;'),
                    edgedb.Set((1,)))

    async def test_server_proto_exec_error_recover_02(self):
        for _ in range(2):
            with self.assertRaises(edgedb.DivisionByZeroError):
                await self.con.execute('select 1 / 0;')

            with self.assertRaises(edgedb.DivisionByZeroError):
                await self.con.execute('select 1 / 0;')

            for _ in range(10):
                await self.con.execute('select 1;')

    async def test_server_proto_exec_error_recover_03(self):
        query = 'select 10 // <int64>$0;'
        for i in [1, 2, 0, 3, 1, 0, 1]:
            if i:
                self.assertEqual(
                    await self.con.fetch(query, i),
                    edgedb.Set([10 // i]))
            else:
                with self.assertRaises(edgedb.DivisionByZeroError):
                    await self.con.fetch(query, i)

    async def test_server_proto_exec_error_recover_04(self):
        for i in [1, 2, 0, 3, 1, 0, 1]:
            if i:
                await self.con.execute(f'select 10 // {i};')
            else:
                with self.assertRaises(edgedb.DivisionByZeroError):
                    await self.con.fetch(f'select 10 // {i};')

    async def test_server_proto_basic_datatypes_01(self):
        for _ in range(10):
            self.assertEqual(
                await self.con.fetch(
                    'select ()'),
                edgedb.Set([()]))

            self.assertEqual(
                await self.con.fetch(
                    'select (1,)'),
                edgedb.Set([(1,)]))

            self.assertEqual(
                await self.con.fetch(
                    'select <array<int64>>[]'),
                edgedb.Set([[]]))

            self.assertEqual(
                await self.con.fetch(
                    'select ["a", "b"]'),
                edgedb.Set([["a", "b"]]))

            self.assertEqual(
                await self.con.fetch('''
                    SELECT {(a := 1 + 1 + 40, world := ("hello", 32)),
                            (a:=1, world := ("yo", 10))};
                '''),
                edgedb.Set([
                    edgedb.NamedTuple(a=42, world=("hello", 32)),
                    edgedb.NamedTuple(a=1, world=("yo", 10)),
                ]))

    async def test_server_proto_basic_datatypes_02(self):
        self.assertEqual(
            await self.con.fetch(
                r'''select [b"\x00a", b"b", b'', b'\na']'''),
            edgedb.Set([[b"\x00a", b"b", b'', b'\na']]))

        self.assertEqual(
            await self.con.fetch(
                r'select <bytes>$0', b'he\x00llo'),
            edgedb.Set([b'he\x00llo']))

    async def test_server_proto_args_01(self):
        self.assertEqual(
            await self.con.fetch(
                'select (<array<str>>$foo)[0] ++ (<array<str>>$bar)[0];',
                foo=['aaa'], bar=['bbb']),
            edgedb.Set(('aaabbb',)))

    async def test_server_proto_args_02(self):
        self.assertEqual(
            await self.con.fetch(
                'select (<array<str>>$0)[0] ++ (<array<str>>$1)[0];',
                ['aaa'], ['bbb']),
            edgedb.Set(('aaabbb',)))

    async def test_server_proto_json_cast_01(self):
        self.assertEqual(
            await self.con.fetch('''
                select <json>(
                    select schema::Type{name} filter .name = 'std::bool'
                )
            '''),
            edgedb.Set(('{"name": "std::bool"}',))
        )

    async def test_server_proto_wait_cancel_01(self):
        # Test that client protocol handles waits interrupted
        # by closing.
        lock_key = tb.gen_lock_key()

        con2 = await self.cluster.connect(user='edgedb',
                                          database=self.con.dbname)

        await self.con.fetch('select sys::advisory_lock(<int64>$0)', lock_key)

        try:
            async with tg.TaskGroup() as g:

                async def exec_to_fail():
                    with self.assertRaises(ConnectionAbortedError):
                        await con2.fetch(
                            'select sys::advisory_lock(<int64>$0)', lock_key)

                g.create_task(exec_to_fail())

                await asyncio.sleep(0.1)
                await con2.close()

        finally:
            self.assertEqual(
                await self.con.fetch(
                    'select sys::advisory_unlock(<int64>$0)', lock_key),
                [True])

    async def test_server_proto_query_cache_invalidate_01(self):
        typename = 'CacheInv_01'

        con1 = self.con
        con2 = await self.cluster.connect(user='edgedb', database=con1.dbname)
        try:
            await con2.execute(f'''
                CREATE TYPE test::{typename} {{
                    CREATE REQUIRED PROPERTY prop1 -> std::str;
                }};

                INSERT test::{typename} {{
                    prop1 := 'aaa'
                }}
            ''')

            query = f'SELECT test::{typename}.prop1'

            for _ in range(5):
                self.assertEqual(
                    await con1.fetch(query),
                    edgedb.Set(['aaa']))

            await con2.execute(f'''
                DELETE (SELECT test::{typename});

                ALTER TYPE test::{typename} {{
                    DROP PROPERTY prop1;
                }};

                ALTER TYPE test::{typename} {{
                    # DROP PROPERTY prop1;
                    CREATE REQUIRED PROPERTY prop1 -> std::int64;
                }};

                INSERT test::{typename} {{
                    prop1 := 123
                }};
            ''')

            for _ in range(5):
                self.assertEqual(
                    await con1.fetch(query),
                    edgedb.Set([123]))

        finally:
            await con2.close()

    async def test_server_proto_query_cache_invalidate_02(self):
        typename = 'CacheInv_02'

        con1 = self.con
        con2 = await self.cluster.connect(user='edgedb', database=con1.dbname)
        try:
            await con2.fetch(f'''
                CREATE TYPE test::{typename} {{
                    CREATE REQUIRED PROPERTY prop1 -> std::str;
                }};
            ''')

            await con2.fetch(f'''
                INSERT test::{typename} {{
                    prop1 := 'aaa'
                }}
            ''')

            query = f'SELECT test::{typename}.prop1'

            for _ in range(5):
                self.assertEqual(
                    await con1.fetch(query),
                    edgedb.Set(['aaa']))

            await con2.fetch(f'''
                DELETE (SELECT test::{typename});
            ''')

            await con2.fetch(f'''
                ALTER TYPE test::{typename} {{
                    DROP PROPERTY prop1;
                }};
            ''')

            await con2.fetch(f'''
                ALTER TYPE test::{typename} {{
                    CREATE REQUIRED PROPERTY prop1 -> std::int64;
                }};
            ''')

            await con2.fetch(f'''
                INSERT test::{typename} {{
                    prop1 := 123
                }};
            ''')

            for _ in range(5):
                self.assertEqual(
                    await con1.fetch(query),
                    edgedb.Set([123]))

        finally:
            await con2.close()
