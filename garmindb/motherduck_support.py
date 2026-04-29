"""MotherDuck compatibility hooks for the external idbutils package."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import datetime
import logging
import re

from sqlalchemy import create_engine, event, extract, func, text
from sqlalchemy.exc import NoSuchModuleError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Query
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import Integer, Time

import idbutils
import idbutils.db as idb_db
from idbutils.db_object import DbViewException


MOTHERDUCK_DRIVER_MESSAGE = "MotherDuck support requires duckdb and duckdb-engine. Install them with: pip install duckdb duckdb-engine"
_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_INSTALLED = False
_GARMIN_SCHEMAS = frozenset({'garmin', 'garmin_monitoring', 'garmin_activities', 'garmin_summary', 'summary'})


class MotherDuckSupportException(Exception):
    """MotherDuck support could not be initialized."""


class _SecsFromTime(FunctionElement):
    type = Integer()
    inherit_cache = True


class _TimeFromSecs(FunctionElement):
    type = Time()
    inherit_cache = True


@compiles(_SecsFromTime)
def _compile_secs_from_time_default(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"(strftime('%s', {value}) - strftime('%s', '00:00'))"


@compiles(_SecsFromTime, 'postgresql')
def _compile_secs_from_time_postgres(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"EXTRACT(EPOCH FROM CAST({value} AS time))"


@compiles(_SecsFromTime, 'duckdb')
def _compile_secs_from_time_duckdb(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"CAST(epoch(CAST({value} AS TIME)) AS INTEGER)"


@compiles(_TimeFromSecs)
def _compile_time_from_secs_default(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"time({value}, 'unixepoch')"


@compiles(_TimeFromSecs, 'postgresql')
def _compile_time_from_secs_postgres(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"(time '00:00' + make_interval(secs => CAST({value} AS double precision)))::time"


@compiles(_TimeFromSecs, 'duckdb')
def _compile_time_from_secs_duckdb(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"CAST(TIMESTAMP '1970-01-01' + CAST({value} AS DOUBLE) * INTERVAL '1 second' AS TIME)"


def _is_motherduck_params(db_params):
    return getattr(db_params, 'db_type', None) == 'motherduck'


def _is_motherduck_db(db):
    return _is_motherduck_params(getattr(db, 'db_params', None))


def _is_motherduck_session(session):
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == 'duckdb'


def _validate_identifier(identifier):
    if not _IDENTIFIER_RE.match(identifier):
        raise MotherDuckSupportException(f'Invalid MotherDuck schema name: {identifier}')


def _quote_identifier(identifier):
    _validate_identifier(identifier)
    return f'"{identifier}"'


def _schema_literal(identifier):
    _validate_identifier(identifier)
    return f"'{identifier}'"


def _motherduck_schema(cls, db_params):
    if cls.db_name not in _GARMIN_SCHEMAS:
        raise MotherDuckSupportException(
            f'MotherDuck support is only available for Garmin workflow databases: {", ".join(sorted(_GARMIN_SCHEMAS))}.'
        )
    if getattr(db_params, 'test_db', False):
        return 'test_' + cls.db_name
    return cls.db_name


def _ensure_duckdb_driver():
    try:
        import duckdb  # noqa: F401
        import duckdb_engine  # noqa: F401
    except ModuleNotFoundError as e:
        if e.name in ('duckdb', 'duckdb_engine'):
            raise MotherDuckSupportException(MOTHERDUCK_DRIVER_MESSAGE) from e
        raise


def _motherduck_url(cls, db_params):
    _ensure_duckdb_driver()
    db_name = getattr(db_params, 'db_name', None)
    if not db_name:
        raise MotherDuckSupportException('MotherDuck db type requires db_name.')
    return f'duckdb:///md:{db_name}'


def _motherduck_connect_args(db_params):
    token = getattr(db_params, 'motherduck_token', None)
    if not token:
        raise MotherDuckSupportException('MotherDuck db type requires motherduck_token.')
    return {'config': {'motherduck_token': token}}


def _set_search_path(dbapi_connection, connection_record, schema):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f'SET search_path = {_schema_literal(schema)}')
    finally:
        cursor.close()


def _set_local_search_path(connection, schema):
    connection.exec_driver_sql(f'SET search_path = {_schema_literal(schema)}')


def _install_search_path_event(engine, schema):
    event.listen(engine, 'connect', lambda dbapi_connection, connection_record: _set_search_path(dbapi_connection, connection_record, schema))
    event.listen(engine, 'checkout', lambda dbapi_connection, connection_record, connection_proxy: _set_search_path(dbapi_connection, connection_record, schema))
    event.listen(engine, 'begin', lambda connection: _set_local_search_path(connection, schema))


def _prepare_motherduck_engine(engine, schema):
    _validate_identifier(schema)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS {_quote_identifier(schema)}'))
        connection.execute(text(f'SET search_path = {_schema_literal(schema)}'))
    _install_search_path_event(engine, schema)


def _raise_motherduck_driver_exception(exc):
    if isinstance(exc, MotherDuckSupportException):
        raise exc
    if isinstance(exc, NoSuchModuleError):
        raise MotherDuckSupportException(MOTHERDUCK_DRIVER_MESSAGE) from exc
    if isinstance(exc, ModuleNotFoundError) and exc.name in ('duckdb', 'duckdb_engine'):
        raise MotherDuckSupportException(MOTHERDUCK_DRIVER_MESSAGE) from exc
    raise exc


def _motherduck_delete(cls, db_params):
    schema = _motherduck_schema(cls, db_params)
    _validate_identifier(schema)
    engine = None
    try:
        engine = create_engine(cls._motherduck_url(db_params), connect_args=_motherduck_connect_args(db_params))
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS {_quote_identifier(schema)} CASCADE'))
    except Exception as exc:
        _raise_motherduck_driver_exception(exc)
    finally:
        if engine is not None:
            engine.dispose()


def _compile_query(session, query):
    return str(query.statement.compile(bind=session.get_bind(), compile_kwargs={'literal_binds': True}))


def _create_view(session, view_name, query_str):
    quoted_view_name = _quote_identifier(view_name)
    session.execute(text(f'DROP VIEW IF EXISTS {quoted_view_name} CASCADE'))
    result = session.execute(text(f'CREATE VIEW {quoted_view_name} AS {query_str}'))
    logging.getLogger(__name__).debug("Created view %s using query %s: %r", view_name, query_str, result)


def _time_result(result):
    if result is None:
        return datetime.time.min
    if isinstance(result, datetime.time):
        return result
    return datetime.datetime.strptime(str(result), '%H:%M:%S').time()


def install():
    """Install MotherDuck compatibility hooks on idbutils."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_init = idbutils.DB.__init__
    original_delete_view = idbutils.DbObject.delete_view
    original_create_view_if_doesnt_exist = idbutils.DbObject.create_view_if_doesnt_exist
    original_create_join_view = idbutils.DbObject.create_join_view
    original_create_multi_join_view = idbutils.DbObject.create_multi_join_view
    original_create_view_from_selectable = idbutils.DbObject._create_view_from_selectable
    original_s_get_time_col_func = idbutils.DbObject._s_get_time_col_func
    original_s_get_months = idbutils.DbObject.s_get_months
    original_s_get_days = idbutils.DbObject.s_get_days
    original_s_get_col_func_of_max_per_day_for_value = idbutils.DbObject._s_get_col_func_of_max_per_day_for_value

    def patched_init(self, db_params, debug_level=0):
        if not _is_motherduck_params(db_params):
            return original_init(self, db_params, debug_level)
        idb_db.logger.debug("%s: %r debug: %s ", self.__class__.__name__, db_params, debug_level)
        idb_db.logger.setLevel(logging.DEBUG if debug_level > 0 else logging.INFO)
        self.db_params = db_params
        schema = _motherduck_schema(self.__class__, db_params)
        self.schema_name = schema
        try:
            engine = create_engine(self._motherduck_url(self.db_params), echo=(debug_level > 1), connect_args=_motherduck_connect_args(self.db_params))
            _prepare_motherduck_engine(engine, schema)
            self.engine = engine.execution_options(schema_translate_map={None: schema})
            self.Base.metadata.create_all(self.engine)
            self.attributes = self._DbAttributes()
            self.attributes.version_check(self, self.db_version)
            for table in self.db_tables.values():
                self.init_table(table)
        except Exception as exc:
            _raise_motherduck_driver_exception(exc)

    def patched_delete_view(cls, db, view_name=None):
        if not _is_motherduck_db(db):
            return original_delete_view.__func__(cls, db, view_name)
        if view_name is None:
            view_name = cls._get_default_view_name()
        with db.managed_session() as session:
            session.execute(text(f'DROP VIEW IF EXISTS {_quote_identifier(view_name)} CASCADE'))

    def patched_create_view_if_doesnt_exist(cls, db, view_name, query_str):
        if not _is_motherduck_db(db):
            return original_create_view_if_doesnt_exist.__func__(cls, db, view_name, query_str)
        with db.managed_session() as session:
            _create_view(session, view_name, query_str)

    def patched_create_join_view(cls, db, view_name, selectable, join_table, filter_by=None, order_by=None):
        if not _is_motherduck_db(db):
            return original_create_join_view.__func__(cls, db, view_name, selectable, join_table, filter_by, order_by)
        with db.managed_session() as session:
            try:
                query = Query(selectable, session=session).join(join_table)
                if filter_by is not None:
                    query = query.filter(filter_by)
                if order_by is not None:
                    query = query.order_by(order_by)
                _create_view(session, view_name, _compile_query(session, query))
            except Exception as e:
                raise DbViewException(f"Failed to create DB view {view_name} with table {join_table}", e)

    def patched_create_multi_join_view(cls, db, view_name, selectable, joins, order_by=None):
        if not _is_motherduck_db(db):
            return original_create_multi_join_view.__func__(cls, db, view_name, selectable, joins, order_by)
        with db.managed_session() as session:
            query = Query(selectable, session=session)
            for (join_table, join_clause) in joins:
                query = query.join(join_table, join_clause)
            if order_by is not None:
                query = query.order_by(order_by)
            _create_view(session, view_name, _compile_query(session, query))

    def patched_create_view_from_selectable(cls, db, view_name, selectable, order_by):
        if not _is_motherduck_db(db):
            return original_create_view_from_selectable.__func__(cls, db, view_name, selectable, order_by)
        with db.managed_session() as session:
            query = Query(selectable, session=session).order_by(order_by)
            _create_view(session, view_name, _compile_query(session, query))

    def patched_secs_from_time(cls, col):
        return _SecsFromTime(col)

    def patched_time_from_secs(cls, value):
        return _TimeFromSecs(value)

    def patched_s_get_time_col_func(cls, session, col, stat_func, start_ts=None, end_ts=None):
        if not _is_motherduck_session(session):
            return original_s_get_time_col_func.__func__(cls, session, col, stat_func, start_ts, end_ts)
        result = cls._s_query(session, cls._time_from_secs(stat_func(cls._secs_from_time(col))), None, start_ts, end_ts, cls._secs_from_time(col)).scalar()
        return _time_result(result)

    def patched_s_get_months(cls, session, year):
        if not _is_motherduck_session(session):
            return original_s_get_months.__func__(cls, session, year)
        return cls._rows_to_ints_not_none(session.query(extract('month', cls.time_col)).filter(extract('year', cls.time_col) == int(year)).distinct().all())

    def patched_s_get_days(cls, session, year):
        if not _is_motherduck_session(session):
            return original_s_get_days.__func__(cls, session, year)
        return cls._rows_to_ints(session.query(extract('doy', cls.time_col)).filter(extract('year', cls.time_col) == int(year)).distinct().all())

    def patched_s_get_col_func_of_max_per_day_for_value(cls, session, col, stat_func, start_ts, end_ts, match_col=None, match_value=None):
        if not _is_motherduck_session(session):
            return original_s_get_col_func_of_max_per_day_for_value.__func__(cls, session, col, stat_func, start_ts, end_ts, match_col, match_value)
        max_daily_query = session.query(func.max(col).label('maxes')).filter(cls.during(start_ts, end_ts)).group_by(extract('doy', cls.time_col))
        if match_col is not None and match_value is not None:
            max_daily_query = max_daily_query.filter(match_col == match_value)
        return session.query(stat_func(max_daily_query.subquery().columns.maxes)).scalar()

    idbutils.DB._motherduck_url = classmethod(_motherduck_url)
    idbutils.DB._motherduck_delete = classmethod(_motherduck_delete)
    idbutils.DB.__init__ = patched_init
    idbutils.DbObject.delete_view = classmethod(patched_delete_view)
    idbutils.DbObject.create_view_if_doesnt_exist = classmethod(patched_create_view_if_doesnt_exist)
    idbutils.DbObject.create_join_view = classmethod(patched_create_join_view)
    idbutils.DbObject.create_multi_join_view = classmethod(patched_create_multi_join_view)
    idbutils.DbObject._create_view_from_selectable = classmethod(patched_create_view_from_selectable)
    idbutils.DbObject._secs_from_time = classmethod(patched_secs_from_time)
    idbutils.DbObject._time_from_secs = classmethod(patched_time_from_secs)
    idbutils.DbObject._s_get_time_col_func = classmethod(patched_s_get_time_col_func)
    idbutils.DbObject.s_get_months = classmethod(patched_s_get_months)
    idbutils.DbObject.s_get_days = classmethod(patched_s_get_days)
    idbutils.DbObject._s_get_col_func_of_max_per_day_for_value = classmethod(patched_s_get_col_func_of_max_per_day_for_value)

    _INSTALLED = True


install()
