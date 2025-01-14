.. _ref_eql_overview:

========
Concepts
========

EdgeQL input consists of a sequence of *commands*, and the database
returns a specific response to each command in sequence.

For example, the following EdgeQL :eql:stmt:`SELECT` command would return a
set of all ``User`` objects with the value of the ``name`` property equal to
``"John"``.

.. code-block:: edgeql

    SELECT User FILTER User.name = 'John';


.. _ref_eql_fundamentals_type_system:

Type System
===========

EdgeQL is a strongly typed language.  Every value in EdgeQL has a type,
which is determined statically from the database schema and the expression
that defines that value.  Refer to
:ref:`Data Model <ref_datamodel_typesystem>` for details about the type
system.


.. _ref_eql_fundamentals_set:

Everything is a Set
===================

Every value in EdgeQL is viewed as a set of elements.  A set may be empty
(*empty set*), contain a single element (a *singleton*), or contain multiple
elements.  Strictly speaking, EdgeQL sets are *multisets*, as they do not
require the elements to be unique.

A set cannot contain elements of different base types.  Mixing objects and
primitive types, as well as primitive types with a different base type, is
not allowed.

In SQL databases ``NULL`` is a special *value* denoting an absence of data.
EdgeDB works with *sets*, so an absence of data is just an empty set.


.. _ref_eql_fundamentals_references:

Set References and Paths
========================

A *set reference* is a *name* (a simple identifier or a qualified schema name)
that represents a set of values.  It can be the name of an object type or
an *expression alias* (defined in a statement :ref:`WITH block <ref_eql_with>`
or in the schema via an :ref:`alias declaration <ref_eql_sdl_aliases>`).

For example a reference to the ``User`` object type in the following
query will resolve to a set of all ``User`` objects:

.. code-block:: edgeql

    SELECT User;

Note, that unlike SQL no explicit ``FROM`` clause is needed.

A set reference can be an expression alias:

.. code-block:: edgeql

    WITH odd_numbers := {1, 3, 5, 7, 9}
    SELECT odd_numbers;

See :ref:`with block <ref_eql_with>` for more information on expression
aliases.

A *path expression* (or simply a *path*) is an expression followed by a
sequence of dot-separated link or property traversal specifications.  It
represents a set of values reachable from the source set.
See :ref:`ref_eql_expr_paths` for more information on path syntax and
behavior.

A *simple path* is a path which begins with a set reference.


.. _ref_eql_fundamentals_name_resolution:

Name Resolution
===============

In EdgeQL a name can either be *fully-qualified*, i.e. of the form
``module_name::entity_name`` or in short form of just ``entity_name``
(for more details see :ref:`ref_eql_lexical_names`). Any short name is
ultimately resolved to some fully-qualified name in the following
manner:

1) Look for a match to the short name in the current module (typically
   ``default``, but it can be changed).
2) Look for a match to the short name in the ``std`` module.

Normally the current module is called ``default``, which is
automatically created in any new database. It is possible to override
the current module globally on the session level with a ``SET MODULE
my_module`` :ref:`command <ref_eql_statements_session_set_alias>`. It
is also possible to override the current module on per-query basis
using ``WITH MODULE my_module`` :ref:`clause <ref_eql_with>`.


.. _ref_eql_fundamentals_aggregates:

Aggregates
==========

A function parameter or an operand of an operator can be declared as an
*aggregate parameter*.  An aggregate parameter means that the function or
operator are called *once* on an entire set passed as a corresponding
argument, rather than being called sequentially on each element of an
argument set.  A function or an operator with an aggregate parameter is
called an *aggregate*.  Non-aggregate functions and operators are
*regular* functions and operators.

For example, basic arithmetic :ref:`operators <ref_std_math>`
are regular operators, while the :eql:func:`sum` function and the
:eql:op:`DISTINCT` operator are aggregates.

An aggregate parameter is specified using the ``SET OF`` modifier
in the function or operator declaration.  See :eql:stmt:`CREATE FUNCTION`
for details.


.. _ref_eql_fundamentals_optional:

OPTIONAL
========

Normally, if a non-aggregate argument of a function or an operator is empty,
then the function will not be called and the result will be empty.

A function parameter or an operand of an operator can be declared as
``OPTIONAL``, in which case the function is called normally when the
corresponding argument is empty.

A notable example of a function that gets called on empty input
is the :eql:op:`coalescing <COALESCE>` operator.


.. _ref_eql_polymorphic_queries:

Polymorphic Queries
===================

:index: poly polymorphism nested shapes

A link target can be an abstract type, thus allowing objects of
different extending types to be referenced.  This necessitates writing
*polymorphic queries* that could fetch different data depending on the
type of the actual objects.  Consider the following schema:

.. code-block:: sdl

    abstract type Named {
        required property name -> str {
            delegated constraint exclusive;
        }
    }

    type User extending Named {
        property avatar -> str;
        multi link favorites -> Named;
    }

    type Game extending Named {
        property price -> int64;
    }

    type Article extending Named {
        property url -> str;
    }

Every ``User`` can have its ``favorites`` link point to either other
``User``, ``Game``, or ``Article``.  To fetch data related to
different types of objects in the ``favorites`` link the following
syntax can be used:

.. code-block:: edgeql

    SELECT User {
        name,
        avatar,
        favorites: {
            # common to all Named
            name,

            # specific to Games
            [IS Game].price,

            # specific to Article
            [IS Article].url,

            # specific to User
            [IS User].avatar,

            # a computed property tracking how many favorites
            # does my favorite User have?
            favorites_count := count(
                # start the path at the root of the shape
                User.favorites[IS User].favorites)
        }
    }

The :eql:op:`[IS TypeName] <ISINTERSECT>` construct can be used in
:ref:`paths <ref_eql_expr_paths>` to restrict the target to a specific
type.  When it is used in :ref:`shapes <ref_eql_expr_shapes>` it
allows to create polymorphic nested queries.

Another scenario where polymorphic queries may be useful is when a
link target is a :eql:op:`union type <TYPEOR>`.

It is also possible to fetch data that contains only one of the
possible types of ``favorites`` even if a particular ``User`` has a
mix of everything:

.. code-block:: edgeql

    # User + favorite Articles only
    SELECT User {
        name,
        favorites[IS Article]: {
            name,
            url
        }
    }
