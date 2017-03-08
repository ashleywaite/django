"""
Query subclasses which provide extra functionality beyond simple data retrieval.
"""

from types import MethodType

from django.core.exceptions import FieldError
from django.db import connections
from django.db.models.query_utils import Q
from django.db.models.sql.constants import (
    CURSOR, GET_ITERATOR_CHUNK_SIZE, NO_RESULTS,
)
from django.db.models.sql.query import Query

__all__ = [
    'DeleteQuery', 'UpdateQuery', 'InsertQuery', 'AggregateQuery',
    'LiteralQuery', 'WithQuery', 'InsertReturningQuery', 'UpdateReturningQuery']


class DeleteQuery(Query):
    """A DELETE SQL query."""

    compiler = 'SQLDeleteCompiler'

    def do_query(self, table, where, using):
        self.tables = [table]
        self.where = where
        cursor = self.get_compiler(using).execute_sql(CURSOR)
        return cursor.rowcount if cursor else 0

    def delete_batch(self, pk_list, using):
        """
        Set up and execute delete queries for all the objects in pk_list.

        More than one physical query may be executed if there are a
        lot of values in pk_list.
        """
        # number of objects deleted
        num_deleted = 0
        field = self.get_meta().pk
        for offset in range(0, len(pk_list), GET_ITERATOR_CHUNK_SIZE):
            self.where = self.where_class()
            self.add_q(Q(
                **{field.attname + '__in': pk_list[offset:offset + GET_ITERATOR_CHUNK_SIZE]}))
            num_deleted += self.do_query(self.get_meta().db_table, self.where, using=using)
        return num_deleted

    def delete_qs(self, query, using):
        """
        Delete the queryset in one SQL query (if possible). For simple queries
        this is done by copying the query.query.where to self.query, for
        complex queries by using subquery.
        """
        innerq = query.query
        # Make sure the inner query has at least one table in use.
        innerq.get_initial_alias()
        # The same for our new query.
        self.get_initial_alias()
        innerq_used_tables = [t for t in innerq.tables
                              if innerq.alias_refcount[t]]
        if not innerq_used_tables or innerq_used_tables == self.tables:
            # There is only the base table in use in the query.
            self.where = innerq.where
        else:
            pk = query.model._meta.pk
            if not connections[using].features.update_can_self_select:
                # We can't do the delete using subquery.
                values = list(query.values_list('pk', flat=True))
                if not values:
                    return 0
                return self.delete_batch(values, using)
            else:
                innerq.clear_select_clause()
                innerq.select = [
                    pk.get_col(self.get_initial_alias())
                ]
                values = innerq
            self.where = self.where_class()
            self.add_q(Q(pk__in=values))
        cursor = self.get_compiler(using).execute_sql(CURSOR)
        return cursor.rowcount if cursor else 0


class UpdateQuery(Query):
    """An UPDATE SQL query."""

    compiler = 'SQLUpdateCompiler'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_query()

    def _setup_query(self):
        """
        Run on initialization and after cloning. Any attributes that would
        normally be set in __init__ should go in here, instead, so that they
        are also set up after a clone() call.
        """
        self.values = []
        self.related_ids = None
        if not hasattr(self, 'related_updates'):
            self.related_updates = {}

    def clone(self, klass=None, **kwargs):
        return super().clone(klass, related_updates=self.related_updates.copy(), **kwargs)

    def update_batch(self, pk_list, values, using):
        self.add_update_values(values)
        for offset in range(0, len(pk_list), GET_ITERATOR_CHUNK_SIZE):
            self.where = self.where_class()
            self.add_q(Q(pk__in=pk_list[offset: offset + GET_ITERATOR_CHUNK_SIZE]))
            self.get_compiler(using).execute_sql(NO_RESULTS)

    def add_update_values(self, values):
        """
        Convert a dictionary of field name to value mappings into an update
        query. This is the entry point for the public update() method on
        querysets.
        """
        values_seq = []
        for name, val in values.items():
            field = self.get_meta().get_field(name)
            direct = not (field.auto_created and not field.concrete) or not field.concrete
            model = field.model._meta.concrete_model
            if not direct or (field.is_relation and field.many_to_many):
                raise FieldError(
                    'Cannot update model field %r (only non-relations and '
                    'foreign keys permitted).' % field
                )
            if model is not self.get_meta().model:
                self.add_related_update(model, field, val)
                continue
            values_seq.append((field, model, val))
        return self.add_update_fields(values_seq)

    def add_update_fields(self, values_seq):
        """
        Append a sequence of (field, model, value) triples to the internal list
        that will be used to generate the UPDATE query. Might be more usefully
        called add_update_targets() to hint at the extra information here.
        """
        for field, model, val in values_seq:
            if hasattr(val, 'resolve_expression'):
                # Resolve expressions here so that annotations are no longer needed
                val = val.resolve_expression(self, allow_joins=False, for_save=True)
            self.values.append((field, model, val))

    def add_related_update(self, model, field, value):
        """
        Add (name, value) to an update query for an ancestor model.

        Update are coalesced so that only one update query per ancestor is run.
        """
        self.related_updates.setdefault(model, []).append((field, None, value))

    def get_related_updates(self):
        """
        Return a list of query objects: one for each update required to an
        ancestor model. Each query will have the same filtering conditions as
        the current query but will only update a single table.
        """
        if not self.related_updates:
            return []
        result = []
        for model, values in self.related_updates.items():
            query = UpdateQuery(model)
            query.values = values
            if self.related_ids is not None:
                query.add_filter(('pk__in', self.related_ids))
            result.append(query)
        return result


