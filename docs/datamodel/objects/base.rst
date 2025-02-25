.. _ref_datamodel_object_types_base:

============
Base Objects
============

``std::BaseObject`` is the root of the object type hierarchy and all
object types in EdgeDB, including system types, extend ``std::BaseObject``
directly or indirectly.  User-defined object types extend from ``std::Object``,
which is a subtype of ``std::BaseObject``.

.. eql:type:: std::BaseObject

    Root object type.

    Definition:

    .. code-block:: sdl

        abstract type std::BaseObject {
            # Universally unique object identifier
            required readonly property id -> uuid;

            # Object type in the information schema.
            required readonly link __type__ -> schema::ObjectType;
        }

.. eql:type:: std::Object

    Root object type for user-defined types.

    Definition:

    .. code-block:: sdl

        abstract type std::Object extending std::BaseObject;


See Also
--------

Object type
:ref:`SDL <ref_eql_sdl_object_types>`,
:ref:`DDL <ref_eql_ddl_object_types>`,
and :ref:`introspection <ref_eql_introspection_object_types>`.
