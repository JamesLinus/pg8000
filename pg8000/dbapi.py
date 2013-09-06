# vim: sw=4:expandtab:foldmethod=marker
#
# Copyright (c) 2007-2009, Mathieu Fenniak
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# * Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
# * The name of the author may not be used to endorse or promote products
# derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

__author__ = "Mathieu Fenniak"

import datetime
from datetime import timedelta
import time
from pg8000.types import (
    Interval, min_int2, max_int2, min_int4, max_int4, min_int8, max_int8)
from pg8000.errors import (
    NotSupportedError, ProgrammingError, InternalError, IntegrityError,
    OperationalError, DatabaseError, InterfaceError, Error,
    ConnectionClosedError, CopyQueryOrTableRequiredError, CursorClosedError,
    QueryParameterParseError, QueryParameterIndexError,
    ArrayContentNotHomogenousError, ArrayContentEmptyError,
    ArrayDimensionsNotConsistentError, ArrayContentNotSupportedError,
    Warning, CopyQueryWithoutStreamError)
from warnings import warn
import socket
import ssl as sslmodule
import threading
from struct import unpack_from, pack, Struct
import hashlib
from decimal import Decimal
import pg8000
import pg8000.util
from pg8000 import i_unpack, ii_unpack, iii_unpack, hhhh_pack, h_pack, \
    hhhh_unpack, d_unpack, q_unpack, d_pack, f_unpack, q_pack, i_pack, \
    h_unpack, dii_unpack, qii_unpack, ci_unpack, bh_unpack, \
    ihihih_unpack, cccc_unpack, ii_pack, iii_pack, dii_pack, qii_pack

##
# The DBAPI level supported.  Currently 2.0.  This property is part of the
# DBAPI 2.0 specification.
apilevel = "2.0"

##
# Integer constant stating the level of thread safety the DBAPI interface
# supports.  This DBAPI interface supports sharing of the module and
# connections.  This property is part of the DBAPI 2.0 specification.
threadsafety = 3

##
# String property stating the type of parameter marker formatting expected by
# the interface.  This value defaults to "format".  This property is part of
# the DBAPI 2.0 specification.
# <p>
# Unlike the DBAPI specification, this value is not constant.  It can be
# changed to any standard paramstyle value (ie. qmark, numeric, named, format,
# and pyformat).
paramstyle = 'format'  # paramstyle can be changed to any DB-API paramstyle

# I have no idea what this would be used for by a client app.  Should it be
# TEXT, VARCHAR, CHAR?  It will only compare against row_description's
# type_code if it is this one type.  It is the varchar type oid for now, this
# appears to match expectations in the DB API 2.0 compliance test suite.

STRING = 1043

BINARY = pg8000.types.Bytea

# numeric type_oid
NUMBER = 1700

# timestamp type_oid
DATETIME = 1114

# oid type_oid
ROWID = 26


def Date(year, month, day):
    return datetime.date(year, month, day)


def Time(hour, minute, second):
    return datetime.time(hour, minute, second)


def Timestamp(year, month, day, hour, minute, second):
    return datetime.datetime(year, month, day, hour, minute, second)


def DateFromTicks(ticks):
    return Date(*time.localtime(ticks)[:3])


def TimeFromTicks(ticks):
    return Time(*time.localtime(ticks)[3:6])


def TimestampFromTicks(ticks):
    return Timestamp(*time.localtime(ticks)[:6])


##
# Construct an object holding binary data.
def Binary(value):
    return pg8000.types.Bytea(value)

statement_number_lock = threading.Lock()
statement_number = 0

portal_number_lock = threading.Lock()
portal_number = 0

FC_TEXT = 0
FC_BINARY = 1


def convert_paramstyle(src_style, query, args):
    # I don't see any way to avoid scanning the query string char by char,
    # so we might as well take that careful approach and create a
    # state-based scanner.  We'll use int variables for the state.
    #  0 -- outside quoted string
    #  1 -- inside single-quote string '...'
    #  2 -- inside quoted identifier   "..."
    #  3 -- inside escaped single-quote string, E'...'
    state = 0
    output_query = ""
    output_args = []
    if src_style == "numeric":
        output_args = args
    elif src_style in ("pyformat", "named"):
        mapping_to_idx = {}
    i = 0
    while 1:
        if i == len(query):
            break
        c = query[i]
        # print "begin loop", repr(i), repr(c), repr(state)
        if state == 0:
            if c == "'":
                i += 1
                output_query += c
                state = 1
            elif c == '"':
                i += 1
                output_query += c
                state = 2
            elif c == 'E':
                # check for escaped single-quote string
                i += 1
                if i < len(query) and i > 1 and query[i] == "'":
                    i += 1
                    output_query += "E'"
                    state = 3
                else:
                    output_query += c
            elif src_style == "qmark" and c == "?":
                i += 1
                param_idx = len(output_args)
                if param_idx == len(args):
                    raise QueryParameterIndexError(
                        "too many parameter fields, not enough parameters")
                output_args.append(args[param_idx])
                output_query += "$" + str(param_idx + 1)
            elif src_style == "numeric" and c == ":":
                i += 1
                if i < len(query) and i > 1 and query[i].isdigit():
                    output_query += "$" + query[i]
                    i += 1
                else:
                    raise QueryParameterParseError(
                        "numeric parameter : does not have numeric arg")
            elif src_style == "named" and c == ":":
                name = ""
                while 1:
                    i += 1
                    if i == len(query):
                        break
                    c = query[i]
                    if c.isalnum() or c == '_':
                        name += c
                    else:
                        break
                if name == "":
                    raise QueryParameterParseError(
                        "empty name of named parameter")
                idx = mapping_to_idx.get(name)
                if idx is None:
                    idx = len(output_args)
                    output_args.append(args[name])
                    idx += 1
                    mapping_to_idx[name] = idx
                output_query += "$" + str(idx)
            elif src_style == "format" and c == "%":
                i += 1
                if i < len(query) and i > 1:
                    if query[i] == "s":
                        param_idx = len(output_args)
                        if param_idx == len(args):
                            raise QueryParameterIndexError(
                                "too many parameter fields, not enough "
                                "parameters")
                        output_args.append(args[param_idx])
                        output_query += "$" + str(param_idx + 1)
                    elif query[i] == "%":
                        output_query += "%"
                    else:
                        raise QueryParameterParseError(
                            "Only %s and %% are supported")
                    i += 1
                else:
                    raise QueryParameterParseError(
                        "format parameter % does not have format code")
            elif src_style == "pyformat" and c == "%":
                i += 1
                if i < len(query) and i > 1:
                    if query[i] == "(":
                        i += 1
                        # begin mapping name
                        end_idx = query.find(')', i)
                        if end_idx == -1:
                            raise QueryParameterParseError(
                                "began pyformat dict read, but couldn't find "
                                "end of name")
                        else:
                            name = query[i:end_idx]
                            i = end_idx + 1
                            if i < len(query) and query[i] == "s":
                                i += 1
                                idx = mapping_to_idx.get(name)
                                if idx is None:
                                    idx = len(output_args)
                                    output_args.append(args[name])
                                    idx += 1
                                    mapping_to_idx[name] = idx
                                output_query += "$" + str(idx)
                            else:
                                raise QueryParameterParseError(
                                    "format not specified or not supported "
                                    "(only %(...)s supported)")
                    elif query[i] == "%":
                        output_query += "%"
                    elif query[i] == "s":
                        # we have a %s in a pyformat query string.  Assume
                        # support for format instead.
                        i -= 1
                        src_style = "format"
                    else:
                        raise QueryParameterParseError(
                            "Only %(name)s, %s and %% are supported")
            else:
                i += 1
                output_query += c
        elif state == 1:
            output_query += c
            i += 1
            if c == "'":
                # Could be a double ''
                if i < len(query) and query[i] == "'":
                    # is a double quote.
                    output_query += query[i]
                    i += 1
                else:
                    state = 0
            elif src_style in ("pyformat", "format") and c == "%":
                # hm... we're only going to support an escaped percent sign
                if i < len(query):
                    if query[i] == "%":
                        # good.  We already output the first percent sign.
                        i += 1
                    else:
                        raise QueryParameterParseError(
                            "'%" + query[i] +
                            "' not supported in quoted string")
        elif state == 2:
            output_query += c
            i += 1
            if c == '"':
                state = 0
            elif src_style in ("pyformat", "format") and c == "%":
                # hm... we're only going to support an escaped percent sign
                if i < len(query):
                    if query[i] == "%":
                        # good.  We already output the first percent sign.
                        i += 1
                    else:
                        raise QueryParameterParseError(
                            "'%" + query[i] +
                            "' not supported in quoted string")
        elif state == 3:
            output_query += c
            i += 1
            if c == "\\":
                # check for escaped single-quote
                if i < len(query) and query[i] == "'":
                    output_query += "'"
                    i += 1
            elif c == "'":
                state = 0
            elif src_style in ("pyformat", "format") and c == "%":
                # hm... we're only going to support an escaped percent sign
                if i < len(query):
                    if query[i] == "%":
                        # good.  We already output the first percent sign.
                        i += 1
                    else:
                        raise QueryParameterParseError(
                            "'%" + query[i] +
                            "' not supported in quoted string")

    return output_query, tuple(output_args)