class UpdateReturningQuery(UpdateQuery):
    compiler = 'SQLUpdateReturningCompiler'

    def clone(self, klass=None, **kwargs):
        clone = super().clone(klass, **kwargs)
        clone.values = self.values
        return clone


class InsertQuery(Query):
    compiler = 'SQLInsertCompiler'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields = []
        self.objs = []

    def insert_values(self, fields, objs, raw=False):
        self.fields = fields
        self.objs = objs
        self.raw = raw


class InsertReturningQuery(InsertQuery):
    compiler = 'SQLInsertReturningCompiler'

    def clone(self, klass=None, **kwargs):
        return super().clone(
            klass,
            fields=self.fields,
            objs=self.objs,
            raw=self.raw,
            **kwargs)

    def get_return_fields(self):
        return self.values_select, []


class AggregateQuery(Query):
    """
    Take another query as a parameter to the FROM clause and only select the
    elements in the provided list.
    """

    compiler = 'SQLAggregateCompiler'

    def add_subquery(self, query, using):
        query.subquery = True
        self.subquery, self.sub_params = query.get_compiler(using).as_sql(with_col_aliases=True)


class WithQuery(Query):
    compiler = 'SQLWithCompiler'

    def __init__(self, base_query, *args, **kwargs):
        self.base_query = base_query
        self.queries = []

    def add_with(self, query):
        self.queries.append(query)

    def collect_queries(self, with_alias="cte"):
        queries = []

        # Collect all queries attached to this or any attached queries
        for i, query in enumerate(self.queries):
            if query not in queries:
                query_alias = "{}_{}".format(with_alias, i)
                if isinstance(query, WithQuery):
                    query.base_query.with_alias = query_alias
                    queries.extend(query.collect_queries(with_alias=query_alias))
                    queries.append(query.base_query)
                else:
                    query.with_alias = query_alias
                    queries.append(query)

        self.add_extra_tables(queries)

        return queries

    def add_extra_tables(self, queries):
        self.base_query.extra_tables += tuple([
            query.with_alias for query in queries
            if query.with_alias not in self.base_query.extra_tables])

    def clone(self, klass=None, **kwargs):
        base_clone = self.base_query.clone(klass, **kwargs)
        clone = WithQuery(base_clone)
        clone.queries = self.queries
        return clone

    def set_values(self, fields):
        self.base_query.set_values(fields)

    def __getattr__(self, attr):
        # Pretend to be the base query unless it's specific to this
        return getattr(self.base_query, attr)


class LiteralQuery(Query):
    compiler = 'SQLLiteralCompiler'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields = None
        self.field_names = None
        self.objs = []

    def set_values(self, field_names):
        self.values_select = field_names
        if self.model:
            self.fields = [self.model._meta.get_field(field_name) for field_name in field_names]

    def clone(self, klass=None, **kwargs):
        return super().clone(
            klass,
            fields=self.fields,
            objs=self.objs,
            **kwargs)

    def clear_values(self):
        self.objs = []

    def append_values(self, objs, fields=None):
        self.objs.extend(objs)

    def get_return_fields(self):
        return self.values_select, []
