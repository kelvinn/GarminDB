"""Postgres compatibility hooks for the external idbutils package."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import datetime
import logging
import re

from sqlalchemy import create_engine, event, extract, func, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import NoSuchModuleError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Query
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import Integer, Time

import idbutils
import idbutils.db as idb_db
from idbutils.db_object import DbViewException


POSTGRES_DRIVER_MESSAGE = "Postgres support requires the psycopg driver. Install it with: pip install 'psycopg[binary]'"
_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_INSTALLED = False
_PROFILE_POSTGRES_NATIVE = 'postgres_native'


class PostgresSupportException(Exception):
    """Postgres support could not be initialized."""


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


@compiles(_TimeFromSecs)
def _compile_time_from_secs_default(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"time({value}, 'unixepoch')"


@compiles(_TimeFromSecs, 'postgresql')
def _compile_time_from_secs_postgres(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"(time '00:00' + make_interval(secs => CAST({value} AS double precision)))::time"


def normalize_postgres_url(database_url):
    """Normalize supported Postgres URL forms to SQLAlchemy's psycopg dialect."""
    if database_url.startswith('postgresql://'):
        return 'postgresql+psycopg://' + database_url[len('postgresql://'):]
    if database_url.startswith('postgres://'):
        return 'postgresql+psycopg://' + database_url[len('postgres://'):]
    return database_url


def _is_postgres_params(db_params):
    return getattr(db_params, 'db_type', None) in ('postgres', 'postgresql')


def _is_postgres_db(db):
    return _is_postgres_params(getattr(db, 'db_params', None))


def _is_postgres_session(session):
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == 'postgresql'


def _postgres_schema(cls, db_params):
    if getattr(db_params, 'test_db', False):
        return 'test_' + cls.db_name
    return cls.db_name


def _validate_identifier(identifier):
    if not _IDENTIFIER_RE.match(identifier):
        raise PostgresSupportException(f'Invalid Postgres schema name: {identifier}')


def _quote_identifier(identifier):
    _validate_identifier(identifier)
    return f'"{identifier}"'


def _ensure_psycopg_driver():
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError as e:
        if e.name == 'psycopg':
            raise PostgresSupportException(POSTGRES_DRIVER_MESSAGE) from e
        raise


def _postgres_url_from_params(db_params):
    db_name = getattr(db_params, 'db_name', None)
    if not db_name:
        raise PostgresSupportException('Postgres db type requires db_name.')
    return URL.create(
        'postgresql+psycopg',
        username=getattr(db_params, 'db_username', None),
        password=getattr(db_params, 'db_password', None),
        host=getattr(db_params, 'db_host', None) or 'localhost',
        port=getattr(db_params, 'db_port', None) or 5432,
        database=db_name
    )


def _postgres_backend_profile(db_params):
    return _PROFILE_POSTGRES_NATIVE


def _postgres_connect_args(db_params):
    connect_args = {
        'connect_timeout': int(getattr(db_params, 'postgres_connect_timeout_sec', 10))
    }
    statement_timeout = int(getattr(db_params, 'postgres_statement_timeout_ms', 0))
    if statement_timeout > 0:
        connect_args['options'] = f'-c statement_timeout={statement_timeout}'
    return connect_args


def _create_postgres_engine(url, db_params, echo=False):
    return create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        connect_args=_postgres_connect_args(db_params)
    )


def _postgres_url(cls, db_params):
    _ensure_psycopg_driver()
    database_url = getattr(db_params, 'database_url', None)
    if database_url:
        return normalize_postgres_url(database_url)
    return _postgres_url_from_params(db_params)


def _postgresql_url(cls, db_params):
    return cls._postgres_url(db_params)


def _set_search_path(dbapi_connection, connection_record, schema, backend_profile):
    _validate_identifier(schema)
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f'SET search_path TO {_quote_identifier(schema)}, public')
    finally:
        cursor.close()


def _set_local_search_path(connection, schema, backend_profile):
    _validate_identifier(schema)
    connection.exec_driver_sql(f'SET LOCAL search_path TO {_quote_identifier(schema)}, public')


def _install_search_path_event(engine, schema, backend_profile):
    event.listen(engine, 'connect', lambda dbapi_connection, connection_record: _set_search_path(dbapi_connection, connection_record, schema, backend_profile))
    # Transaction poolers can hand each transaction to a different server connection, so schema must be set at transaction start.
    event.listen(engine, 'begin', lambda connection: _set_local_search_path(connection, schema, backend_profile))


