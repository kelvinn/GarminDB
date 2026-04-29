"""Class that manages Garmin Connect download config."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import os
import sys
import platform
import subprocess
import datetime
import tempfile
from urllib.parse import urlparse, unquote

from idbutils import JsonConfig
from fitfile import Sport

from .statistics import Statistics
from idbutils import DbParams


class ConfigException(Exception):
    """Something unexpected happened while handling the configuration."""


class GarminConnectConfigManager(JsonConfig):
    """Class that manages Garmin Connect downloads."""

    temp_dir = tempfile.mkdtemp()
    homedir = os.path.expanduser('~')

    def __init__(self, config_dir=None):
        """Return a new GarminConnectConfigManager instance."""
        self.enabled_statistics = None
        self.config_dir = config_dir or self.__create_dir_if_needed(self.homedir + os.sep + '.GarminDb')
        config_file = self.config_dir + os.sep + 'GarminConnectConfig.json'
        try:
            super().__init__(config_file)
        except Exception as e:
            print(str(e))
            print(f"Missing or bad config: copy GarminConnectConfig.json.example from {os.path.dirname(os.path.abspath(__file__))} to {config_file} and edit it to "
                  "add your Garmin Connect username and password.")
            sys.exit(-1)

    def get_node_value(self, node, leaf):
        node = self.config.get(node)
        if node is not None:
            return node.get(leaf)

    def get_node_value_default(self, node, leaf, default):
        node = self.config.get(node)
        if node is not None:
            return node.get(leaf, default)
        return default

    @classmethod
    def __create_dir_if_needed(cls, dir):
        if not os.path.exists(dir):
            os.makedirs(dir)
        return dir

    def get_session_file(self):
        """Return the path to the session file."""
        return self.config_dir + os.sep + 'garth_session'

    def get_db_type(self):
        """Return the type (SQLite, MySQL, etc) of database that is configured."""
        return self.get_node_value_default('db', 'type', 'sqlite')

    def get_db_database_url(self):
        """Return the legacy configured database URL."""
        return self.get_node_value('db', 'database_url')

    def get_db_username(self):
        """Return the configured username of the database."""
        return self.get_node_value('db', 'db_username') or self.get_node_value('db', 'user')

    def get_db_password(self):
        """Return the configured password of the database."""
        return self.get_node_value('db', 'db_password') or self.get_node_value('db', 'password')

    def get_db_host(self):
        """Return the configured hostname of the database."""
        return self.get_node_value('db', 'db_host') or self.get_node_value('db', 'host')

    def get_db_port(self):
        """Return the configured port of the database."""
        return self.get_node_value('db', 'db_port')

    def get_db_name(self):
        """Return the configured database name."""
        return self.get_node_value('db', 'db_name')

    def get_db_dir(self, test_dir=False):
        """Return the configured directory of where the database will be stored."""
        return self.__create_dir_if_needed(self.get_base_dir(test_dir) + os.sep + 'DBs')

    @staticmethod
    def __env_value_from_file(env_file, key_name):
        if not os.path.isfile(env_file):
            return None
        with open(env_file, 'r') as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                if key.strip() == key_name:
                    return value.strip().strip('"').strip("'")
        return None

    @staticmethod
    def __database_url_from_env_file(env_file):
        return GarminConnectConfigManager.__env_value_from_file(env_file, 'DATABASE_URL')

    def __database_url_from_env_files(self):
        for env_file in (self.config_dir + os.sep + '.env', os.getcwd() + os.sep + '.env'):
            database_url = self.__database_url_from_env_file(env_file)
            if database_url:
                return database_url
        return None

    def get_database_url(self):
        """Return the configured database URL from config, environment, or .env."""
        return self.get_db_database_url() or os.environ.get('DATABASE_URL') or self.__database_url_from_env_files()

    def __motherduck_token_from_env_files(self):
        for env_file in (self.config_dir + os.sep + '.env', os.getcwd() + os.sep + '.env'):
            token = self.__env_value_from_file(env_file, 'MOTHERDUCK_TOKEN')
            if token:
                return token
        return None

    def get_motherduck_token(self):
        """Return the configured MotherDuck token from config, environment, or .env."""
        return self.get_node_value('db', 'motherduck_token') or os.environ.get('MOTHERDUCK_TOKEN') or self.__motherduck_token_from_env_files()

    @staticmethod
    def __db_port(port):
        if port in (None, ''):
            return None
        try:
            return int(port)
        except ValueError as e:
            raise ConfigException(f'Invalid db.db_port value: {port}') from e

    @staticmethod
    def __postgres_db_params_from_url(database_url):
        if not database_url:
            return {}
        parsed = urlparse(database_url)
        if parsed.scheme not in ('postgres', 'postgresql', 'postgresql+psycopg'):
            raise ConfigException(f'Unsupported Postgres database URL scheme: {parsed.scheme}')
        db_name = parsed.path.lstrip('/') or None
        return {
            'db_username' : unquote(parsed.username) if parsed.username else None,
            'db_password' : unquote(parsed.password) if parsed.password else None,
            'db_host' : parsed.hostname,
            'db_port' : parsed.port,
            'db_name' : unquote(db_name) if db_name else None
        }

    @staticmethod
    def __is_motherduck_host(host):
        return host is not None and 'motherduck.com' in host.lower()

    @staticmethod
    def __is_motherduck_database_url(database_url):
        if not database_url:
            return False
        parsed = urlparse(database_url)
        return GarminConnectConfigManager.__is_motherduck_host(parsed.hostname)

    def __configured_db_params(self):
        return {
            'db_username' : self.get_db_username(),
            'db_password' : self.get_db_password(),
            'db_host' : self.get_db_host(),
            'db_port' : self.__db_port(self.get_db_port()),
            'db_name' : self.get_db_name()
        }

    @staticmethod
    def __merge_db_params(db_params, fallback_params):
        for key, value in fallback_params.items():
            if db_params.get(key) in (None, '') and value not in (None, ''):
                db_params[key] = value
        return db_params

    def get_db_params(self, test_db=False):
        """Return the database configuration."""
        db_type = (self.get_db_type() or 'sqlite').lower()
        if db_type == 'postgresql':
            db_type = 'postgres'
        db_params = {
            'db_type' : db_type
        }
        if test_db:
            db_params['test_db'] = True
        if db_type == 'sqlite':
            db_params['db_path'] = self.get_db_dir(test_db)
        elif db_type == "mysql":
            db_params['db_type'] = 'mysql'
            db_params.update(self.__configured_db_params())
        elif db_type == 'postgres':
            configured_params = self.__configured_db_params()
            if self.__is_motherduck_host(configured_params.get('db_host')) or self.__is_motherduck_database_url(self.get_database_url()):
                raise ConfigException('MotherDuck must use db.type "motherduck"; the Postgres interface is not supported for MotherDuck.')
            legacy_params = self.__postgres_db_params_from_url(self.get_database_url())
            db_params.update(self.__merge_db_params(configured_params, legacy_params))
            if not db_params.get('db_name'):
                raise ConfigException('Postgres db type requires db.db_name, or a legacy database_url/DATABASE_URL containing a database name.')
        elif db_type == 'motherduck':
            db_params.update({
                'db_name' : self.get_db_name(),
                'motherduck_token' : self.get_motherduck_token()
            })
            if not db_params.get('db_name'):
                raise ConfigException('MotherDuck db type requires db.db_name.')
            if not db_params.get('motherduck_token'):
                raise ConfigException('MotherDuck db type requires db.motherduck_token or MOTHERDUCK_TOKEN.')
        return DbParams(**db_params)

    def get_base_dir(self, test_dir=False):
        """Return the configured directory of where the data files will be stored."""
        base = self.get_node_value_default('directories', 'base_dir', 'HealthData')
        if test_dir:
            return self.temp_dir + os.sep + base
        if self.get_node_value_default('directories', 'relative_to_home', True):
            return self.__create_dir_if_needed(self.homedir + os.sep + base)
        return self.__create_dir_if_needed(base)

    def get_backup_dir(self):
        """Return the path to the backup directory."""
        return self.__create_dir_if_needed(self.get_base_dir() + os.sep + 'Backups')

    def __get_fit_files_dir(self, test_dir=False):
        return self.get_base_dir(test_dir) + os.sep + 'FitFiles'

    def get_fit_files_dir(self, test_dir=False):
        """Return the configured directory of where the FIT files will be stored creating it if needed."""
        return self.__create_dir_if_needed(self.__get_fit_files_dir(test_dir))

    def __get_monitoring_base_dir(self):
        return self.get_base_dir() + os.sep + 'FitFiles' + os.sep + 'Monitoring'

    def get_monitoring_base_dir(self):
        """Return the configured directory of where the monitoring files will be stored creating it if needed."""
        return self.__create_dir_if_needed(self.__get_monitoring_base_dir())

    def get_monitoring_dir(self, year, test_dir=False):
        """Return the configured directory of where the monitoring files will be stored creating it if needed."""
        return self.__create_dir_if_needed(self.__get_monitoring_base_dir() + os.sep + str(year))

    def get_activities_dir(self, test_dir=False):
        """Return the configured directory of where the activities files will be stored."""
        return self.__create_dir_if_needed(self.__get_fit_files_dir(test_dir) + os.sep + 'Activities')

    def get_sleep_dir(self):
        """Return the configured directory of where the sleep files will be stored."""
        return self.__create_dir_if_needed(self.get_base_dir() + os.sep + 'Sleep')

    def get_weight_dir(self):
        """Return the configured directory of where the weight files will be stored."""
        return self.__create_dir_if_needed(self.get_base_dir() + os.sep + 'Weight')

    def get_rhr_dir(self):
        """Return the configured directory of where the resting heart rate files will be stored."""
        return self.__create_dir_if_needed(self.get_base_dir() + os.sep + 'RHR')

    def get_fitbit_dir(self):
        """Return the configured directory of where the FitBit will be stored."""
        return self.__create_dir_if_needed(self.get_base_dir() + os.sep + 'FitBitFiles')

    def get_mshealth_dir(self):
        """Return the configured directory of where the Microsoft Health will be stored."""
        return self.__create_dir_if_needed(self.get_base_dir() + os.sep + 'MSHealth')

    def get_plugins_dir(self):
        """Return the configured directory where the plugin files are located."""
        return self.__create_dir_if_needed(self.get_base_dir() + os.sep + 'Plugins')

    def get_metric(self):
        """Return the unit system (metric, statute) that is configured."""
        return self.get_node_value_default('settings', 'metric', False)

    def get_secure_password(self):
        """Return the Garmin Connect password from secure storage. On MacOS that is the KeyChain."""
        system = platform.system()
        if system == 'Darwin':
            # This relies on there being a 'internet password' entry for URL https://sso.garmin.com in the login keychain
            domain = 'sso.garmin.com'
            try:
                password = subprocess.check_output(["security", "find-internet-password", "-s", domain, "-w"])
                if password:
                    return password.rstrip()
            except Exception:
                pass
            raise ConfigException(f'Secure password was specified but no "Internet Password" entry was found in the Login Keychain for https://{domain}')

    def get_password_from_file(self):
        """Read the Garmin Connect password from a file."""
        password_file = self.get_node_value('credentials', 'password_file')
        try:
            with open(os.path.expanduser(password_file), 'r') as f:
                return f.read().strip()
        except Exception as e:
            raise ConfigException(f'Failed to read password from file {password_file}: {str(e)}')

    def get_user(self):
        """Return the Garmin Connect username."""
        return self.get_node_value('credentials', 'user')

    def get_password(self):
        """Return the Garmin Connect password."""
        if self.get_node_value_default('credentials', 'secure_password', False):
            return self.get_secure_password()
        if self.get_node_value_default('credentials', 'password_file', None):
            return self.get_password_from_file()
        return self.get_node_value('credentials', 'password')

    def get_garmin_base_domain(self):
        """Return the Garmin base domain to use for api calls."""
        return self.get_node_value_default('garmin', 'domain', "garmin.com")

    def default_display_activities(cls):
        """Return a list of the default activities to display."""
        return [Sport.strict_from_string(activity) for activity in super().default_display_activities]

    def latest_activity_count(self):
        """Return the number of activities to download when getting the latest."""
        return self.get_node_value('data', 'download_latest_activities')

    def all_activity_count(self):
        """Return the number of activities to download when getting all activities."""
        return self.get_node_value('data', 'download_all_activities')

    def stat_start_date(self, stat_type):
        """Return a tuple containing the start date and the number of days to fetch stats from."""
        start_date = self.get_node_value('data', stat_type + '_start_date')
        if start_date is None:
            # Fallback to a general start date or a reasonable default
            start_date = self.get_node_value('data', 'start_date')
        if start_date is None:
            # If still nothing, default to 20 days ago
            start_date = datetime.datetime.now().date() - datetime.timedelta(days=20)
        end_date = self.get_node_value_default('data', stat_type + '_end_date', datetime.datetime.now().date())
        days = (end_date - start_date).days
        return (start_date, days)

    def device_mount_dir(self):
        """Return the directory where the Garmin USB device is mounted."""
        return self.get_node_value('directories', 'mount_dir')

    def __device_garmin_dir(self):
        return self.device_mount_dir() + os.sep + 'garmin'

    def device_settings_dir(self):
        """Return the full path to the settings file on a mounted device."""
        return self.__device_garmin_dir() + os.sep + 'settings'

    def device_monitoring_dir(self):
        """Return the full path to the monitoring files on a mounted device."""
        return self.__device_garmin_dir() + os.sep + 'monitor'

    def device_sleep_dir(self):
        """Return the full path to the sleep files on a mounted device."""
        return self.__device_garmin_dir() + os.sep + 'sleep'

    def device_activities_dir(self):
        """Return the full path to the activities files on a mounted device."""
        return self.__device_garmin_dir() + os.sep + 'activity'

    def download_days_overlap(self):
        """Return the number of days to overlap previously downloaded data when downloading."""
        return self.get_node_value('data', 'download_days_overlap')

    def course_views(self, type):
        """Return a list of course ids to create views for for the given activitiy type."""
        return self.get_node_value('course_views', type)

    def is_stat_enabled(self, statistic):
        """Return whether a particular statistic is enabled or not."""
        return statistic in self.enabled_stats()

    def enabled_stats(self):
        """Return all enabled statistics as a list of string names."""
        if not self.enabled_statistics:
            json_enabled_stats_dict = self.config.get('enabled_stats', {stat_name: True for stat_name in list(Statistics)})
            self.enabled_statistics = [Statistics.from_string(stat_name) for stat_name, stat_enabled in json_enabled_stats_dict.items() if stat_enabled]
        return self.enabled_statistics

    def display_activities(self):
        """Return a list of activities to display."""
        activities_list = self.get_node_value('activities', 'display')
        if not activities_list:
            activities_list = self.get_node_value_default('settings', 'default_display_activities', [])
        return [Sport.strict_from_string(activity) for activity in activities_list]
