# Miro - an RSS based video player application
# Copyright (C) 2012
# Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

"""miro.data.connectionpool -- SQLite connection pool """
import contextlib
import logging

import sqlite3

from miro import messages
from miro.data import dbcollations

class ConnectionLimitError(StandardError):
    """We've hit our connection limits."""

class ConnectionPool(object):
    """Pool of SQLite database connections

    :attribute wal_mode: Is the database using WAL mode for its journal?
    """
    def __init__(self, db_path, min_connections=2, max_connections=4):
        """Create a new ConnectionPool

        :param db_path: path to the database to connect to
        :param min_connections: Minimum number of connections to maintain
        :param max_connections: Maximum number of connections to the database
        """
        self.db_path = db_path
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.all_connections = set()
        self.free_connections = []
        self._check_wal_mode()

    def _check_wal_mode(self):
        """Try to set journal_mode=wall and return if it was successful
        """
        connection = self.get_connection()
        cursor = connection.execute("PRAGMA journal_mode=wal");
        self.wal_mode = cursor.fetchone()[0] == u'wal'
        self.release_connection(connection)

    def _make_new_connection(self):
        # TODO: should have error handling here, but what should we do?
        connection = sqlite3.connect(self.db_path,
                                     isolation_level=None,
                                     detect_types=sqlite3.PARSE_DECLTYPES)
        dbcollations.setup_collations(connection)
        self.free_connections.append(connection)
        self.all_connections.add(connection)

    def get_connection(self):
        """Get a new connection to the database

        When you're finished with the connection, call release_connection() to
        put it back into the pool.

        If there are max_connections checked out and get_connection() is
        called again, ConnectionLimitError will be raised.

        :returns sqlite3.Connection object
        """
        if not self.free_connections:
            if len(self.all_connections) < self.max_connections:
                self._make_new_connection()
            else:
                raise ConnectionLimitError()
        return self.free_connections.pop()

    def release_connection(self, connection):
        """Put a connection back into the pool."""

        if connection not in self.all_connections:
            raise ValueError("%s not from this pool" % connection)
        if len(self.all_connections) > self.min_connections:
            connection.close()
            self.all_connections.remove(connection)
        else:
            self.free_connections.append(connection)

    @contextlib.contextmanager
    def context(self):
        """ContextManager used to get a connection.

        Usage:
            with connection_pool.context() as connection:
                cursor = connection.cursor()
                cursor.execute("blah blah blah")
        """
        connection = self.get_connection()
        yield connection
        # Rollback any changes not committed
        connection.rollback()
        self.release_connection(connection)

class DeviceConnectionPool(ConnectionPool):
    """ConnectionPool for a device."""
    def __init__(self, device_info):
        # min_connections is 0 since we should normally not have any
        # connections to the device database.  The max connections is 2 in
        # case the user is on the video tab and is playing items from the
        # audio tab (or vice-versa)
        ConnectionPool.__init__(self, device_info.sqlite_path,
                                min_connections=0, max_connections=2)

class DeviceConnectionPoolMap(object):
    """Manage a ConnectionPool for each connected device.
    """
    def __init__(self):
        self.pool_map = {}

    def reset(self):
        self.pool_map = {}

    def get_pool(self, device_id):
        return self.pool_map[device_id]

    def _ensure_connection_pool(self, device_info):
        if device_info.id not in self.pool_map:
            self.pool_map[device_info.id] = DeviceConnectionPool(device_info)

    def _ensure_no_connection_pool(self, device_id):
        if device_id in self.pool_map:
            del self.pool_map[device_id]

    def on_tabs_changed(self, message):
        if message.type != 'connect':
            return
        for info in message.added + message.changed:
            if isinstance(info, messages.DeviceInfo):
                if info.db_info is not None:
                    self._ensure_connection_pool(info)
                else:
                    self._ensure_no_connection_pool(info.id)
        for id_ in message.removed:
            self._ensure_no_connection_pool(id_)
