.. _ref_cli_edgedb_instance_destroy:


=======================
edgedb instance destroy
=======================

Remove an EdgeDB instance.

.. cli:synopsis::

     edgedb instance destroy [OPTIONS] <name>


Description
===========

``edgedb instance destroy`` is a terminal command for removing an EdgeDB
instance and all its data.


Options
=======

:cli:synopsis:`<name>`
    The EdgeDB instance name.

:cli:synopsis:`--force`
    Destroy the instance even if it is referred to by a project.

:cli:synopsis:`-v, --verbose`
    Verbose output.