def _install_postgres_functions(connection, schema):
    schema_name = _quote_identifier(schema)
    connection.execute(text(f"""
        CREATE OR REPLACE FUNCTION {schema_name}.strftime(fmt text, value timestamp without time zone)
        RETURNS text
        LANGUAGE SQL
        IMMUTABLE
        AS $$
            SELECT CASE
                WHEN fmt = '%s' THEN trunc(extract(epoch FROM value))::bigint::text
                WHEN fmt = '%j' THEN to_char(value, 'DDD')
                ELSE to_char(value, fmt)
            END
        $$;
    """))
    connection.execute(text(f"""
        CREATE OR REPLACE FUNCTION {schema_name}.strftime(fmt text, value date)
        RETURNS text
        LANGUAGE SQL
        IMMUTABLE
        AS $$ SELECT {schema_name}.strftime(fmt, value::timestamp) $$;
    """))
    connection.execute(text(f"""
        CREATE OR REPLACE FUNCTION {schema_name}.strftime(fmt text, value time without time zone)
        RETURNS text
        LANGUAGE SQL
        IMMUTABLE
        AS $$
            SELECT CASE
                WHEN fmt = '%s' THEN trunc(extract(epoch FROM value))::bigint::text
                WHEN fmt = '%j' THEN '001'
                ELSE to_char(value, fmt)
            END
        $$;
    """))
    connection.execute(text(f"""
        CREATE OR REPLACE FUNCTION {schema_name}.strftime(fmt text, value text)
        RETURNS text
        LANGUAGE SQL
        IMMUTABLE
        AS $$ SELECT {schema_name}.strftime(fmt, value::time) $$;
    """))
    connection.execute(text(f"""
        CREATE OR REPLACE FUNCTION {schema_name}.round(value double precision, places integer)
        RETURNS numeric
        LANGUAGE SQL
        IMMUTABLE
        AS $$ SELECT pg_catalog.round(value::numeric, places) $$;
    """))


def _prepare_postgres_engine(engine, schema, backend_profile):
    _install_search_path_event(engine, schema, backend_profile)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS {_quote_identifier(schema)}'))
        connection.execute(text(f'SET search_path TO {_quote_identifier(schema)}, public'))
        _install_postgres_functions(connection, schema)


def _raise_postgres_driver_exception(exc):
    if isinstance(exc, PostgresSupportException):
        raise exc
    if isinstance(exc, NoSuchModuleError):
        raise PostgresSupportException(POSTGRES_DRIVER_MESSAGE) from exc
    if isinstance(exc, ModuleNotFoundError) and exc.name == 'psycopg':
        raise PostgresSupportException(POSTGRES_DRIVER_MESSAGE) from exc
    raise exc


def _postgres_delete(cls, db_params):
    schema = _postgres_schema(cls, db_params)
    _validate_identifier(schema)
    backend_profile = _postgres_backend_profile(db_params)
    engine = None
    try:
        engine = _create_postgres_engine(cls._postgres_url(db_params), db_params)
        _prepare_postgres_engine(engine, schema, backend_profile)
        with engine.begin() as connection:
            rows = connection.execute(
                text("SELECT table_name FROM information_schema.views WHERE table_schema = :schema"),
                {'schema': schema}
            ).all()
            for row in rows:
                connection.execute(text(f'DROP VIEW IF EXISTS {_quote_identifier(row[0])} CASCADE'))
        cls.Base.metadata.drop_all(engine)
    except Exception as exc:
        _raise_postgres_driver_exception(exc)
    finally:
        if engine is not None:
            engine.dispose()


def _postgresql_delete(cls, db_params):
    cls._postgres_delete(db_params)


def _compile_query(session, query):
    return str(query.statement.compile(bind=session.get_bind(), compile_kwargs={'literal_binds': True}))


def _create_view(session, view_name, query_str):
    if _is_postgres_session(session):
        quoted_view_name = _quote_identifier(view_name)
        session.execute(text(f'DROP VIEW IF EXISTS {quoted_view_name} CASCADE'))
        result = session.execute(text(f'CREATE VIEW {quoted_view_name} AS {query_str}'))
    else:
        result = session.execute(text('CREATE VIEW IF NOT EXISTS ' + view_name + ' AS ' + query_str))
    logging.getLogger(__name__).debug("Created view %s using query %s: %r", view_name, query_str, result)


def _time_result(result):
    if result is None:
        return datetime.time.min
    if isinstance(result, datetime.time):
        return result
    return datetime.datetime.strptime(str(result), '%H:%M:%S').time()


