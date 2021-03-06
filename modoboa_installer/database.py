"""Database related tools."""

import os
import pwd
import stat

from . import package
from . import utils


class Database(object):

    """Common database backend."""

    packages = None
    service = None

    def __init__(self, config):
        """Install if necessary."""
        self.config = config
        engine = self.config.get("database", "engine")
        self.dbhost = self.config.get("database", "host")
        self.dbuser = config.get(engine, "user")
        self.dbpassword = config.get(engine, "password")
        if self.config.getboolean("database", "install"):
            self.install_package()

    def install_package(self):
        """Install database package if required."""
        package.backend.install_many(self.packages[package.backend.FORMAT])
        utils.exec_cmd("service {} start".format(self.service))


class PostgreSQL(Database):

    """Postgres."""

    packages = {
        "deb": ["postgresql", "postgresql-server-dev-all"],
        "rpm": ["postgresql-server", "postgresql-devel"]
    }
    service = "postgresql"

    def __init__(self, config):
        super(PostgreSQL, self).__init__(config)
        self._pgpass_done = False

    def install_package(self):
        """Install database if required."""
        package.backend.install_many(self.packages[package.backend.FORMAT])
        if package.backend.FORMAT == "rpm":
            utils.exec_cmd("postgresql-setup initdb")
            pattern = "s/^host(.+)ident$/host$1md5/"
            cfgfile = "/var/lib/pgsql/data/pg_hba.conf"
            utils.exec_cmd("perl -pi -e '{}' {}".format(pattern, cfgfile))
        utils.exec_cmd("service {} start".format(self.service))

    def _exec_query(self, query, dbname=None, dbuser=None, dbpassword=None):
        """Exec a postgresql query."""
        cmd = "psql"
        if dbname and dbuser:
            self._setup_pgpass(dbname, dbuser, dbpassword)
            cmd += " -h {} -d {} -U {} -w".format(self.dbhost, dbname, dbuser)
        query = query.replace("'", "'\"'\"'")
        cmd = "{} -c '{}' ".format(cmd, query)
        utils.exec_cmd(cmd, sudo_user=self.dbuser)

    def create_user(self, name, password):
        """Create a user."""
        query = "SELECT 1 FROM pg_roles WHERE rolname='{}'".format(name)
        code, output = utils.exec_cmd(
            """psql -tAc "{}" | grep -q 1""".format(query),
            sudo_user=self.dbuser)
        if not code:
            return
        query = "CREATE USER {} PASSWORD '{}'".format(name, password)
        self._exec_query(query)

    def create_database(self, name, owner):
        """Create a database."""
        code, output = utils.exec_cmd(
            "psql -lqt | cut -d \| -f 1 | grep -w {} | wc -l"
            .format(name), sudo_user=self.dbuser)
        if code:
            return
        utils.exec_cmd(
            "createdb {} -O {}".format(name, owner),
            sudo_user=self.dbuser)

    def grant_access(self, dbname, user):
        """Grant access to dbname."""
        query = "GRANT ALL ON DATABASE {} TO {}".format(dbname, user)
        self._exec_query(query)

    def _setup_pgpass(self, dbname, dbuser, dbpasswd):
        """Setup .pgpass file."""
        if self._pgpass_done:
            return
        if self.dbhost not in ["localhost", "127.0.0.1"]:
            self._pgpass_done = True
            return
        pw = pwd.getpwnam(self.dbuser)
        target = os.path.join(pw[5], ".pgpass")
        with open(target, "w") as fp:
            fp.write("127.0.0.1:*:{}:{}:{}\n".format(
                dbname, dbname, dbpasswd))
        mode = stat.S_IRUSR | stat.S_IWUSR
        os.chmod(target, mode)
        os.chown(target, pw[2], pw[3])
        self._pgpass_done = True

    def load_sql_file(self, dbname, dbuser, dbpassword, path):
        """Load SQL file."""
        self._setup_pgpass(dbname, dbuser, dbpassword)
        utils.exec_cmd(
            "psql -h {} -d {} -U {} -w < {}".format(
                self.dbhost, dbname, dbuser, path),
            sudo_user=self.dbuser)


class MySQL(Database):

    """MySQL backend."""

    packages = {
        "deb": ["mysql-server", "libmysqlclient-dev"],
        "rpm": ["mariadb", "mariadb-devel", "mariadb-server"],
    }
    service = "mariadb" if package.backend.FORMAT == "rpm" else "mysql"

    def install_package(self):
        """Preseed package installation."""
        package.backend.preconfigure(
            "mysql-server", "root_password", "password", self.dbpassword)
        package.backend.preconfigure(
            "mysql-server", "root_password_again", "password", self.dbpassword)
        super(MySQL, self).install_package()
        if package.backend.FORMAT == "rpm":
            utils.exec_cmd("mysqladmin -u root password '{}'".format(
                self.dbpassword))

    def _exec_query(self, query, dbname=None, dbuser=None, dbpassword=None):
        """Exec a mysql query."""
        if dbuser is None and dbpassword is None:
            dbuser = self.dbuser
            dbpassword = self.dbpassword
        cmd = "mysql -h {} -u {} -p{}".format(self.dbhost, dbuser, dbpassword)
        if dbname:
            cmd += " -D {}".format(dbname)
        query = query.replace("'", "'\"'\"'")
        utils.exec_cmd(cmd + """ -e '{}' """.format(query))

    def create_user(self, name, password):
        """Create a user."""
        self._exec_query(
            "CREATE USER '{}'@'%' IDENTIFIED BY '{}'".format(
                name, password))
        self._exec_query(
            "CREATE USER '{}'@'localhost' IDENTIFIED BY '{}'".format(
                name, password))

    def create_database(self, name, owner):
        """Create a database."""
        self._exec_query(
            "CREATE DATABASE IF NOT EXISTS {} "
            "DEFAULT CHARACTER SET {} "
            "DEFAULT COLLATE {}".format(
                name, self.config.get("mysql", "charset"),
                self.config.get("mysql", "collation"))
        )
        self.grant_access(name, owner)

    def grant_access(self, dbname, user):
        """Grant access to dbname."""
        self._exec_query(
            "GRANT ALL PRIVILEGES ON {}.* to '{}'@'%'"
            .format(dbname, user))
        self._exec_query(
            "GRANT ALL PRIVILEGES ON {}.* to '{}'@'localhost'"
            .format(dbname, user))

    def load_sql_file(self, dbname, dbuser, dbpassword, path):
        """Load SQL file."""
        utils.exec_cmd(
            "mysql -h {} -u {} -p{} {} < {}".format(
                self.dbhost, dbuser, dbpassword, dbname, path)
        )


def get_backend(config):
    """Return appropriate backend."""
    engine = config.get("database", "engine")
    if engine == "postgres":
        backend = PostgreSQL
    elif engine == "mysql":
        backend = MySQL
    else:
        raise utils.FatalError("database engine not supported")
    return backend(config)


def create(config, name, password):
    """Create a database and a user."""
    backend = get_backend(config)
    backend.create_user(name, password)
    backend.create_database(name)


def grant_database_access(config, name, user):
    """Grant access to a database."""
    get_backend(config).grant_access(name, user)