def require_open_cursor(fn):
    def _fn(self, *args, **kwargs):
        if self._conn is None:
            raise CursorClosedError()
        return fn(self, *args, **kwargs)
    return _fn


def unexpected_response(message_code):
    return InternalError("Unexpected response msg {0}".format(message_code))


##
# The class of object returned by the {@link #ConnectionWrapper.cursor cursor
# method}.
# The Cursor class allows multiple queries to be performed concurrently with a
# single PostgreSQL connection.  The Cursor object is implemented internally by
# using a {@link PreparedStatement PreparedStatement} object, so if you plan to
# use a statement multiple times, you might as well create a PreparedStatement
# and save a small amount of reparsing time.
# <p>
# As of v1.01, instances of this class are thread-safe.  See {@link
# PreparedStatement PreparedStatement} for more information.
# <p>
# Stability: Added in v1.00, stability guaranteed for v1.xx.
#
# @param connection     An instance of {@link Connection Connection}.
class Cursor(object):
    def __init__(self, connection):
        self._conn = connection
        self._stmt = None
        self.arraysize = 1
        self._row_count = -1

    def require_stmt(func):
        def retval(self, *args, **kwargs):
            if self._stmt is None:
                raise ProgrammingError("attempting to use unexecuted cursor")
            return func(self, *args, **kwargs)
        return retval

    ##
    # Return a count of the number of rows currently being read.
    # <p>
    # Stability: Added in v1.03, stability guaranteed for v1.xx.
    @property
    @require_stmt
    def row_count(self):
        return self._stmt.row_count

    ##
    # Read a row from the database server, and return it in a dictionary
    # indexed by column name/alias.  This method will raise an error if two
    # columns have the same name.  Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    @require_stmt
    def read_dict(self):
        return self._stmt.read_dict()

    ##
    # Read a row from the database server, and return it as a tuple of values.
    # Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    @require_stmt
    def read_tuple(self):
        return self._stmt.read_tuple()

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a tuple for each row, in the same manner as {@link
    # #PreparedStatement.read_tuple read_tuple}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    @require_stmt
    def iterate_tuple(self):
        return self._stmt.iterate_tuple()

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a dict for each row, in the same manner as {@link
    # #PreparedStatement.read_dict read_dict}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    @require_stmt
    def iterate_dict(self):
        return self._stmt.iterate_dict()

    ##
    # This read-only attribute returns a reference to the connection object on
    # which the cursor was created.
    # <p>
    # Stability: Part of a DBAPI 2.0 extension.  A warning "DB-API extension
    # cursor.connection used" will be fired.
    @property
    def connection(self):
        warn("DB-API extension cursor.connection used", stacklevel=3)
        return self._conn

    ##
    # This read-only attribute specifies the number of rows that the last
    # .execute*() produced (for DQL statements like 'select') or affected (for
    # DML statements like 'update' or 'insert').
    # <p>
    # The attribute is -1 in case no .execute*() has been performed on the
    # cursor or the rowcount of the last operation is cannot be determined by
    # the interface.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @property
    def rowcount(self):
        return self._row_count

    ##
    # This read-only attribute is a sequence of 7-item sequences.  Each value
    # contains information describing one result column.  The 7 items returned
    # for each column are (name, type_code, display_size, internal_size,
    # precision, scale, null_ok).  Only the first two values are provided by
    # this interface implementation.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    description = property(lambda self: self._getDescription())

    @require_open_cursor
    def _getDescription(self):
        if self._stmt is None:
            return None
        row_desc = self._stmt.get_row_description()
        if row_desc is None or len(row_desc) == 0:
            return None
        columns = []
        for col in row_desc:
            columns.append(
                (col["name"], col["type_oid"], None, None, None, None, None))
        return columns

    ##
    # Executes a database operation.  Parameters may be provided as a sequence
    # or mapping and will be bound to variables in the operation.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @require_open_cursor
    def execute(self, operation, args=(), stream=None):
        self._row_count = -1
        self._conn.begin()
        self._execute(operation, args, stream=stream)
        self._row_count = self._stmt.row_count

    def _execute(self, operation, args, stream=None):
        new_query, new_args = convert_paramstyle(
            paramstyle, operation, args)
        if self._conn._state == 'closed':
            raise ConnectionClosedError()

        with self._conn._unnamed_prepared_statement_lock:
            self._stmt = PreparedStatement(
                self._conn, new_query, statement_name="", *new_args,
                stream=stream)
            self._stmt.execute(*new_args, stream=stream)

    def copy_from(self, fileobj, table=None, sep='\t', null=None, query=None):
        if query is None:
            if table is None:
                raise CopyQueryOrTableRequiredError()
            query = "COPY %s FROM stdout DELIMITER '%s'" % (table, sep)
            if null is not None:
                query += " NULL '%s'" % (null,)
        self.copy_execute(fileobj, query)

    def copy_to(self, fileobj, table=None, sep='\t', null=None, query=None):
        if query is None:
            if table is None:
                raise CopyQueryOrTableRequiredError()
            query = "COPY %s TO stdout DELIMITER '%s'" % (table, sep)
            if null is not None:
                query += " NULL '%s'" % (null,)
        self.copy_execute(fileobj, query)

    @require_open_cursor
    def copy_execute(self, fileobj, query):
        self.execute(query, stream=fileobj)

    ##
    # Prepare a database operation and then execute it against all parameter
    # sequences or mappings provided.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @require_open_cursor
    def executemany(self, operation, parameter_sets):
        self._row_count = -1
        self._conn.begin()
        for parameters in parameter_sets:
            self._execute(operation, parameters)
            if self.row_count == -1:
                self._row_count = -1
            elif self._row_count == -1:
                self._row_count = self.row_count
            else:
                self._row_count += self.row_count

    ##
    # Fetch the next row of a query result set, returning a single sequence, or
    # None when no more data is available.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    def fetchone(self):
        try:
            return self._stmt.read_tuple()
        except AttributeError:
            raise ProgrammingError("attempting to use unexecuted cursor")


    ##
    # Fetch the next set of rows of a query result, returning a sequence of
    # sequences.  An empty sequence is returned when no more rows are
    # available.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    # @param size   The number of rows to fetch when called.  If not provided,
    #               the arraysize property value is used instead.
    def fetchmany(self, size=None):
        if size is None:
            size = self.arraysize
        rows = []
        for i in range(size):
            value = self.fetchone()
            if value is None:
                break
            rows.append(value)
        return rows

    ##
    # Fetch all remaining rows of a query result, returning them as a sequence
    # of sequences.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @require_open_cursor
    def fetchall(self):
        return tuple(self.iterate_tuple())

    ##
    # Close the cursor.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @require_open_cursor
    def close(self):
        if self._stmt is not None:
            self._stmt.close()
            self._stmt = None
        self._conn = None

    def __next__(self):
        warn("DB-API extension cursor.next() used", stacklevel=2)
        try:
            retval = self._stmt.read_tuple()
        except AttributeError:
            raise ProgrammingError("attempting to use unexecuted cursor")
        if retval is None:
            raise StopIteration()
        return retval

    def __iter__(self):
        warn("DB-API extension cursor.__iter__() used", stacklevel=2)
        return self

    def setinputsizes(self, sizes):
        pass

    def setoutputsize(self, size, column=None):
        pass


def require_open_connection(fn):
    def _fn(self, *args, **kwargs):
        if self._state == 'closed':
            raise ConnectionClosedError()
        return fn(self, *args, **kwargs)
    return _fn

# Message codes
NOTICE_RESPONSE = b"N"
AUTHENTICATION_REQUEST = b"R"
PARAMETER_STATUS = b"S"
BACKEND_KEY_DATA = b"K"
READY_FOR_QUERY = b"Z"
ROW_DESCRIPTION = b"T"
ERROR_RESPONSE = b"E"
DATA_ROW = b"D"
COMMAND_COMPLETE = b"C"
PARSE_COMPLETE = b"1"
BIND_COMPLETE = b"2"
CLOSE_COMPLETE = b"3"
PORTAL_SUSPENDED = b"s"
NO_DATA = b"n"
PARAMETER_DESCRIPTION = b"t"
NOTIFICATION_RESPONSE = b"A"
COPY_DONE = b"c"
COPY_DATA = b"d"
COPY_IN_RESPONSE = b"G"
COPY_OUT_RESPONSE = b"H"

BIND = b"B"
PARSE = b"P"
EXECUTE = b"E"
FLUSH = b'H'
SYNC = b'S'
PASSWORD = b'p'
DESCRIBE = b'D'
TERMINATE = b'X'
CLOSE = b'C'

# ErrorResponse codes
RESPONSE_SEVERITY = b"S"  # always present
RESPONSE_CODE = b"C"  # always present
RESPONSE_MSG = b"M"  # always present
RESPONSE_DETAIL = b"D"
RESPONSE_HINT = b"H"
RESPONSE_POSITION = b"P"
RESPONSE__POSITION = b"p"
RESPONSE__QUERY = b"q"
RESPONSE_WHERE = b"W"
RESPONSE_FILE = b"F"
RESPONSE_LINE = b"L"
RESPONSE_ROUTINE = b"R"


# Byte1('N') - Identifier
# Int32 - Message length
# Any number of these, followed by a zero byte:
#   Byte1 - code identifying the field type (see responseKeys)
#   String - field value
def data_into_dict(data):
    return dict((s[0:1], s[1:]) for s in data.split(b"\x00"))


##
# This class represents a connection to a PostgreSQL database.
# <p>
# The database connection is derived from the {@link #Cursor Cursor} class,
# which provides a default cursor for running queries.  It also provides
# transaction control via the 'commit', and 'rollback' methods.
# <p>
# As of v1.01, instances of this class are thread-safe.  See {@link
# PreparedStatement PreparedStatement} for more information.
# <p>
# Stability: Added in v1.00, stability guaranteed for v1.xx.
#
# @param user   The username to connect to the PostgreSQL server with.  This
# parameter is required.
#
# @keyparam host   The hostname of the PostgreSQL server to connect with.
# Providing this parameter is necessary for TCP/IP connections.  One of either
# host, or unix_sock, must be provided.
#
# @keyparam unix_sock   The path to the UNIX socket to access the database
# through, for example, '/tmp/.s.PGSQL.5432'.  One of either unix_sock or host
# must be provided.  The port parameter will have no affect if unix_sock is
# provided.
#
# @keyparam port   The TCP/IP port of the PostgreSQL server instance.  This
# parameter defaults to 5432, the registered and common port of PostgreSQL
# TCP/IP servers.
#
# @keyparam database   The name of the database instance to connect with.  This
# parameter is optional, if omitted the PostgreSQL server will assume the
# database name is the same as the username.
#
# @keyparam password   The user password to connect to the server with.  This
# parameter is optional.  If omitted, and the database server requests password
# based authentication, the connection will fail.  On the other hand, if this
# parameter is provided and the database does not request password
# authentication, then the password will not be used.
#
# @keyparam socket_timeout  Socket connect timeout measured in seconds.
# Defaults to 60 seconds.
#
# @keyparam ssl     Use SSL encryption for TCP/IP socket.  Defaults to False.

##
# The class of object returned by the {@link #connect connect method}.
class Connection(object):
    # DBAPI Extension: supply exceptions as attributes on the connection
    Warning = property(lambda self: self._getError(Warning))
    Error = property(lambda self: self._getError(Error))
    InterfaceError = property(lambda self: self._getError(InterfaceError))
    DatabaseError = property(lambda self: self._getError(DatabaseError))
    OperationalError = property(lambda self: self._getError(OperationalError))
    IntegrityError = property(lambda self: self._getError(IntegrityError))
    InternalError = property(lambda self: self._getError(InternalError))
    ProgrammingError = property(lambda self: self._getError(ProgrammingError))
    NotSupportedError = property(
        lambda self: self._getError(NotSupportedError))

    def _getError(self, error):
        warn(
            "DB-API extension connection.%s used" %
            error.__name__, stacklevel=3)
        return error

    def __init__(
            self, user, host, unix_sock, port, database, password,
            socket_timeout, ssl):
        self._client_encoding = "ascii"
        self._integer_datetimes = False
        self._sock_lock = threading.Lock()
        self.user = user
        self.password = password
        self.autocommit = False
        try:
            if unix_sock is None and host is not None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            elif unix_sock is not None:
                if not hasattr(socket, "AF_UNIX"):
                    raise InterfaceError(
                        "attempt to connect to unix socket on unsupported "
                        "platform")
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            else:
                raise ProgrammingError(
                    "one of host or unix_sock must be provided")
            if unix_sock is None and host is not None:
                self._sock.connect((host, port))
            elif unix_sock is not None:
                self._sock.connect(unix_sock)
            if ssl:
                with self._sock_lock:
                    # Int32(8) - Message length, including self.
                    # Int32(80877103) - The SSL request code.
                    self._write(ii_pack(8, 80877103))
                    self._flush()
                    resp = self._sock.recv(1)
                    if resp == 'S':
                        self._sock = sslmodule.wrap_socket(self._sock)
                    else:
                        raise InterfaceError("server refuses SSL")

            # settimeout causes ssl failure, on windows.  Python bug 1462352.
            self._sock.settimeout(socket_timeout)

            self._sock_in = self._sock.makefile(mode="rb")
            self._read_bytes = self._sock_in.read
            self._sock = self._sock.makefile(mode="wb")
        except socket.error as e:
            raise InterfaceError("communication error", e)
        self._flush = self._sock.flush
        self._write = self._sock.write
        self._state = "noauth"
        self._backend_key_data = None

        ##
        # An event handler that is fired when the database server issues a notice.
        # The value of this property is a util.MulticastDelegate.  A callback can
        # be added by using connection.NotificationReceived += SomeMethod.  The
        # method will be called with a single argument, an object that has
        # properties: severity, code, msg, and possibly others (detail, hint,
        # position, where, file, line, and routine).  Callbacks can be removed with
        # the -= operator.
        # <p>
        # Stability: Added in v1.03, stability guaranteed for v1.xx.
        self.NoticeReceived = pg8000.util.MulticastDelegate()

        ##
        # An event handler that is fired when a runtime configuration option is
        # changed on the server.  The value of this property is a
        # util.MulticastDelegate.  A callback can be added by using
        # connection.NotificationReceived += SomeMethod.  Callbacks can be removed
        # with the -= operator.  The method will be called with a single argument,
        # an object that has properties "key" and "value".
        # <p>
        # Stability: Added in v1.03, stability guaranteed for v1.xx.
        self.ParameterStatusReceived = pg8000.util.MulticastDelegate()

        ##
        # An event handler that is fired when NOTIFY occurs for a notification that
        # has been LISTEN'd for.  The value of this property is a
        # util.MulticastDelegate.  A callback can be added by using
        # connection.NotificationReceived += SomeMethod.  The method will be called
        # with a single argument, an object that has properties: backend_pid,
        # condition, and additional_info.  Callbacks can be removed with the -=
        # operator.
        # <p>
        # Stability: Added in v1.03, stability guaranteed for v1.xx.
        self.NotificationReceived = pg8000.util.MulticastDelegate()

        self.ParameterStatusReceived += self.handle_PARAMETER_STATUS
        self.py_types = {
            bool: (16, FC_BINARY, bool_pack),
            float: (701, FC_BINARY, d_pack),
            Decimal: (1700, FC_BINARY, numeric_send),
            pg8000.types.Bytea: (17, FC_BINARY, byteasend),
            type(None): (-1, FC_BINARY, lambda value: i_pack(-1))}

        def textout(v):
            return v.encode(self._client_encoding)
        self.py_types[str] = (25, FC_BINARY, textout)

        def time_out(v):
            return v.isoformat().encode(self._client_encoding)
        self.py_types[datetime.time] = (1083, FC_TEXT, time_out)

        self.inspect_funcs = {
            int: inspect_int,
            datetime.datetime: self.inspect_datetime,
            list: self.array_inspect}

        def timestamp_send(v):
            delta = v - datetime.datetime(2000, 1, 1)
            val = delta.microseconds + delta.seconds * 1000000 + \
                delta.days * 86400000000
            if self._integer_datetimes:
                # data is 64-bit integer representing milliseconds since
                # 2000-01-01
                return q_pack(val)
            else:
                # data is double-precision float representing seconds since
                #2000-01-01
                return d_pack(val / 1000.0 / 1000.0)
        self.timestamp_send = timestamp_send

        def interval_send(data):
            if self._integer_datetimes:
                return qii_pack(data.microseconds, data.days, data.months)
            else:
                return dii_pack(
                    data.microseconds / 1000.0 / 1000.0, data.days,
                    data.months)
        self.py_types[Interval] = (1186, FC_BINARY, interval_send)

        def date_out(v):
            return v.isoformat().encode(self._client_encoding)
        self.py_types[datetime.date] = (1082, FC_TEXT, date_out)

        def timestamptz_send(v):
            # timestamps should be sent as UTC.  If they have zone info,
            # convert them.
            return self.timestamp_send(v.astimezone(utc).replace(tzinfo=None))
        self.timestamptz_send = timestamptz_send

        self.message_types = {
            NOTICE_RESPONSE: self.handle_NOTICE_RESPONSE,
            AUTHENTICATION_REQUEST: self.handle_AUTHENTICATION_REQUEST,
            PARAMETER_STATUS: self.handle_PARAMETER_STATUS,
            BACKEND_KEY_DATA: self.handle_BACKEND_KEY_DATA,
            READY_FOR_QUERY: self.handle_READY_FOR_QUERY,
            ROW_DESCRIPTION: self.handle_ROW_DESCRIPTION,
            ERROR_RESPONSE: self.handle_ERROR_RESPONSE,
            DATA_ROW: self.handle_DATA_ROW,
            COMMAND_COMPLETE: self.handle_COMMAND_COMPLETE,
            PARSE_COMPLETE: self.handle_PARSE_COMPLETE,
            BIND_COMPLETE: self.handle_BIND_COMPLETE,
            CLOSE_COMPLETE: self.handle_CLOSE_COMPLETE,
            PORTAL_SUSPENDED: self.handle_PORTAL_SUSPENDED,
            NO_DATA: self.handle_NO_DATA,
            PARAMETER_DESCRIPTION: self.handle_PARAMETER_DESCRIPTION,
            NOTIFICATION_RESPONSE: self.handle_NOTIFICATION_RESPONSE,
            COPY_DONE: self.handle_COPY_DONE,
            COPY_DATA: self.handle_COPY_DATA,
            COPY_IN_RESPONSE: self.handle_COPY_IN_RESPONSE,
            COPY_OUT_RESPONSE: self.handle_COPY_OUT_RESPONSE}

        self.verifyState("noauth")
        self.awaiting = set()
        self.awaiting.add("auth")
        # Int32 - Message length, including self.
        # Int32(196608) - Protocol version number.  Version 3.0.
        # Any number of key/value pairs, terminated by a zero byte:
        #   String - A parameter name (user, database, or options)
        #   String - Parameter value
        protocol = 196608
        val = bytearray(i_pack(protocol) + b"user\x00")
        val.extend(self.user.encode("ascii"))
        val.append(0)
        if database is not None:
            val.extend(b"database\x00")
            val.extend(database.encode("ascii"))
            val.append(0)
        val.append(0)
        val = i_pack(len(val) + 4) + val
        self._write(val)
        self._flush()
        with self._sock_lock:
            self.handle_messages(None)

        Cursor.__init__(self, self)
        self._begin = PreparedStatement(self, "BEGIN TRANSACTION")
        self._commit = PreparedStatement(self, "COMMIT TRANSACTION")
        self._rollback = PreparedStatement(self, "ROLLBACK TRANSACTION")
        self._unnamed_prepared_statement_lock = threading.RLock()
        self.in_transaction = False
        self.notifies = []
        self.notifies_lock = threading.Lock()

    def handle_ERROR_RESPONSE(self, data, ps):
        for req in (EXECUTE, DESCRIBE, CLOSE, PARSE, BIND):
            if req in self.awaiting:
                self.unawait(req)
        msg_dict = data_into_dict(data)
        if msg_dict[RESPONSE_CODE] == "28000":
            raise InterfaceError("md5 password authentication failed")
        else:
            raise ProgrammingError(
                msg_dict[RESPONSE_SEVERITY], msg_dict[RESPONSE_CODE],
                msg_dict[RESPONSE_MSG])

    def handle_CLOSE_COMPLETE(self, data, ps):
        self.unawait(CLOSE)

    def handle_PARSE_COMPLETE(self, data, ps):
        # Byte1('1') - Identifier.
        # Int32(4) - Message length, including self.
        if ps is None or not PARSE in self.awaiting:
            raise unexpected_response(PARSE_COMPLETE)
        self.unawait(PARSE)

    def handle_BIND_COMPLETE(self, data, ps):
        if not BIND in self.awaiting:
            raise unexpected_response(BIND_COMPLETE)
        self.unawait(BIND)

    def handle_PORTAL_SUSPENDED(self, data, ps):
        ps.portal_suspended = True
        self.unawait(EXECUTE)

    def handle_PARAMETER_DESCRIPTION(self, data, ps):
        # Well, we don't really care -- we're going to send whatever we
        # want and let the database deal with it.  But thanks anyways!

        # count = h_unpack(data)[0]
        # type_oids = unpack_from("!" + "i" * count, data, 2)
        pass

    def handle_COPY_DONE(self, data, ps):
        self._copy_done = True

    def handle_COPY_OUT_RESPONSE(self, data, ps):
        # Int8(1) - 0 textual, 1 binary
        # Int16(2) - Number of columns
        # Int16(N) - Format codes for each column (0 text, 1 binary)

        is_binary, num_cols = bh_unpack(data)
        # column_formats = unpack_from('!' + 'h' * num_cols, data, 3)
        if ps.stream is None:
            raise CopyQueryWithoutStreamError()

    def handle_COPY_DATA(self, data, ps):
        ps.stream.write(data)

    def handle_COPY_IN_RESPONSE(self, data, ps):
        # Int16(2) - Number of columns
        # Int16(N) - Format codes for each column (0 text, 1 binary)
        is_binary, num_cols = bh_unpack(data)
        # column_formats = unpack_from('!' + 'h' * num_cols, data, 3)
        assert self._sock_lock.locked()
        if ps.stream is None:
            raise CopyQueryWithoutStreamError()
        bffr = bytearray(8192)
        while True:
            bytes_read = ps.stream.readinto(bffr)
            if bytes_read == 0:
                break
            bffr[:0] = b'd' + i_pack(bytes_read + 4)
            self._write(bffr[: bytes_read + 5])
            self._flush()
        # Send CopyDone
        # Byte1('c') - Identifier.
        # Int32(4) - Message length, including self.
        self._send_message(COPY_DONE)
        self._send_message(SYNC)
        self._flush()




    def handle_NOTIFICATION_RESPONSE(self, data, ps):
        self.NotificationReceived(data)
        ##
        # A message sent if this connection receives a NOTIFY that it was
        # LISTENing for.
        # <p>
        # Stability: Added in pg8000 v1.03.  When limited to accessing
        # properties from a notification event dispatch, stability is
        # guaranteed for v1.xx.
        backend_pid = i_unpack(data)[0]
        idx = 4
        null = data.find(b"\x00", idx) - idx
        condition = data[idx:idx + null].decode("ascii")
        idx += null + 1
        null = data.find(b"\x00", idx) - idx
        # additional_info = data[idx:idx + null]

        # psycopg2 compatible notification interface
        with self.notifies_lock:
            self.notifies.append((backend_pid, condition))

    ##
    # Creates a {@link #CursorWrapper CursorWrapper} object bound to this
    # connection.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @require_open_connection
    def cursor(self):
        return Cursor(self)

    ##
    # Commits the current database transaction.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @require_open_connection
    def commit(self):
        # There's a threading bug here.  If a query is sent after the
        # commit, but before the begin, it will be executed immediately
        # without a surrounding transaction.  Like all threading bugs -- it
        # sounds unlikely, until it happens every time in one
        # application...  however, to fix this, we need to lock the
        # database connection entirely, so that no cursors can execute
        # statements on other threads.  Support for that type of lock will
        # be done later.
        self._commit.execute()
        self.in_transaction = False

    ##
    # Rolls back the current database transaction.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    @require_open_connection
    def rollback(self):
        # see bug description in commit.

        self._rollback.execute()
        self.in_transaction = False

    ##
    # Closes the database connection.
    # <p>
    # Stability: Part of the DBAPI 2.0 specification.
    def close(self):
        if self._state == "closed":
            raise ConnectionClosedError()
        with self._sock_lock:
            # Byte1('X') - Identifies the message as a terminate message.
            # Int32(4) - Message length, including self.
            self._send_message(TERMINATE)
            self._flush()
            self._sock.close()
            self._state = "closed"

    ##
    # Begins a new transaction.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def begin(self):
        if not self._conn.in_transaction and not self.autocommit:
            self._conn._begin.execute()
            self._conn.in_transaction = True

    def verifyState(self, state):
        if self._state != state:
            raise InterfaceError(
                "connection state must be {0}, is {1}".format(
                    state, self._state))

    def handle_AUTHENTICATION_REQUEST(self, data, ps):
        assert self._sock_lock.locked()
        # Int32 -   An authentication code that represents different
        #           authentication messages:
        #               0 = AuthenticationOk
        #               5 = MD5 pwd
        #               2 = Kerberos v5 (not supported by pg8000)
        #               3 = Cleartext pwd (not supported by pg8000)
        #               4 = crypt() pwd (not supported by pg8000)
        #               6 = SCM credential (not supported by pg8000)
        #               7 = GSSAPI (not supported by pg8000)
        #               8 = GSSAPI data (not supported by pg8000)
        #               9 = SSPI (not supported by pg8000)
        # Some authentication messages have additional data following the
        # authentication code.  That data is documented in the appropriate
        # class.
        auth_code = i_unpack(data)[0]
        if auth_code == 0:
            pass
        elif auth_code == 5:
            ##
            # A message representing the backend requesting an MD5 hashed
            # password response.  The response will be sent as
            # md5(md5(pwd + login) + salt).

            # Additional message data:
            #  Byte4 - Hash salt.
            salt = b"".join(cccc_unpack(data))
            if self.password is None:
                raise InterfaceError(
                    "server requesting MD5 password authentication, but no "
                    "password was provided")
            pwd = b"md5" + hashlib.md5(
                hashlib.md5(
                    self.password.encode("ascii") +
                    self.user.encode("ascii")).hexdigest().encode("ascii") +
                salt).hexdigest().encode("ascii")
            # Byte1('p') - Identifies the message as a password message.
            # Int32 - Message length including self.
            # String - The password.  Password may be encrypted.
            val = bytearray(pwd)
            val.append(0)
            self._send_message(PASSWORD, val)
            self._flush()

        elif auth_code in (2, 3, 4, 6, 7, 8, 9):
            raise NotSupportedError(
                "authentication method {0} not supported".format(auth_code))
        else:
            raise InternalError(
                "Authentication method {0} not recognized".format(auth_code))

        self._state = "auth"

    def handle_READY_FOR_QUERY(self, data, ps):
        # Byte1 -   Status indicator.
        self._state = "ready"
        if 'auth' in self.awaiting:
            self.awaiting.remove('auth')
        self._ready_status = {
            b"I": "Idle", b"T": "Idle in Transaction",
            b"E": "Idle in Failed Transaction"}[data]

    def handle_BACKEND_KEY_DATA(self, data, ps):
        self._backend_key_data = data

    def inspect_datetime(self, value):
        if value.tzinfo is not None:
            # send as timestamptz if timezone is provided
            return (1184, FC_BINARY, self.timestamptz_send)
        else:
            # otherwise send as timestamp
            return (1114, FC_BINARY, self.timestamp_send)

    def make_params(self, values):
        params = []
        for value in values:
            typ = type(value)
            try:
                params.append(self.py_types[typ])
            except KeyError:
                try:
                    params.append(self.inspect_funcs[typ](value))
                except KeyError as e:
                    raise NotSupportedError(
                        "type {0} not mapped to pg type".format(e))
        return params

    def handle_ROW_DESCRIPTION(self, data, ps):
        count = h_unpack(data)[0]
        idx = 2
        row_desc = []
        for i in range(count):
            null = data.find(b"\x00", idx) - idx
            field = {"name": data[idx:idx + null]}
            idx += null + 1
            field["table_oid"], field["column_attrnum"], field["type_oid"], \
                field["type_size"], field["type_modifier"], field["format"] = \
                ihihih_unpack(data, idx)
            idx += 18
            row_desc.append(field)
        if ps.statement_row_desc is None:
            ps.statement_row_desc = row_desc
        else:
            ps.portal_row_desc = row_desc
        self.unawait(DESCRIBE)

    def unawait(self, code):
        try:
            self.awaiting.remove(code)
        except KeyError:
            raise InternalError(
                "We were never waiting on {0} in the first place.".format(
                    code))

    def parse(self, ps, statement):
        with self._sock_lock:
            self.verifyState("ready")
            self.parsing = True

            statement_name = ps.statement_name.encode('ascii')

            # Byte1('P') - Identifies the message as a Parse command.
            # Int32 -   Message length, including self.
            # String -  Prepared statement name. An empty string selects the
            #           unnamed prepared statement.
            # String -  The query string.
            # Int16 -   Number of parameter data types specified (can be zero).
            # For each parameter:
            #   Int32 - The OID of the parameter data type.
            val = bytearray(statement_name)
            val.append(0)
            val.extend(statement.encode(self._client_encoding))
            val.append(0)
            val.extend(h_pack(len(ps.params)))
            for oid, fc, send_func in ps.params:
                # Parse message doesn't seem to handle the -1 type_oid for NULL
                # values that other messages handle.  So we'll provide type_oid
                # 705, the PG "unknown" type.
                if oid == -1:
                    oid = 705
                val.extend(i_pack(oid))
            self._send_await(PARSE, val)

            # Byte1('D') - Identifies the message as a describe command.
            # Int32 - Message length, including self.
            # Byte1 - 'S' for prepared statement, 'P' for portal.
            # String - The name of the item to describe.
            val = bytearray(b"S" + statement_name)
            val.append(0)
            self._send_await(DESCRIBE, val)
            self._send_message(SYNC)
            self._send_message(FLUSH)
            self._flush()
            self.handle_messages(ps)

    def _send_await(self, code, data):
        if code in self.awaiting:
            raise InternalError("Already waiting for a response to this code.")
        self.awaiting.add(code)
        self._send_message(code, data)

    def bind(self, ps, values):
        with self._sock_lock:
            self.verifyState("ready")
            if ps.statement_row_desc is None:
                # no data going out
                output_fc = ()
            else:
                # We've got row_desc that allows us to identify what we're
                # going to get back from this statement.
                try:
                    output_fc = tuple(
                        pg_types[f['type_oid']][0] for f in
                        ps.statement_row_desc)
                except KeyError as e:
                    raise NotSupportedError(
                        "type oid %r not mapped to py type" % str(e))

            statement_name_bin = ps.statement_name.encode('ascii')
            portal_name_bin = ps.portal_name.encode('ascii')

            # Byte1('B') - Identifies the Bind command.
            # Int32 - Message length, including self.
            # String - Name of the destination portal.
            # String - Name of the source prepared statement.
            # Int16 - Number of parameter format codes.
            # For each parameter format code:
            #   Int16 - The parameter format code.
            # Int16 - Number of parameter values.
            # For each parameter value:
            #   Int32 - The length of the parameter value, in bytes, not
            #           including this length.  -1 indicates a NULL parameter
            #           value, in which no value bytes follow.
            #   Byte[n] - Value of the parameter.
            # Int16 - The number of result-column format codes.
            # For each result-column format code:
            #   Int16 - The format code.
            retval = bytearray(portal_name_bin + b"\x00")
            retval.extend(statement_name_bin + b"\x00")
            retval.extend(h_pack(len(ps.param_fcs)))
            retval.extend(pack("!" + "h" * len(ps.param_fcs), *ps.param_fcs))
            retval.extend(h_pack(len(ps.params)))
            for i, param in enumerate(ps.params):
                if len(ps.param_fcs) == 0:
                    param_fc = 0
                elif len(ps.param_fcs) == 1:
                    param_fc = ps.param_fcs[0]
                else:
                    param_fc = ps.param_fcs[i]
                oid, fc, send_func = param
                if param_fc != fc:
                    raise NotSupportedError(
                        "type {0}, format code {1} not supported".format(oid,
                        param_fc))
                val = send_func(values[i])
                if oid != -1:
                    retval.extend(i_pack(len(val)))
                retval.extend(val)
            retval.extend(h_pack(len(output_fc)))
            retval.extend(pack("!" + "h" * len(output_fc), *output_fc))
            self._send_await(BIND, retval)

            # We need to describe the portal after bind, since the return
            # format codes will be different (hopefully, always what we
            # requested).

            # Byte1('D') - Identifies the message as a describe command.
            # Int32 - Message length, including self.
            # Byte1 - 'S' for prepared statement, 'P' for portal.
            # String - The name of the item.
            val = bytearray(b'P' + portal_name_bin)
            val.append(0)
            self._send_await(DESCRIBE, val)
            assert self._sock_lock.locked()
            self._send_message(FLUSH)
            self._flush()

            self.handle_messages(ps)

    def _send_message(self, code, data=None):
        if data is None:
            data = bytearray()
        data[:0] = code + i_pack(len(data) + 4)
        self._write(data)

    # Byte1('E') - Identifies the message as an execute message.
    # Int32 -   Message length, including self.
    # String -  The name of the portal to execute.
    # Int32 -   Maximum number of rows to return, if portal contains a query
    # that returns rows.  0 = no limit.
    def send_EXECUTE(self, ps, row_count):
        ps.cmd = None
        ps.portal_suspended = False
        val = bytearray(ps.portal_name, "ascii")
        val.append(0)
        val.extend(i_pack(row_count))
        self._send_await(EXECUTE, val)

    def handle_NO_DATA(self, msg, ps):
        assert self._sock_lock.locked()
        if ps is None:
            raise unexpected_response(NO_DATA)

        if ps.statement_row_desc is None:
            ps.statement_row_desc = []
        else:
            # Bind message returned NoData, causing us to execute the command.
            ps.portal_row_desc = []
            self.send_EXECUTE(ps, 0)
            self._send_message(SYNC)
            self._flush()
        self.unawait(DESCRIBE)

    def handle_COMMAND_COMPLETE(self, data, ps):
        ps.cmd = {}
        data = data[:-1]
        values = data.split(b" ")
        if values[0] in (
                b"INSERT", b"DELETE", b"UPDATE", b"MOVE", b"FETCH", b"COPY",
                b"SELECT"):
            ps.cmd['command'] = values[0]
            row_count = int(values[-1])
            if ps.row_count == -1:
                ps.row_count = row_count
            else:
                ps.row_count += row_count
            if values[0] == "INSERT":
                ps.cmd['oid': int(values[1])]
        else:
            ps.cmd['command'] = data
        self.unawait(EXECUTE)

    def fetch_rows(self, ps):
        with self._sock_lock:
            self.verifyState("ready")
            if ps._cached_rows:
                raise InternalError("attempt to fill cache that isn't empty")
            self.send_EXECUTE(ps, PreparedStatement.row_cache_size)
            self._send_message(SYNC)
            self._send_message(FLUSH)
            self._flush()

            self.handle_messages(ps)

    def handle_DATA_ROW(self, data, ps):
        count = h_unpack(data)[0]
        data_idx = 2
        row = []
        for i in range(count):
            val_len = i_unpack(data, data_idx)[0]
            data_idx += 4
            if val_len == -1:
                row.append(None)
            else:
                description = ps.portal_row_desc[i]
                try:
                    fc, func = pg_types[description['type_oid']]
                except KeyError as e:
                    raise NotSupportedError(
                        "type oid {0} not supported".format(str(e)))

                fmt = description['format']
                if fc != fmt:
                    raise NotSupportedError(
                        "format code {0} not supported for type {1}".format(
                        fmt, description['type_oid']))
                row.append(
                    func(
                        data[data_idx:data_idx + val_len],
                        self._client_encoding, self._integer_datetimes))
                data_idx += val_len
        ps._cached_rows.append(tuple(row))

    def handle_messages(self, prepared_statement=None):
        assert self._sock_lock.locked()
        while len(self.awaiting) > 0:
            message_code, data_len = ci_unpack(self._read_bytes(5))
            try:
                self.message_types[message_code](
                    self._read_bytes(data_len - 4), prepared_statement)
            except KeyError:
                raise InternalError(
                    "Unrecognised message code {0}".format(message_code))

    # Byte1('C') - Identifies the message as a close command.
    # Int32 - Message length, including self.
    # Byte1 - 'S' for prepared statement, 'P' for portal.
    # String - The name of the item to close.
    def _send_CLOSE(self, typ, ps):
        self._send_await(
            CLOSE, bytearray(
                typ + ps.statement_name.encode("ascii") + b"\x00"))

    def _send_CLOSE_portal(self, ps):
        return self._send_CLOSE(b"P", ps)

    def close_statement(self, ps):
        if self._state == "closed":
            return
        self.verifyState("ready")

        with self._sock_lock:
            self._send_CLOSE(b"S", ps)
            self._send_message(SYNC)
            self._flush()

            self.handle_messages(ps)

    def close_portal(self, ps):
        if self._state == "closed":
            return
        self.verifyState("ready")
        with self._sock_lock:
            self._send_CLOSE_portal(ps)
            self._send_message(SYNC)
            self._flush()

            self.handle_messages(ps)

    def handle_NOTICE_RESPONSE(self, data, ps):
        resp = data_into_dict(data)
        self.NoticeReceived(resp)

    def handle_PARAMETER_STATUS(self, data, ps):
        pos = data.find(b"\x00")
        key, value = data[:pos], data[pos + 1:-1]
        if key == b"client_encoding":
            encoding = value.decode("ascii").lower()
            self._client_encoding = pg_to_py_encodings.get(encoding, encoding)
        elif key == b"integer_datetimes":
            self._integer_datetimes = (value == b"on")

    def array_inspect(self, value):
        # Check if array has any values.  If not, we can't determine the proper
        # array typeoid.
        first_element = array_find_first_element(value)
        if first_element is None:
            raise ArrayContentEmptyError("array has no values")

        # supported array output
        typ = type(first_element)

        if issubclass(typ, int):
            # special int array support -- send as smallest possible array type
            int2_ok, int4_ok, int8_ok = True, True, True
            for v in array_flatten(value):
                if v is None:
                    continue
                if min_int2 < v < max_int2:
                    continue
                int2_ok = False
                if min_int4 < v < max_int4:
                    continue
                int4_ok = False
                if min_int8 < v < max_int8:
                    continue
                int8_ok = False
            if int2_ok:
                array_typeoid = 1005  # INT2[]
                oid, fc, send_func = (21, FC_BINARY, h_pack)
            elif int4_ok:
                array_typeoid = 1007  # INT4[]
                oid, fc, send_func = (23, FC_BINARY, i_pack)
            elif int8_ok:
                array_typeoid = 1016  # INT8[]
                oid, fc, send_func = (20, FC_BINARY, q_pack)
            else:
                raise ArrayContentNotSupportedError(
                    "numeric not supported as array contents")
        else:
            try:
                oid, fc, send_func = self.make_params((first_element,))[0]
                array_typeoid = pg_array_types[oid]
            except KeyError:
                raise ArrayContentNotSupportedError(
                    "type {0} not supported as array contents".format(typ))
            except NotSupportedError:
                raise ArrayContentNotSupportedError(
                    "type {0} not supported as array contents".format(typ))

        def send_array(arr):
            # check for homogenous array
            for v in array_flatten(value):
                if v is not None and not isinstance(v, typ):
                    raise ArrayContentNotHomogenousError(
                        "not all array elements are of type %r" % typ)

            # check that all array dimensions are consistent
            array_check_dimensions(value)

            has_null = array_has_null(arr)
            dim_lengths = array_dim_lengths(arr)
            data = bytearray(iii_pack(len(dim_lengths), has_null, oid))
            for i in dim_lengths:
                data.extend(ii_pack(i, 1))
            for v in array_flatten(arr):
                if v is None:
                    data += i_pack(-1)
                else:
                    inner_data = send_func(v)
                    data += i_pack(len(inner_data))
                    data += inner_data
            return data
        return (array_typeoid, FC_BINARY, send_array)


##
# Creates a DBAPI 2.0 compatible interface to a PostgreSQL database.
# <p>
# Stability: Part of the DBAPI 2.0 specification.
#
# @param user   The username to connect to the PostgreSQL server with.  This
# parameter is required.
#
# @keyparam host   The hostname of the PostgreSQL server to connect with.
# Providing this parameter is necessary for TCP/IP connections.  One of either
# host, or unix_sock, must be provided.
#
# @keyparam unix_sock   The path to the UNIX socket to access the database
# through, for example, '/tmp/.s.PGSQL.5432'.  One of either unix_sock or host
# must be provided.  The port parameter will have no affect if unix_sock is
# provided.
#
# @keyparam port   The TCP/IP port of the PostgreSQL server instance.  This
# parameter defaults to 5432, the registered and common port of PostgreSQL
# TCP/IP servers.
#
# @keyparam database   The name of the database instance to connect with.  This
# parameter is optional, if omitted the PostgreSQL server will assume the
# database name is the same as the username.
#
# @keyparam password   The user password to connect to the server with.  This
# parameter is optional.  If omitted, and the database server requests password
# based authentication, the connection will fail.  On the other hand, if this
# parameter is provided and the database does not request password
# authentication, then the password will not be used.
#
# @keyparam socket_timeout  Socket connect timeout measured in seconds.
# Defaults to 60 seconds.
#
# @keyparam ssl     Use SSL encryption for TCP/IP socket.  Defaults to False.
#
# @return An instance of {@link #ConnectionWrapper ConnectionWrapper}.
def connect(
        user, host='localhost', unix_sock=None, port=5432, database=None,
        password=None, socket_timeout=60, ssl=False):
    return Connection(
        user, host, unix_sock, port, database, password, socket_timeout, ssl)


try:
    from pytz import utc
except ImportError:
    ZERO = timedelta(0)

    class UTC(datetime.tzinfo):

        def utcoffset(self, dt):
            return ZERO

        def tzname(self, dt):
            return "UTC"

        def dst(self, dt):
            return ZERO
    utc = UTC()


# pg element typeoid -> pg array typeoid
pg_array_types = {
    701: 1022,
    16: 1000,
    25: 1009,      # TEXT[]
    1700: 1231,  # NUMERIC[]
}


def varcharin(data, client_encoding, integer_datetimes):
    return str(data, client_encoding)


def byteasend(v):
    return v


def bytearecv(data, client_encoding, integer_datetimes):
    return pg8000.types.Bytea(data)


def interval_recv(data, client_encoding, integer_datetimes):
    if integer_datetimes:
        microseconds, days, months = qii_unpack(data)
    else:
        seconds, days, months = dii_unpack(data)
        microseconds = int(seconds * 1000 * 1000)
    return Interval(microseconds, days, months)

bool_struct = Struct("?")
bool_unpack = bool_struct.unpack
bool_pack = bool_struct.pack


def boolrecv(data, client_encoding, integer_datetimes):
    return bool_unpack(data)[0]


def int2recv(data, client_encoding, integer_datetimes):
    return h_unpack(data)[0]


def int2send(v):
    return h_pack(v)


def int4recv(data, client_encoding, integer_datetimes):
    return i_unpack(data)[0]


def int4send(v):
    return i_pack(v)


def int8recv(data, client_encoding, integer_datetimes):
    return q_unpack(data)[0]


def int8send(v):
    return q_pack(v)


def float4recv(data, client_encoding, integer_datetimes):
    return f_unpack(data)[0]


def float8recv(data, client_encoding, integer_datetimes):
    return d_unpack(data)[0]


def float8send(v):
    return d_pack(v)


def timestamp_recv(data, client_encoding, integer_datetimes):
    if integer_datetimes:
        # data is 64-bit integer representing milliseconds since 2000-01-01
        val = q_unpack(data)[0]
        return datetime.datetime(2000, 1, 1) + timedelta(microseconds=val)
    else:
        # data is double-precision float representing seconds since 2000-01-01
        val = d_unpack(data)[0]
        return datetime(2000, 1, 1) + timedelta(seconds=val)


# return a timezone-aware datetime instance if we're reading from a
# "timestamp with timezone" type.  The timezone returned will always be UTC,
# but providing that additional information can permit conversion to local.
def timestamptz_recv(data, client_encoding, integer_datetimes):
    return timestamp_recv(
        data, client_encoding, integer_datetimes).replace(tzinfo=utc)


def date_in(data, client_encoding, integer_datetimes):
    return datetime.date(int(data[0:4]), int(data[5:7]), int(data[8:10]))


def time_in(data, client_encoding, integer_datetimes):
    hour = int(data[0:2])
    minute = int(data[3:5])
    sec = Decimal(data[6:].decode("ascii"))
    return datetime.time(
        hour, minute, int(sec), int((sec - int(sec)) * 1000000))


def numeric_in(data, client_encoding, integer_datetimes):
    if data.find(b".") == -1:
        return int(data)
    else:
        return Decimal(data)


def numeric_recv(data, client_encoding, integer_datetimes):
    num_digits, weight, sign, scale = hhhh_unpack(data)
    pos_weight = max(0, weight) + 1
    digits = ['0000'] * abs(min(weight, 0)) + \
        [str(d).zfill(4) for d in unpack_from(
            "!" + "h" * num_digits, data, 8)] \
        + ['0000'] * (pos_weight - num_digits)
    return Decimal(
        ''.join(['-' if sign else '', ''.join(digits[:pos_weight]), '.',
        ''.join(digits[pos_weight:])[:scale]]))

DEC_DIGITS = 4


def numeric_send(d, **kwargs):
    # This is a very straight port of src/backend/utils/adt/numeric.c
    # set_var_from_str()
    s = str(d)
    pos = 0
    sign = 0
    if s[0] == '-':
        sign = 0x4000  # NEG
        pos = 1
    elif s[0] == '+':
        sign = 0  # POS
        pos = 1
    have_dp = False
    decdigits = [0, 0, 0, 0]
    dweight = -1
    dscale = 0
    for char in s[pos:]:
        if char.isdigit():
            decdigits.append(int(char))
            if not have_dp:
                dweight += 1
            else:
                dscale += 1
            pos += 1
        elif char == '.':
            have_dp = True
            pos += 1
        else:
            break

    if len(s) > pos:
        char = s[pos]
        if char == 'e' or char == 'E':
            pos += 1
            exponent = int(s[pos:])
            dweight += exponent
            dscale -= exponent
            if dscale < 0:
                dscale = 0

    if dweight >= 0:
        weight = int((dweight + 1 + DEC_DIGITS - 1) / DEC_DIGITS - 1)
    else:
        weight = int(-((-dweight - 1) / DEC_DIGITS + 1))
    offset = (weight + 1) * DEC_DIGITS - (dweight + 1)
    ndigits = int(
        (len(decdigits) - DEC_DIGITS + offset + DEC_DIGITS - 1) / DEC_DIGITS)

    i = DEC_DIGITS - offset
    decdigits.extend([0, 0, 0])
    ndigits_ = ndigits
    digits = b''
    while ndigits_ > 0:
        # ifdef DEC_DIGITS == 4
        digits += h_pack(
            ((decdigits[i] * 10 + decdigits[i + 1]) * 10 + decdigits[i + 2])
            * 10 + decdigits[i + 3])
        ndigits_ -= 1
        i += DEC_DIGITS

    # strip_var()
    for char in digits:
        if ndigits == 0:
            break
        if char == '0':
            weight -= 1
            ndigits -= 1
        else:
            break

    for char in reversed(digits):
        if ndigits == 0:
            break
        if char == '0':
            ndigits -= 1
        else:
            break

    if ndigits == 0:
        sign = 0x4000  # pos
        weight = 0
    # ----------

    retval = hhhh_pack(ndigits, weight, sign, dscale) + digits
    return retval


def numeric_out(v, **kwargs):
    return str(v).encode("ascii")


# PostgreSQL encodings:
#   http://www.postgresql.org/docs/8.3/interactive/multibyte.html
# Python encodings:
#   http://www.python.org/doc/2.4/lib/standard-encodings.html
#
# Commented out encodings don't require a name change between PostgreSQL and
# Python.  If the py side is None, then the encoding isn't supported.
pg_to_py_encodings = {
    # Not supported:
    "mule_internal": None,
    "euc_tw": None,

    # Name fine as-is:
    #"euc_jp",
    #"euc_jis_2004",
    #"euc_kr",
    #"gb18030",
    #"gbk",
    #"johab",
    #"sjis",
    #"shift_jis_2004",
    #"uhc",
    #"utf8",

    # Different name:
    "euc_cn": "gb2312",
    "iso_8859_5": "is8859_5",
    "iso_8859_6": "is8859_6",
    "iso_8859_7": "is8859_7",
    "iso_8859_8": "is8859_8",
    "koi8": "koi8_r",
    "latin1": "iso8859-1",
    "latin2": "iso8859_2",
    "latin3": "iso8859_3",
    "latin4": "iso8859_4",
    "latin5": "iso8859_9",
    "latin6": "iso8859_10",
    "latin7": "iso8859_13",
    "latin8": "iso8859_14",
    "latin9": "iso8859_15",
    "sql_ascii": "ascii",
    "win866": "cp886",
    "win874": "cp874",
    "win1250": "cp1250",
    "win1251": "cp1251",
    "win1252": "cp1252",
    "win1253": "cp1253",
    "win1254": "cp1254",
    "win1255": "cp1255",
    "win1256": "cp1256",
    "win1257": "cp1257",
    "win1258": "cp1258",
}


def array_recv(data, client_encoding, integer_datetimes):
    idx = 0

    dim, hasnull, typeoid = iii_unpack(data)
    idx += 12

    # get type conversion method for typeoid
    conversion = pg_types[typeoid][1]

    # Read dimension info
    dim_lengths = []
    for i in range(dim):
        dim_lengths.append(ii_unpack(data, idx)[0])
        idx += 8

    # Read all array values
    values = []
    while idx < len(data):
        element_len, = i_unpack(data, idx)
        idx += 4
        if element_len == -1:
            values.append(None)
        else:
            values.append(
                conversion(
                    data[idx:idx + element_len], client_encoding,
                    integer_datetimes))
            idx += element_len

    # at this point, {{1,2,3},{4,5,6}}::int[][] looks like [1,2,3,4,5,6].
    # go through the dimensions and fix up the array contents to match
    # expected dimensions
    for length in reversed(dim_lengths[1:]):
        values = list(map(list, zip(*[iter(values)] * length)))
    return values


def array_find_first_element(arr):
    for v in array_flatten(arr):
        if v is not None:
            return v
    return None


def array_flatten(arr):
    for v in arr:
        if isinstance(v, list):
            for v2 in array_flatten(v):
                yield v2
        else:
            yield v


def array_check_dimensions(arr):
    v0 = arr[0]
    if isinstance(v0, list):
        req_len = len(v0)
        req_inner_lengths = array_check_dimensions(v0)
        for v in arr:
            inner_lengths = array_check_dimensions(v)
            if len(v) != req_len or inner_lengths != req_inner_lengths:
                raise ArrayDimensionsNotConsistentError(
                    "array dimensions not consistent")
        retval = [req_len]
        retval.extend(req_inner_lengths)
        return retval
    else:
        # make sure nothing else at this level is a list
        for v in arr:
            if isinstance(v, list):
                raise ArrayDimensionsNotConsistentError(
                    "array dimensions not consistent")
        return []


def array_has_null(arr):
    for v in array_flatten(arr):
        if v is None:
            return True
    return False


def array_dim_lengths(arr):
    v0 = arr[0]
    if isinstance(v0, list):
        retval = [len(v0)]
        retval.extend(array_dim_lengths(v0))
    else:
        return [len(arr)]
    return retval

pg_types = {
    16: (FC_BINARY, boolrecv),
    17: (FC_BINARY, bytearecv),
    19: (FC_BINARY, varcharin),  # name type
    20: (FC_BINARY, int8recv),
    21: (FC_BINARY, int2recv),
    23: (FC_BINARY, int4recv),
    25: (FC_BINARY, varcharin),  # TEXT type
    26: (FC_TEXT, numeric_in),  # oid type
    700: (FC_BINARY, float4recv),
    701: (FC_BINARY, float8recv),
    829: (FC_TEXT, varcharin),  # MACADDR type
    1000: (FC_BINARY, array_recv),  # BOOL[]
    1003: (FC_BINARY, array_recv),  # NAME[]
    1005: (FC_BINARY, array_recv),  # INT2[]
    1007: (FC_BINARY, array_recv),  # INT4[]
    1009: (FC_BINARY, array_recv),  # TEXT[]
    1014: (FC_BINARY, array_recv),  # CHAR[]
    1015: (FC_BINARY, array_recv),  # VARCHAR[]
    1016: (FC_BINARY, array_recv),  # INT8[]
    1021: (FC_BINARY, array_recv),  # FLOAT4[]
    1022: (FC_BINARY, array_recv),  # FLOAT8[]
    1042: (FC_BINARY, varcharin),  # CHAR type
    1043: (FC_BINARY, varcharin),  # VARCHAR type
    1082: (FC_TEXT, date_in),
    1083: (FC_TEXT, time_in),
    1114: (FC_BINARY, timestamp_recv),
    1184: (FC_BINARY, timestamptz_recv),  # timestamp w/ tz
    1186: (FC_BINARY, interval_recv),
    1231: (FC_BINARY, array_recv),  # NUMERIC[]
    1263: (FC_BINARY, array_recv),  # cstring[]
    1700: (FC_BINARY, numeric_recv),
    2275: (FC_BINARY, varcharin),  # cstring
}


class DataIterator(object):
    def __init__(self, obj, func):
        self.obj = obj
        self.func = func

    def __iter__(self):
        return self

    def __next__(self):
        retval = self.func(self.obj)
        if retval is None:
            raise StopIteration()
        return retval


##
# This class represents a prepared statement.  A prepared statement is
# pre-parsed on the server, which reduces the need to parse the query every
# time it is run.  The statement can have parameters in the form of $1, $2, $3,
# etc.  When parameters are used, the types of the parameters need to be
# specified when creating the prepared statement.
# <p>
# As of v1.01, instances of this class are thread-safe.  This means that a
# single PreparedStatement can be accessed by multiple threads without the
# internal consistency of the statement being altered.  However, the
# responsibility is on the client application to ensure that one thread reading
# from a statement isn't affected by another thread starting a new query with
# the same statement.
# <p>
# Stability: Added in v1.00, stability guaranteed for v1.xx.
#
# @param connection     An instance of {@link Connection Connection}.
#
# @param statement      The SQL statement to be represented, often containing
# parameters in the form of $1, $2, $3, etc.
#
# @param types          Python type objects for each parameter in the SQL
# statement.  For example, int, float, str.
class PreparedStatement(object):

    ##
    # Determines the number of rows to read from the database server at once.
    # Reading more rows increases performance at the cost of memory.  The
    # default value is 100 rows.  The affect of this parameter is transparent.
    # That is, the library reads more rows when the cache is empty
    # automatically.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.  It is
    # possible that implementation changes in the future could cause this
    # parameter to be ignored.
    row_cache_size = 100

    def __init__(
            self, connection, statement, *values, statement_name=None,
            stream=None):

        # Stability: Added in v1.03, stability guaranteed for v1.xx.
        self.row_count = -1

        global statement_number
        if connection is None:
            raise InterfaceError("connection not provided")
        with statement_number_lock:
            self._statement_number = statement_number
            statement_number += 1
        self.c = connection
        self.portal_name = None
        if statement_name is None:
            self.statement_name = "pg8000_statement_{0}".format(
                self._statement_number)
        else:
            self.statement_name = statement_name
        self.stream = stream
        self._cached_rows = []
        self.params = self.c.make_params(values)
        self.param_fcs = tuple(x[1] for x in self.params)
        self.statement_row_desc = None
        self.c.parse(self, statement)
        self._lock = threading.RLock()
        self.cmd = None

    def close(self):
        if self.statement_name != "":  # don't close unnamed statement
            self.c.close_statement(self)
        if self.portal_name is not None:
            self.c.close_portal(self)
            self.portal_name = None

    def get_row_description(self):
        if self.portal_row_desc is not None:
            return self.portal_row_desc
        if self.statement_row_desc is not None:
            return self.statment_row_desc
        return None

    ##
    # Run the SQL prepared statement with the given parameters.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def execute(self, *values, **kwargs):
        with self._lock:
            # cleanup last execute
            self._cached_rows = []
            self.row_count = -1
            self.portal_suspended = False
            if self.portal_name is not None:
                self.c.close_portal(self)
            with portal_number_lock:
                global portal_number
                self.portal_name = "pg8000_portal_{0}".format(portal_number)
                portal_number += 1
            self.cmd = None
            self.stream = kwargs.get("stream")
            self.portal_row_desc = None
            self.c.bind(self, values)
            if self.portal_row_desc:
                # We execute our cursor right away to fill up our cache. This
                # prevents the cursor from being destroyed, apparently, by a
                # rogue Sync between Bind and Execute.  Since it is quite
                # likely that data will be read from us right away anyways,
                # this seems a safe move for now.
                self.c.fetch_rows(self)

    ##
    # Read a row from the database server, and return it as a tuple of values.
    # Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def read_tuple(self):
        if len(self.portal_row_desc) == 0:
            raise ProgrammingError("no result set")
        with self._lock:
            if len(self._cached_rows) == 0:
                if self.portal_suspended:
                    try:
                        self.c.fetch_rows(self)
                    except AttributeError:
                        raise CursorClosedError()
                if len(self._cached_rows) == 0:
                    return None
            return self._cached_rows.pop(0)

    ##
    # Read a row from the database server, and return it in a dictionary
    # indexed by column name/alias.  This method will raise an error if two
    # columns have the same name.  Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def read_dict(self):
        row = self.read_tuple()
        if row is None:
            return row
        retval = {}
        for i in range(len(self.bind_row_desc.fields)):
            col_name = self.bind_row_desc.fields[i]['name']
            if col_name in retval:
                raise InterfaceError(
                    "cannot return dict of row when two columns have the same "
                    "name (%r)" % (col_name,))
            retval[col_name] = row[i]
        return retval

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a tuple for each row, in the same manner as {@link
    # #PreparedStatement.read_tuple read_tuple}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def iterate_tuple(self):
        return DataIterator(self, PreparedStatement.read_tuple)

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a dict for each row, in the same manner as {@link
    # #PreparedStatement.read_dict read_dict}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def iterate_dict(self):
        return DataIterator(self, PreparedStatement.read_dict)


def inspect_int(value):
    if min_int2 < value < max_int2:
        return (21, FC_BINARY, h_pack)
    elif min_int4 < value < max_int4:
        return (23, FC_BINARY, i_pack)
    elif min_int8 < value < max_int8:
        return (20, FC_BINARY, q_pack)
    else:
        return (1700, FC_BINARY, numeric_send)