def install():
    """Install Postgres compatibility hooks on idbutils."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_init = idbutils.DB.__init__
    original_delete_view = idbutils.DbObject.delete_view
    original_create_view_if_doesnt_exist = idbutils.DbObject.create_view_if_doesnt_exist
    original_create_join_view = idbutils.DbObject.create_join_view
    original_create_multi_join_view = idbutils.DbObject.create_multi_join_view
    original_create_view_from_selectable = idbutils.DbObject._create_view_from_selectable
    original_s_get_months = idbutils.DbObject.s_get_months
    original_s_get_days = idbutils.DbObject.s_get_days
    original_s_get_col_func_of_max_per_day_for_value = idbutils.DbObject._s_get_col_func_of_max_per_day_for_value
    original_latest_time = idbutils.DbObject.latest_time

    def patched_init(self, db_params, debug_level=0):
        if not _is_postgres_params(db_params):
            return original_init(self, db_params, debug_level)
        idb_db.logger.debug("%s: %r debug: %s ", self.__class__.__name__, db_params, debug_level)
        idb_db.logger.setLevel(logging.DEBUG if debug_level > 0 else logging.INFO)
        self.db_params = db_params
        schema = _postgres_schema(self.__class__, db_params)
        backend_profile = _postgres_backend_profile(db_params)
        logger = logging.getLogger(__name__)
        logger.debug("Postgres backend profile resolved for %s schema %s: %s", self.__class__.__name__, schema, backend_profile)
        _validate_identifier(schema)
        self.schema_name = schema
        try:
            url_func = getattr(self, f'_{db_params.db_type}_url')
            self.engine = _create_postgres_engine(url_func(self.db_params), self.db_params, echo=(debug_level > 1))
            logger.debug("Preparing engine with backend profile %s", backend_profile)
            _prepare_postgres_engine(self.engine, schema, backend_profile)
            self.Base.metadata.create_all(self.engine)
            self.attributes = self._DbAttributes()
            self.attributes.version_check(self, self.db_version)
            for table in self.db_tables.values():
                self.init_table(table)
        except Exception as exc:
            _raise_postgres_driver_exception(exc)

    def patched_delete_view(cls, db, view_name=None):
        if not _is_postgres_db(db):
            return original_delete_view.__func__(cls, db, view_name)
        if view_name is None:
            view_name = cls._get_default_view_name()
        with db.managed_session() as session:
            session.execute(text(f'DROP VIEW IF EXISTS {_quote_identifier(view_name)} CASCADE'))

    def patched_create_view_if_doesnt_exist(cls, db, view_name, query_str):
        if not _is_postgres_db(db):
            return original_create_view_if_doesnt_exist.__func__(cls, db, view_name, query_str)
        with db.managed_session() as session:
            _create_view(session, view_name, query_str)

    def patched_create_join_view(cls, db, view_name, selectable, join_table, filter_by=None, order_by=None):
        if not _is_postgres_db(db):
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
        if not _is_postgres_db(db):
            return original_create_multi_join_view.__func__(cls, db, view_name, selectable, joins, order_by)
        with db.managed_session() as session:
            query = Query(selectable, session=session)
            for (join_table, join_clause) in joins:
                query = query.join(join_table, join_clause)
            if order_by is not None:
                query = query.order_by(order_by)
            _create_view(session, view_name, _compile_query(session, query))

    def patched_create_view_from_selectable(cls, db, view_name, selectable, order_by):
        if not _is_postgres_db(db):
            return original_create_view_from_selectable.__func__(cls, db, view_name, selectable, order_by)
        with db.managed_session() as session:
            query = Query(selectable, session=session).order_by(order_by)
            _create_view(session, view_name, _compile_query(session, query))

    def patched_secs_from_time(cls, col):
        return _SecsFromTime(col)

    def patched_time_from_secs(cls, value):
        return _TimeFromSecs(value)

    def patched_s_get_time_col_func(cls, session, col, stat_func, start_ts=None, end_ts=None):
        result = cls._s_query(session, cls._time_from_secs(stat_func(cls._secs_from_time(col))), None, start_ts, end_ts, cls._secs_from_time(col)).scalar()
        return _time_result(result)

    def patched_s_get_months(cls, session, year):
        if not _is_postgres_session(session):
            return original_s_get_months.__func__(cls, session, year)
        return cls._rows_to_ints_not_none(session.query(extract('month', cls.time_col)).filter(extract('year', cls.time_col) == int(year)).distinct().all())

    def patched_s_get_days(cls, session, year):
        if not _is_postgres_session(session):
            return original_s_get_days.__func__(cls, session, year)
        return cls._rows_to_ints(session.query(extract('doy', cls.time_col)).filter(extract('year', cls.time_col) == int(year)).distinct().all())

    def patched_s_get_col_func_of_max_per_day_for_value(cls, session, col, stat_func, start_ts, end_ts, match_col=None, match_value=None):
        if not _is_postgres_session(session):
            return original_s_get_col_func_of_max_per_day_for_value.__func__(cls, session, col, stat_func, start_ts, end_ts, match_col, match_value)
        max_daily_query = session.query(func.max(col).label('maxes')).filter(cls.during(start_ts, end_ts)).group_by(extract('doy', cls.time_col))
        if match_col is not None and match_value is not None:
            max_daily_query = max_daily_query.filter(match_col == match_value)
        return session.query(stat_func(max_daily_query.subquery().columns.maxes)).scalar()

    def patched_latest_time(cls, db, not_zero_col):
        if not _is_postgres_db(db):
            return original_latest_time.__func__(cls, db, not_zero_col)
        if isinstance(getattr(not_zero_col, 'type', None), Time):
            return cls.get_col_max_greater_than_value(db, cls.time_col, not_zero_col, datetime.time.min)
        return original_latest_time.__func__(cls, db, not_zero_col)

    idbutils.DB._postgres_url = classmethod(_postgres_url)
    idbutils.DB._postgresql_url = classmethod(_postgresql_url)
    idbutils.DB._postgres_delete = classmethod(_postgres_delete)
    idbutils.DB._postgresql_delete = classmethod(_postgresql_delete)
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
    idbutils.DbObject.latest_time = classmethod(patched_latest_time)

    _INSTALLED = True


install()
