import os

from middlewared.service import Service, private, job
from middlewared.schema import accepts, Str, Dict, Bool, List
# iocage's imports are per command, these are just general facilities
from iocage.lib.ioc_list import IOCList
from iocage.lib.ioc_json import IOCJson


class JailService(Service):

    def __init__(self, *args):
        super(JailService, self).__init__(*args)

    def test(self):
        return self.list("ALL", {"full": True, "header": True})

    @private
    def check_jail_existence(self, jail):
        jails, paths = IOCList("uuid").list_datasets()
        _jail = {tag: uuid for (tag, uuid) in jails.items() if
                 uuid.startswith(jail) or tag == jail}

        if len(_jail) == 1:
            tag, uuid = next(iter(_jail.items()))
            path = paths[tag]

            return tag, uuid, path
        elif len(_jail) > 1:
            raise RuntimeError("Multiple jails found for {}:".format(jail))
        else:
            raise RuntimeError("{} not found!".format(jail))

    @accepts(Str("lst_type", enum=["ALL", "RELEASE", "BASE", "TEMPLATE"]),
             Dict("options",
                  Bool("full"),
                  Bool("header"),
                  ))
    def list(self, lst_type, options={}):
        """Lists either 'all', 'base', 'template'"""
        lst_type = lst_type.lower()

        if lst_type == "release":
            lst_type = "base"

        full = options.get("full", False)
        hdr = options.get("header", False)

        if lst_type == "plugins":
            from iocage.lib.ioc_fetch import IOCFetch

            _list = IOCFetch("").fetch_plugin_index("", _list=True)
        else:
            _list = IOCList(lst_type, hdr, full).list_datasets()

        return _list

    @accepts(Str("jail"), Dict("options",
                               Str("prop"),
                               Bool("plugin"),
                               ))
    def set(self, jail, options):
        """Sets a jail property."""
        prop = options.get("prop", None)
        plugin = options.get("plugin", False)

        tag, uuid, path = self.check_jail_existence(jail)

        if "template" in prop.split("=")[0]:
            if "template" in path and prop != "template=no":
                raise RuntimeError("{uuid} ({tag}) is already a template!")
            elif "template" not in path and prop != "template=yes":
                raise RuntimeError("{uuid} ({tag}) is already a jail!")

        if plugin:
            _prop = prop.split(".")

            return IOCJson(path, cli=True).json_plugin_set_value(_prop)

        IOCJson(path, cli=True).json_set_value(prop)

        return True

    @accepts(Str("jail"), Dict("options",
                               Str("prop"),
                               Bool("plugin"),
                               ))
    def get(self, jail, options):
        """Gets a jail property."""
        prop = options.get("prop", None)
        plugin = options.get("plugin", False)

        tag, uuid, path = self.check_jail_existence(jail)

        if "template" in prop.split("=")[0]:
            if "template" in path and prop != "template=no":
                raise RuntimeError("{uuid} ({tag}) is already a template!")
            elif "template" not in path and prop != "template=yes":
                raise RuntimeError("{uuid} ({tag}) is already a jail!")

        if plugin:
            _prop = prop.split(".")
            return IOCJson(path).json_plugin_set_value(_prop)

        if prop == "all":
            return IOCJson(path).json_get_value(prop)
        elif prop == "state":
            status, _ = IOCList.list_get_jid(path.split("/")[3])

            if status:
                return "UP"
            else:
                return "DOWN"

        return IOCJson(path).json_get_value(prop)

    @accepts(Dict("options",
             Str("release"),
             Str("server"),
             Str("user"),
             Str("password"),
             Str("plugin_file"),
             Str("props"),
             ))
    @job(lock=lambda args: f"jail_fetch:{args[-1]}")
    def fetch(self, job, options):
        """Fetches a release or plugin."""
        from iocage.lib.ioc_fetch import IOCFetch

        release = options.get("release", None)
        server = options.get("server", "ftp.freebsd.org")
        user = options.get("user", "anonymous")
        password = options.get("password", "anonymous@")
        plugin_file = options.get("plugin_file", None)
        props = options.get("props", None)

        if plugin_file:
            IOCFetch("", server, user, password).fetch_plugin(plugin_file,
                                                              props, 0)
            return True

        IOCFetch(release, server, user, password).fetch_release()

        return True

    @accepts(Str("jail"))
    def destroy(self, jail):
        """Takes a jail and destroys it."""
        from iocage.lib.ioc_destroy import IOCDestroy

        tag, uuid, path = self.check_jail_existence(jail)
        conf = IOCJson(path).json_load()
        status, _ = IOCList().list_get_jid(uuid)

        if status:
            from iocage.lib.ioc_stop import IOCStop
            IOCStop(uuid, tag, path, conf, silent=True)

        IOCDestroy(uuid, tag, path).destroy_jail()

        return True

    @accepts(Str("jail"))
    def start(self, jail):
        """Takes a jail and starts it."""
        from iocage.lib.ioc_start import IOCStart

        tag, uuid, path = self.check_jail_existence(jail)
        conf = IOCJson(path).json_load()
        status, _ = IOCList().list_get_jid(uuid)

        if not status:
            if conf["type"] in ("jail", "plugin"):
                IOCStart(uuid, tag, path, conf)

                return True
            else:
                raise RuntimeError(f"{jail} must be type jail or plugin to"
                                   " be started")
        else:
            raise RuntimeError(f"{jail} already running.")

    @accepts(Str("jail"))
    def stop(self, jail):
        """Takes a jail and stops it."""
        from iocage.lib.ioc_stop import IOCStop

        tag, uuid, path = self.check_jail_existence(jail)
        conf = IOCJson(path).json_load()
        status, _ = IOCList().list_get_jid(uuid)

        if status:
            if conf["type"] in ("jail", "plugin"):
                IOCStop(uuid, tag, path, conf)

                return True
            else:
                raise RuntimeError(f"{jail} must be type jail or plugin to"
                                   " be stopped")
        else:
            raise RuntimeError(f"{jail} already stopped")

    @accepts(Dict("options",
             Str("release"),
             Str("template"),
             Str("pkglist"),
             # Str("uuid"),
             Bool("basejail"),
             Bool("empty"),
             Bool("short"),
             List("props"),
             ))
    def create(self, options):
        """Creates a jail."""
        from iocage.lib.ioc_create import IOCCreate

        release = options.get("release", None)
        template = options.get("template", None)
        pkglist = options.get("pkglist", None)
        # uuid = options.get("uuid", None)  Not in 0.9.7
        basejail = options.get("basejail", False)
        empty = options.get("empty", False)
        short = options.get("short", False)
        props = options.get("props", [])
        pool = IOCJson().json_get_value("pool")
        iocroot = IOCJson(pool).json_get_value("iocroot")

        if template:
            release = template

        if not os.path.isdir(f"{iocroot}/releases/{release}") and not \
                template and not empty:
            # FIXME: List index out of range
            # self.fetch(options={"release": release})
            pass

        IOCCreate(release, props, 0, pkglist, template=template,
                  short=short, basejail=basejail, empty=empty).create_jail()

        return True