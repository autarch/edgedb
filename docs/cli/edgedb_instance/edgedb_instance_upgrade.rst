.. _ref_cli_edgedb_instance_upgrade:


=======================
edgedb instance upgrade
=======================

Upgrade EdgeDB instance or installatio

.. cli:synopsis::

    edgedb instance upgrade [OPTIONS] [<name>]


Description
===========

This command is used to upgrade EdgeDB instances individually or in
bulk.


Options
=======

:cli:synopsis:`<name>`
    The EdgeDB instance name to upgrade.

:cli:synopsis:`--force`
    Force upgrade process even if there is no new version.

:cli:synopsis:`--to-latest`
    Upgrade specified instance to the latest major version.

:cli:synopsis:`--to-nightly`
    Upgrade specified instance to a latest nightly version.

:cli:synopsis:`--local-minor`
    Upgrade all local instances to the latest minor versions.

:cli:synopsis:`-v, --verbose`
    Verbose output.
